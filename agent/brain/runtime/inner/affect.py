"""情緒狀態的**適配層**：核心是 vendored（`brain/vendor/aem_v2.py`，未改）。

核心實作與契約註解見 `brain/vendor/aem_v2.py`；行為約束如下：
  · **權威是契約不是程式**；要改行為改這一層，不要動核心。
  · 順序硬需求：`message` → `appraisal`（appraisal 的 `source_event_id` 必須指向已存在的 message）。
  · 同一 source 只結案一次；**她自己的輸出不得入判定**（echo 防護）。
  · 投影是閉式的：`project()` 只吃 `now − occurred_at` ⇒ 不落快照也能算任意時刻。

我們這裡只做「快·瞬態」那一半（AEM）。關係向量（慢·穩態）另有其層，不混進來。

`device` 這一維（契約第 5 條「角色 × 端末」）：我們現在是**一份/角色**，
寫死 `device="app"`；將來多端末就讓它變成真的分片。
"""

from __future__ import annotations

import hashlib
import math
import os
import pathlib
import re
import threading
import time

from agent.brain.runtime.inner import dedupe
from agent.brain.runtime.inner import affect_repair as repair
from agent.brain.runtime.inner.mood_events import notify
# 心情 → 語氣的詞表與 `mood_block` 在 `mood_words.py`（情感整理 P7）；這裡轉出，舊 import 照用
from agent.brain.runtime.inner.mood_words import mood_block  # noqa: F401
from agent.brain.vendor import aem_v2

#: 沒有政策檔時的預設（契約的 `validate_policy` 會驗它）。
DEFAULT_POLICY = {
    #: 衰減半衰期（分鐘）。a 從 120 縮到 60（owner 2026-10-24）：schema 不允許負的
    #: arousal 增量，所以「興奮退掉」只能靠衰減快一點。
    "half_life_min": {"v": 240, "a": 60},
    "gain": {"v": 0.45, "a": 0.35},
    #: 事件在記憶裡活多久（owner 2026-10-24：48h → 12h）。
    #: 真正的「淡去」是 half_life_min（v=4h／a=2h）；這裡只是上限——一次不愉快掛兩天
    #: 太久，短一點她才會有起伏。
    "affect_ttl_h": 12,
    "proactive_threshold": 0.6,
    "scan_intervals_s": [1200, 600, 300],
    # 她獨處久了會想找人（心跳靠這條；預設 90 分鐘開始算）
    "absence": {"enabled": True, "after_s": 5400},
    "allowed_action_kinds": ["report_blocker", "check_in"],
    "action_ttl_s": 7200,
    "respect_do_not_interrupt": True,
    "max_proactive_24h": 12,
    "min_proactive_gap_s": 900,
    "reservation_ttl_s": 60,
    "watch_ttl_s": 7200,
    "task_ttl_s": 86400,
    "interruptibility_ttl_s": 1200,
    "appraisal_window_s": 120,
    "max_state_bytes": 8388608,
    "rules": {
        "absence": {"delta_v": -0.2, "delta_a": 0.2, "urgency": 0.65},
        "task_blocked": {"delta_v": -0.3, "delta_a": 0.4, "urgency": 0.8},
    },
    "expression_constraints": ["NO_REPLY_DEMAND", "NO_GUILT_APPEAL"],
    # owner 2026-11-09（AEM 稽核）：q0 10→3——10 個「中性假事件」把尺壓死（單筆 dv .18 只推 0.036）。
    # 實測（重放三人 24h）：q0=3 時一輪對話約 +0.06、兩輪在 TTL 內 ≈0.11，曲線才有起伏。
    # arousal 二階核 180/1200s→15/240s：舊峰值在 402 秒（6.7 分），回話那 5–20 秒只到峰值的 7.5%
    # ⇒ 她「當下沒被喚起、六分鐘後才上頭」。新峰值 ≈44 秒、約 9 分鐘退乾淨。
    "dynamics": {
        "valence": "avg", "arousal": "peak", "q0": 3.0,
        "arousal_kernel": {"type": "2nd", "tau1_s": 15.0, "tau2_s": 240.0},
    },
}

#: 「他明確拒絕／叫停」的字眼 ⇒ 負增量（owner 2026-10-24）
REJECTION_MARKS = ("やめて", "やめろ", "いらない", "いやだ", "結構です", "けっこう",
                   "不用", "不要", "別再", "别再", "停下", "住手", "不用了", "不要了")

#: 事件 → (delta_valence, delta_arousal)
EVENT_AFFECT: dict[str, tuple[float, float]] = {
    "interrupted": (-0.12, 0.18),   # 被打斷：不太高興、被喚起
    "rejected": (-0.09, 0.05),      # 要求被拒／被叫停：低落一點
    "ignored": (-0.07, -0.05),      # 她開口沒人接：失落、靜下來
}

#: 這些種類一定是「往下」的（不管模型給的 valence 是正還是負）
NEGATIVE_KINDS = frozenset({"concern", "frustration", "uncertainty"})

#: 重複同樣的評分幾次之後開始折半（owner 2026-10-24「評分多樣性保護」）
REPEAT_DAMPEN_AFTER = 3

#: 她自己的輸出（TTS／assistant）⇒ 一律不得入判定（防自激閉環）。
SELF_SPEAKERS = {"self", "assistant", "tts", "bot", "entity"}


def _slug(value: str, fallback: str = "unknown") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean or fallback


def _kernel(dyn: dict, elapsed: float, hl_min: float) -> float:
    """arousal 的時間核（二階＝正規化到峰值 1；一階＝2^(-t/HL)）。"""
    kr = (dyn or {}).get("arousal_kernel")
    if not kr or kr.get("type") != "2nd":
        return 2.0 ** (-elapsed / (hl_min * 60.0))
    tau1, tau2 = float(kr["tau1_s"]), float(kr["tau2_s"])
    if elapsed < 0:
        return 0.0
    k = tau2 / (tau2 - tau1)
    peak_t = tau1 * tau2 * math.log(tau2 / tau1) / (tau2 - tau1)
    peak = k * (math.exp(-peak_t / tau2) - math.exp(-peak_t / tau1))
    return k * (math.exp(-elapsed / tau2) - math.exp(-elapsed / tau1)) / peak


#: 跨源同句的合併索引在 `runtime/inner/dedupe.py`（owner 2026-11-01「30 秒內、非同源、同一句話算一件」）
_DEDUPE_WINDOW_S = dedupe.WINDOW_S_DEFAULT
_fingerprint = dedupe.fingerprint
_source_key = dedupe.source_key


class AffectService:
    """每個角色一份狀態；`advance()` 是心跳，`appraise()` 是判定輸入。"""

    def __init__(self, root, *, device: str = "app", policy: dict | None = None,
                 enabled: bool = True, log=None, clock=time.time, on_change=None) -> None:
        self.root = pathlib.Path(root)
        self.device = _slug(device, "app")
        self.policy = dict(policy or DEFAULT_POLICY)
        self.enabled = bool(enabled)
        self.on_change = on_change
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._lock = threading.RLock()
        self._last_touch: dict[str, float] = {}
        #: 每個角色最近的評分指紋（給 `_dampen` 判重複用）
        self._recent: dict[str, list[tuple]] = {}
        #: 這隻 process 已經檢查過政策的角色（避免每輪都讀一次 state）
        self._migrated: set[str] = set()
        #: 跨源同句合併（owner 2026-11-01）：查表／落盤都在 `runtime/inner/dedupe.py`。
        #: `BLUEAGENT_AEM_DEDUPE_S=0` ＝ 關掉這條規則（回舊行為：兩個入口各記一筆）
        self._dedupe = dedupe.CrossSourceDedupe(
            self.root, clock=clock, log=lambda message: self._log(message))
        self.dedupe_window_s = self._dedupe.window_s
        #: 睡眠時的衰減換速（owner 2026-11-16）：`entity -> time_scale(t0, t1)`；None ＝ 舊行為。
        #: 由 `compose/life_wiring.build_lives` 依生物鐘接上（`inner/sleep_decay.py`）。
        self.time_scale = None

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _store(self, entity: str):
        return aem_v2.Store(root=str(self.root / _slug(entity)),
                            agent=self.device)

    def ensure(self, entity: str) -> dict:
        """確保這個角色的 state 存在，回它。

        併發：心跳執行緒與回話執行緒會同時進來 ⇒ 兩邊都可能看到 `NOT_INITIALIZED`，
        一邊 `init()` 成功、另一邊就被 `ALREADY_INITIALIZED` 打回來。那**不是錯**
        （另一邊剛好先建好），重讀就好；以前這裡沒接住，整輪就變成「沒 event_id」的
        錯誤 dict（2026-10-27 全套測試偶發紅就是這條路）。`self._lock` 也在這裡拿住，
        同一個 service 的執行緒不會互相踩。
        """
        store = self._store(entity)
        with self._lock:
            try:
                state = store.read()
            except aem_v2.AEMError as exc:
                if exc.code == "STATE_CORRUPT":
                    return self._reproject(entity)
                if exc.code != "NOT_INITIALIZED":
                    raise
                try:
                    store.init(self.policy, self._clock())
                except aem_v2.AEMError as init_exc:
                    if init_exc.code != "ALREADY_INITIALIZED":
                        raise
                return store.read()
            self._migrate_policy(entity)
            return state

    def _reproject(self, entity: str) -> dict:
        """一次性重投影（實作在 `inner/affect_repair.py`）。"""
        return repair.reproject(self, entity)

    def _migrate_policy(self, entity: str) -> None:
        """把 state 裡存的 policy 換成新的（實作在 `inner/affect_repair.py`）。"""
        repair.migrate_policy(self, entity)

    # ── 心跳：只衰減／重算，不是刺激 ──────────────────────────────────────────
    def advance(self, entity: str) -> dict:
        if not self.enabled:
            return {}
        with self._lock:
            self.ensure(entity)
            repair.rearm_absence(self, entity)          # 稽核第 4 組 E：長假也要能重新「想他」
            view = self._store(entity).advance(self._clock()).get("view") or {}
        notify(self.on_change, entity, self._log)
        return view

    def snapshot(self, entity: str) -> dict:
        """只讀（不建檔、不 advance）——給 `/api/state` 與心跳判定用。"""
        if not self.enabled:
            return {}
        try:
            return self._store(entity).read()
        except aem_v2.AEMError:
            return {}

    # ── 判定輸入 ────────────────────────────────────────────────────────────
    def note_message(self, entity: str, text: str, *, speaker: str = "",
                     event_id: str = "", occurred_at: float | None = None,
                     source: str = "") -> dict:
        """記一則「有人對她說話」的 message（appraisal 的來源）。

        `source`（2026-11-01 owner 定）：這句話從哪個入口進來的（`app`／`discord`／`trpg`…）。
        **30 秒內、非同源、同一句話 ⇒ 算一件**——兩個入口同時收到同一句時只留一條 receipt，
        核心對同一個 `event_id` 會回 `DUPLICATE`，所以這裡直接沿用第一條的 id，不再 apply。
        理由：雙記會讓 valence／arousal 翻倍，而且**入口分布會把尺帶歪**（手機常斷、Discord 常開，
        偏移來自「用哪個端」而不是「他說了什麼」）。同源重複**不併**——他真有可能講兩次。
        """
        if not self.enabled:
            return {"ok": False, "error": "AEM_DISABLED"}
        if (speaker or "").strip().lower() in SELF_SPEAKERS:
            self._log("affect: echo 防護（speaker 是她自己）")
            return {"ok": False, "error": "ECHO_SELF_SPEAKER"}
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "EMPTY_MESSAGE"}
        source_key = _source_key(source, self.device)
        fingerprint = _fingerprint(body)
        merged = self._dedupe.hit(entity, fingerprint, source_key)
        if merged is not None:
            self._log(f"affect: 跨源同句併為一件（{merged['source']} → {source_key}，"
                      f"{merged['age']:.1f}s）")
            return {"ok": True, "event_id": merged["event_id"], "deduped": True,
                    "deduped_from": merged["source"], "source": source_key,
                    "text": body}
        event = {
            "event_id": event_id or "message:" + hashlib.sha1(
                f"{self._clock()}-{body}".encode()).hexdigest()[:16],
            "kind": "message",
            "occurred_at": self._clock() if occurred_at is None else float(occurred_at),
            "len": len(body),
            "text": body,
        }
        try:
            self.ensure(entity)
            self._store(entity).apply(event, self._clock())
        except aem_v2.AEMError as exc:
            self._log(f"affect: message 被拒 {exc.code}")
            return {"ok": False, "error": exc.code}
        self.touch(entity)
        self._dedupe.note(entity, fingerprint, source_key, event["event_id"])
        # 主人明確拒絕／叫停 ⇒ 負增量（owner 2026-10-24：量表要能往下）
        mark = next((word for word in REJECTION_MARKS if word in body), "")
        if mark:
            self._log(f"affect: 被拒絕（{mark}）⇒ 負增量")
            self._appraise_rule(entity, "rejected", event["event_id"])
        return {"ok": True, "event_id": event["event_id"]}

    def _appraise_rule(self, entity: str, rule: str, source_event_id: str) -> dict:
        """事件型情緒（被打斷／被拒絕／被無視）——固定增量，可測、好調。"""
        delta_v, delta_a = EVENT_AFFECT.get(rule, (0.0, 0.0))
        if not delta_v and not delta_a:
            return {"ok": False, "error": "UNKNOWN_RULE"}
        return self.appraise(entity, kind=str(rule), delta_v=delta_v, delta_a=delta_a,
                             confidence=0.7, source_event_id=source_event_id,
                             target_type="owner", target_id="owner")

    def interrupted(self, entity: str, *, source_event_id: str = "") -> dict:
        """被打斷（主人插話）⇒ 有點不高興、被喚起（owner 2026-10-24）。"""
        if not self.enabled:
            return {"ok": False, "error": "AEM_DISABLED"}
        # 稽核第 1 組 D：以前用「（打斷）」message 當載體 ⇒ 核心把它算成**主人回來了**
        # （更新 last_user_message_at、以 owner_returned 關掉缺席）。打斷不是主人說話 ⇒ 走 outcome。
        event_id = source_event_id or f"barge:{self._clock():.3f}"
        return self.outcome(entity, kind="frustration", delta_v=-0.12, delta_a=0.18,
                            event_id=event_id, target_type="conversation", target_id="owner",
                            confidence=0.7, dampen=False)

    def appraise(self, entity: str, *, kind: str, delta_v: float, delta_a: float,
                 confidence: float = 0.6, source_event_id: str, target_type: str = "owner",
                 target_id: str = "owner", occurred_at: float | None = None,
                 dampen: bool = False) -> dict:
        """把一次判定寫成一筆 episode（`message` 必須先存在）。`dampen`：同種同幅 3 次後折半（P1）。"""
        if not self.enabled:
            return {"ok": False, "error": "AEM_DISABLED"}
        if dampen:
            delta_v, delta_a = self._dampen(entity, kind, delta_v, delta_a)
        # 稽核第 3 組：核心的 `con = 2·dv/conf` 把信心約掉（只留在分母稀釋）⇒ 這裡先乘一次，
        # 低信心的推力才真的變小（實測兩筆 dv=0.4、信心 1.0／0.3 由 0.142 → 0.092）。
        delta_v, delta_a = repair.scale_delta(delta_v, delta_a, confidence)
        event = {
            "event_id": "ap:" + hashlib.sha1(
                f"{source_event_id}-{kind}".encode()).hexdigest()[:18],
            "kind": "appraisal",
            "occurred_at": self._clock() if occurred_at is None else float(occurred_at),
            "source_event_id": str(source_event_id),
            "episode_kind": str(kind) if kind in
            {"pleasure", "interest", "concern", "frustration", "uncertainty"} else "unclassified",
            "target": {"type": str(target_type), "id": str(target_id)},
            "delta_v": max(-1.0, min(1.0, float(delta_v))),
            "delta_a": max(0.0, min(1.0, float(delta_a))),
            "confidence": max(0.0, min(1.0, float(confidence))),
            # ⚠ 核心的 appraisal 白名單**沒有** `source` 這個欄位（用了會 INVALID_INPUT）；
            # 「這是誰判的」寫在 `model`（我們的尺第一版＝rule-v1，之後可換 judge）
            "model": "rule-v1",
            "control": "owner",
            "contract_version": "appraisal-v2",
            "context_ref": None,
        }
        try:
            self.ensure(entity)
            store = self._store(entity)
            store.apply(event, self._clock())
            # 核心的 `read()` 只回**已發布**的版本：寫入後要 advance 一次才看得到新數值
            store.advance(self._clock())
        except aem_v2.AEMError as exc:
            # 跨源同句併為一件時，第二個入口會拿第一條的 `event_id` 再判一次。
            # 底下三條都是**規則生效的正常結果**，不是錯誤（2026-11-01）：
            #   · `DUPLICATE`／`EVENT_ID_CONFLICT`——appraisal 的 id 是
            #     `ap:<sha1(source_event_id-kind)>`，同一條 source 再判一次必然撞號
            #     （`EVENT_ID_CONFLICT` 是因為兩次的 `occurred_at` 差幾秒、payload 不同）
            #   · `JUDGE_ALREADY_FINAL`——若 id 沒撞，核心仍會擋「同一條 message 二次結案」
            if exc.code in {"DUPLICATE", "EVENT_ID_CONFLICT", "JUDGE_ALREADY_FINAL"}:
                return {"ok": True, "duplicate": True, "code": exc.code,
                        "event_id": str(source_event_id)}
            self._log(f"affect: appraisal 被拒 {exc.code}")
            return {"ok": False, "error": exc.code}
        notify(self.on_change, entity, self._log)
        return {"ok": True, "event_id": event["event_id"]}

    def outcome(self, entity: str, *, kind: str, delta_v: float, delta_a: float, event_id: str,
                target_type: str = "task", target_id: str = "self", confidence: float = 0.6,
                dampen: bool = True) -> dict:
        """她自己這邊的事（活動／公共區反饋／等回音）＝核心的 `outcome`：不掛 message、不動 absence 計時。
        以前走 `appraise()` 一律 `SOURCE_NOT_FOUND`（她的活動從沒進過心情）。target_type∈owner/task/conversation。"""
        if not self.enabled:
            return {"ok": False, "error": "AEM_DISABLED"}
        if dampen:
            delta_v, delta_a = self._dampen(entity, kind, delta_v, delta_a)
        delta_v, delta_a = repair.scale_delta(delta_v, delta_a, confidence)
        event = {"event_id": "oc:" + hashlib.sha1(f"{event_id}-{kind}".encode()).hexdigest()[:18],
                 "kind": "outcome", "occurred_at": self._clock(), "model": "rule-v1",
                 "episode_kind": kind if kind in aem_v2.KINDS else "unclassified",
                 "target": {"type": str(target_type), "id": _slug(target_id, "self")},
                 "delta_v": max(-1.0, min(1.0, float(delta_v))),
                 "delta_a": max(0.0, min(1.0, float(delta_a))),
                 "confidence": max(0.0, min(1.0, float(confidence))),
                 "control": "owner", "contract_version": "outcome-v1"}
        try:
            self.ensure(entity)
            self._store(entity).apply(event, self._clock())
            self._store(entity).advance(self._clock())
        except aem_v2.AEMError as exc:
            if exc.code in {"DUPLICATE", "EVENT_ID_CONFLICT"}:
                return {"ok": True, "duplicate": True}
            self._log(f"affect: outcome 被拒 {exc.code}")
            return {"ok": False, "error": exc.code}
        notify(self.on_change, entity, self._log)
        return {"ok": True, "event_id": event["event_id"]}

    def _dampen(self, entity: str, kind: str, delta_v: float, delta_a: float):
        """連續同種類＋同幅度 ⇒ 這次的增量折半（owner 2026-10-24）。

        為什麼：實測 24 小時 126 次評分裡 98 次是 `interest +0.18/+0.28`——同一個推力
        每輪重複加，valence 就被一路墊在 0.3~0.5，標籤永遠是「ごきげん」。
        這裡只降權、不歸零：真的連續 3 次「有點興趣」也該有一點點，但不該主導整條曲線。
        """
        key = (str(kind), round(float(delta_v), 2), round(float(delta_a), 2))
        slug = _slug(entity)
        with self._lock:
            history = self._recent.setdefault(slug, [])
            repeats = sum(1 for item in history[-4:] if item == key)
            history.append(key)
            del history[:-8]
        if repeats >= REPEAT_DAMPEN_AFTER:
            return float(delta_v) * 0.5, float(delta_a) * 0.5
        return float(delta_v), float(delta_a)

    # ── SPIN 狀態 → AEM（P2）────────────────────────────────────────────────
    def note_state(self, entity: str, state: dict, *, source_event_id: str,
                   target_type: str = "owner", target_id: str = "owner",
                   occurred_at: float | None = None) -> dict:
        """把 SPIN 第 ② 段引出的狀態換算成 AEM 的 delta（owner 2026-10-22 的 P2）。

        為什麼要換算而不是另存一套心情：她只有**一個**心情。狀態層（SPIN）負責
        「這個場面她怎麼讀」，AEM 負責「這個讀法把她推到哪裡」——兩層用同一條曲線，
        才不會出現「日誌說她緊張、語氣卻是平的」這種互相打架。

        換算（刻意簡單、可回退）：
          · valence(-1..1) → delta_v（乘 intensity ⇒ 淡淡的感覺推得少）
          · intensity(0..1) → delta_a（被喚起的程度）
          · label → episode_kind（開心/興趣/擔心/挫折/不確定 對得上的就用，其他 unclassified）
        """
        if not self.enabled:
            return {"ok": False, "error": "AEM_DISABLED"}
        body = state or {}
        emotion = body.get("emotion") or {}
        try:
            valence = float(emotion.get("valence") or 0.0)
            intensity = float(emotion.get("intensity") or 0.0)
        except (TypeError, ValueError):
            valence, intensity = 0.0, 0.0
        label = str(emotion.get("label") or "")
        kind = "unclassified"
        for key, words in (
            ("pleasure", ("嬉", "楽", "喜", "安心", "満足", "happy", "glad")),
            ("interest", ("興味", "わくわく", "好奇", "楽しみ", "気になる", "curious")),
            ("concern", ("不安", "心配", "怖", "寂", "悲", "worry", "sad", "fear")),
            ("frustration", ("怒", "いら", "むか", "不満", "退屈", "angry", "annoy")),
            ("uncertainty", ("戸惑", "困惑", "迷", "わから", "confus", "unsure")),
        ):
            if any(word in label for word in words):
                kind = key
                break
        # ── 2026-10-24 修正（主人：「她好像一直很開心」）───────────────
        # ① 負面種類一定要把 valence 拉下來：以前是 valence×intensity，模型只要給正數
        #    就永遠往上（實測 126 次評分只有 3% 是負的、frustration 0 次）。
        # ② arousal 以前是 `max(0, intensity) * 0.8` ⇒ **結構上不可能下降**；
        #    改成 intensity×0.8 − 0.15（強度低＝平靜下來，會是負的）。
        # ③ 連續同種類同幅度 ⇒ 折半（防止「每輪都是 interest +0.18」把基線一路墊高）。
        if kind in NEGATIVE_KINDS:
            delta_v = -max(0.06, abs(valence) * max(0.2, intensity))
        else:
            delta_v = valence * max(0.2, intensity)
        # ⚠ AEM 的 schema 規定 `delta_a ∈ [0,1]`（負值會被判 INVALID_INPUT）——
        # 「平靜下來」只能靠**衰減**，不能靠負增量。所以低強度就給 0（不上推），
        # 再把 arousal 的半衰期從 120 分鐘縮到 60 分鐘（見 DEFAULT_POLICY）。
        delta_a = max(0.0, min(0.8, intensity * 0.8 - 0.15))
        delta_v, delta_a = self._dampen(entity, kind, delta_v, delta_a)
        payload = dict(kind=kind,
                       delta_v=round(delta_v, 3),
                       delta_a=round(delta_a, 3),
                       confidence=round(max(0.3, min(1.0, intensity)), 3),
                       target_type=target_type, target_id=target_id,
                       occurred_at=occurred_at)
        result = self.appraise(entity, source_event_id=str(source_event_id), **payload)
        # 狀態階段跑在訊息事件之前（管線內），所以 `SOURCE_NOT_FOUND` 是常態：
        # 自己補一筆 message 事件當載體，再判一次。心情是連續的，這裡不能默默丟掉。
        if not result.get("ok") and result.get("error") == "SOURCE_NOT_FOUND":
            # 稽核第 1 組 D：管線順序讓狀態階段沒有 message 載體。以前偽造「（状態：…）」message，
            # 那會被核心算成主人說話（缺席計時歸零、message_count 虛增）⇒ 改走 outcome。
            result = self.outcome(entity, kind=kind, delta_v=round(delta_v, 3),
                                  delta_a=round(delta_a, 3), event_id=f"state:{source_event_id}",
                                  target_type=target_type, target_id=target_id,
                                  confidence=float(payload["confidence"]), dampen=False)
        return result

    # ── 給心跳／語氣用的讀數 ────────────────────────────────────────────────
    def touch(self, entity: str, *, at: float | None = None) -> None:
        """有人跟她說話了（獨處計時重置）。"""
        with self._lock:
            self._last_touch[_slug(entity)] = float(at if at is not None else self._clock())
        notify(self.on_change, entity, self._log)

    def idle_s(self, entity: str, *, at: float | None = None) -> float:
        """多久沒跟她說話；以持久化的 `facts.last_user_message_at` 為準（稽核第 5 組 G）。"""
        return repair.idle_s(self, entity, at=at)

    def mood(self, entity: str, *, at: float | None = None) -> dict:
        """`{valence, arousal, social_need, label, propensity}`——心跳與 UI 都吃這個。"""
        now = float(at if at is not None else self._clock())
        # 閉式投影：任何時刻都重算一次（**不寫檔**）。
        # `read()` 只回 view（沒有 policy/episodes），要 `show()` 才是完整 state
        try:
            state = self._store(entity).show()
            view = aem_v2.project(state, now, time_scale=self.time_scale(entity)
                                  if self.time_scale is not None else None)
        except Exception:                     # noqa: BLE001 - 沒有狀態就當「平常」
            view = {}
        affect = view.get("affect") or {}
        valence = float(affect.get("valence") or 0.0)
        arousal = float(affect.get("arousal") or 0.0)
        absence = self.policy.get("absence") or {}
        after = float(absence.get("after_s") or 5400)
        idle = self.idle_s(entity, at=now)
        social_need = 0.0 if idle <= 0 else min(1.0, max(0.0, (idle - after) / max(1.0, after * 2)))
        # 想說話的程度。**這是我們的「尺」**（contract 四格之一：尺要自己長）：
        #   · 0.15 的底：人本來就會想分享一點東西，不是只有情緒高才有話
        #   · 心情（valence）權重最大：心情好＝想講給人聽
        #   · 被喚起（arousal）：興奮／不安都會想開口
        #   · 寂寞（social_need）：久沒人理會主動找人
        #   · 過度喚起反而收一點（不是越嗨越吵）
        propensity = (0.15 + 1.2 * max(0.0, valence) + 0.8 * arousal + 0.9 * social_need
                      - 0.35 * max(0.0, arousal - 0.75))
        return {
            "entity": str(entity), "valence": round(valence, 3),
            "arousal": round(arousal, 3), "social_need": round(social_need, 3),
            "idle_s": round(idle, 1),
            "propensity": round(max(0.0, min(1.0, propensity)), 3),
            "label": _label(valence, arousal),
            # 端末翻譯用的鍵（App 的立繪心情徽章；label 是給 prompt 的日語，不拿來當鍵）
            "key": LABEL_KEYS.get(_label(valence, arousal), "calm"),
            "revision": int(view.get("revision") or 0),
        }

    # ── 「她現在什麼心情、為什麼」（owner 2026-10-24）──────────────────────────
    def timeline(self, entity: str, *, limit: int = 20,
                 since: float | None = None) -> list[dict]:
        """最近的「什麼事讓她心情怎麼動」——只讀，不改狀態。

        為什麼要有：`mood()` 只給一個點（valence/arousal），看不出「為什麼」，
        也看不出「她是不是一直很開心」。這支把 AEM 的 episode（每次評分的增量與原因）
        攤開：時間、種類、valence 增量、arousal 增量、對象。
        """
        if not self.enabled:
            return []
        try:
            state = self._store(entity).show()
        except Exception:                                   # noqa: BLE001
            return []
        rows: list[dict] = []
        for episode in state.get("episodes") or []:
            affect = episode.get("affect") or {}
            when = float(episode.get("occurred_at") or 0.0)
            if since is not None and when < float(since):
                continue
            target = episode.get("target")
            if isinstance(target, dict):
                target = str(target.get("id") or target.get("type") or "")
            rows.append({
                "at": when,
                "kind": str(episode.get("kind") or ""),
                "target": str(target or ""),
                "delta_valence": round(float(affect.get("delta_valence") or 0.0), 3),
                "delta_arousal": round(float(affect.get("delta_arousal") or 0.0), 3),
                "expires_at": float(affect.get("expires_at") or 0.0),
            })
        rows.sort(key=lambda row: row["at"], reverse=True)
        return rows[:max(1, int(limit))]

    def cause(self, entity: str, *, at: float | None = None) -> dict:
        """此刻心情的主要原因（管理台用）；實作在 `inner/mood_cause.py`。"""
        from agent.brain.runtime.inner import mood_cause
        return mood_cause.main_cause(self, entity, now=at)

    def annotated_timeline(self, entity: str, *, limit: int = 20, since: float | None = None) -> list[dict]:
        """`timeline()` ＋ 每筆的原因與「看不看得見」；實作在 `inner/mood_cause.py`。"""
        from agent.brain.runtime.inner import mood_cause
        return mood_cause.annotate(self, entity, limit=limit, since=since)

    def series(self, entity: str, *, hours: float = 24.0, step: float = 300.0,
               at: float | None = None) -> list[dict]:
        """歷史曲線（任意時刻的關閉式投影）；實作在 `inner/mood_series.py`（本檔已到 600 行上限）。"""
        from agent.brain.runtime.inner import mood_series
        return mood_series.series(self, entity, hours=hours, step=step, at=at)

    def stats(self, entity: str, *, hours: float = 24.0) -> dict:
        """近 N 小時的評分統計；實作在 `inner/mood_series.py`。"""
        from agent.brain.runtime.inner import mood_series
        return mood_series.stats(self, entity, hours=hours)


#: 心情標籤 → 端末的翻譯鍵（`next_*.json` 的 `mood_<key>`）
LABEL_KEYS = {"わくわく": "excited", "いらいら": "irritated", "ごきげん": "happy",
              "しょんぼり": "down", "ふつう": "calm"}


def _label(valence: float, arousal: float) -> str:
    """門檻按**實測分布**標定（owner 2026-11-09 AEM 稽核第 2 組）。

    舊門檻（0.3／−0.25／arousal 0.55）是按滿量程設的，而實測 24h 內 |valence| 從沒到 0.3、
    arousal 從沒到 0.55 ⇒ 三個角色整天都是「ふつう」，四個標籤等於不存在。
    這裡取重放分布的 p75 附近（q0=3）：安靜的日子 ≈89% ふつう＋少量 ごきげん，忙的日子一半一半。
    """
    if arousal >= 0.12 and valence >= 0.06:
        return "わくわく"
    if arousal >= 0.12 and valence <= -0.04:
        return "いらいら"
    if valence >= 0.07:
        return "ごきげん"
    if valence <= -0.04:
        return "しょんぼり"
    return "ふつう"


__all__ = ["AffectService", "DEFAULT_POLICY", "LABEL_KEYS", "SELF_SPEAKERS", "mood_block"]
