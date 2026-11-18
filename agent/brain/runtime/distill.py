"""背景提煉：session 過濾 → LLM 抽事實 → 按門檻入檔 → **每次判定都留履歷**。

owner 2026-10-23 定的三條：
  1. 用 **DeepSeek** 提煉（模型名走 `BLUEAGENT_LLM_*`，與大腦同一組 env）；
  2. **按門檻入檔**（預設 0.6，`BLUEAGENT_DISTILL_MIN_CONF`）；
  3. **入檔履歷可查**（`facts_history` ＋ 每次跑一份 `data/memory/distill/<run_id>.json`）。

為什麼要它：寫入側的規則式抽取只認「記住／我喜歡／希望被叫」這幾種句式，
普通敘述（「我在讀《佐々木とピーちゃん》，讀到第六卷」）一條都落不下來 ⇒ 她一出滾動窗就失憶。

流程（一次 `run_once`）：

    raw/<entity>/*.jsonl
      → SessionBuilder（同 thread、間隔 ≤ 30 min、至少 2 輪）
      → RuleFilter（全是寒暄/太短 ⇒ drop，仍留在 raw）
      → 已提煉過的 session 跳過（state.json 水位）
      → LLM（一次一個 session，JSON 出候選事實）
      → 門檻過濾（conf ≥ min_conf）＋ 去重（正規化全等）
      → memory.add(source="distill") ＋ facts_history 記一筆
      → data/memory/distill/<run_id>.json 存整輪帳

失敗語義：LLM 掛了／JSON 壞了 ⇒ 該 session **不記已提煉**（下輪重試），不寫半條記憶。
"""

from __future__ import annotations


from agent.brain.runtime.memory.window_repeat import REPEAT_LIMIT, repetition_reason
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import urllib.request

from agent.brain.memory.facts import grams, normalize_text
from agent.brain.runtime.memory.distill_support import (
    ALLOWED_LAYERS,
    EVENT_TTL_DAYS,
    GAME_HEADER,
    GAME_PROMPT_RULE,
    apply_candidates,
    event_valid_to,
    slot_hint,
    window_game,
    window_signature,
    with_slot_hint,
)
from agent.brain.runtime.memory.distill_known import known_hint, related_known
from agent.brain.runtime.memory.pointer import make as make_pointer

#: session 切分：同一條線裡隔超過這麼久就算新的一段。
SESSION_GAP_S = float(os.environ.get("BLUEAGENT_DISTILL_GAP_S") or 30 * 60)
#: **提煉窗口**的最小／最大輪數（owner 2026-10-23：「提煉要有範圍窗口，不是搜出來的一句話」）。
#: 小於下限的段跟前後合併，大於上限的切開——餵給模型的永遠是一段**有上下文的連續對話**。
MIN_TURNS = int(os.environ.get("BLUEAGENT_DISTILL_MIN_TURNS") or 8)
MAX_TURNS = int(os.environ.get("BLUEAGENT_DISTILL_MAX_TURNS") or 60)
MIN_CHARS = int(os.environ.get("BLUEAGENT_DISTILL_MIN_CHARS") or 12)
#: 同一句在一窗裡重複到這個次數 ⇒ 判為「復読／空転」，不提煉（owner 2026-11-03）。
REPEAT_LIMIT = int(os.environ.get("BLUEAGENT_DISTILL_REPEAT_LIMIT") or 4)
#: 入檔門檻（owner 2026-10-23：按閾值入檔）。
MIN_CONFIDENCE = float(os.environ.get("BLUEAGENT_DISTILL_MIN_CONF") or 0.6)
#: 一輪最多處理幾個 session（成本上限）。
MAX_SESSIONS = int(os.environ.get("BLUEAGENT_DISTILL_MAX_SESSIONS") or 5)

# 表結構住在 window_store（唯一讀寫入口）；這個名字留給舊 import
from agent.brain.runtime.memory.window_store import SCHEMA as _SUMMARY_SCHEMA  # noqa: E402
from agent.brain.runtime.memory.fact_history import FactHistory, memory_db_path  # noqa: E402
from agent.brain.memory.fact_rules import ADDRESS_SLOT, canonical_address  # noqa: E402


#: 提煉指令（**日語**，review 2026-10-28：以前整段繁中 ⇒ 提煉出來的摘要／事實也跟著寫成中文，
#: 再回流進她的 prompt）。JSON 欄位名、layer／kind 列舉值、「主人」這個主語寫法都**不變**。
PROMPT = """あなたは記憶の抽出係。以下は**連続した会話のウィンドウ**（同じスレッドのある時間帯。日本語と中国語が混ざることもある）。

次の二つをやること。**順番が大事**：

① まず一、二文で「**この時間帯に何を話したか**」（ウィンドウ要約）：話題、誰と誰か、どこまで話したか、宙に浮いたままの件はあるか。
   要約は具体的に書く（例：「主人は『佐々木とピーちゃん』を六巻まで読んでいて、ネタバレなしで話し、今夜いっしょにキャラシートを作る話も出た」）。
   「いろいろ話した」のような中身のない書き方はしない。

② 次に**ウィンドウ全体**から長く覚えておく価値のある安定した情報（事実）を選ぶ。基準は「**ウィンドウ全体がそれを支えているか**」：
   - ウィンドウの中で繰り返し出てくる／はっきり成り立っていて、長く有効なものだけを取る；
   - 一文だけから事実を抜き出さない。一度きりの情報・推測・あいさつは取らない；
   - 事実はこの会話を離れても読めるように、主語を書く。主人のことは必ず「主人」と書き、owner とは書かない。

❌ 絶対に取らないもの：作業や開発の指示（デプロイ／リリース／再起動／コンテナ／ポート／API／SSE／キュー／設定変更／リポジトリへの push／commit／テスト／
「X を Y に変える」）、質問や未確定のこと（「彼女に…か聞く」「…するかどうか」）、その場かぎりの操作（今ちょっと動かす、まず試す）。

layer は次のどれかだけ：pair（彼女とある一人の二人の記憶）／circle（彼女と何人か）／consensus（何人かで決めたこと）／private（秘密）。
""" + GAME_PROMPT_RULE + """

**JSON だけ**を返す：
{"window":{"topic":"…","summary":"…","people":["…"],"open_loops":["…"]},
 "candidates":[{"text":"…","layer":"pair","confidence":0.0~1.0,"evidence":["turn_id",…],
                "kind":"preference|state|event|rule","valid_days":0,"slot":"…","supersedes":["…"]}]}

`kind` の基準：好み（好き／嫌い、長く有効）＝preference；新しい値に置き換わる状態（いま読んでいる本、
いまの気分）＝state；ある時に起きること（今夜いっしょにキャラシートの準備、明日どこかへ行く）＝event；約束／決まりごと＝rule。
`valid_days`：event のときだけ「何日後に無効になるか」を書く（日数、0＝無効にならない）。
`slot`：state のときだけ「どの枠の現在値か」を書く（例：主人がいま読んでいる本／主人のいまの気分／
彼女が最近忙しくしていること）。同じ slot は**いちばん新しい時点の一件だけ**残し、古いものは置き換え済みにする（さかのぼれる）。
`supersedes`：この新しい事実が**前に書いた事実を置き換える**ときだけ、置き換える相手の文をそのまま（一字一句）書く。
例：前に「主人は甘いものが苦手」とあり、今「最近は甘いものも食べられるようになった」⇒ `["主人は甘いものが苦手"]`。
置き換えないなら空配列。推測で古い事実を消さない（言い換え・言い直しは置き換えではない）。

会話ウィンドウ：
"""

#: 任務／工程指令的味道（規則層先擋一次，別全靠模型自律）。
#: 系統提示／桌端叫醒這類文本不是記憶（曾被提煉成 state 存進庫）
SYSTEM_NOISE = ("桌端叫醒",                             # lang-ok: 比對用的詞表，不進 prompt
                "【TRPG 桌", "輪到你】席位", "轮到你】席位", "叫醒你：",
                "system prompt", "システムプロンプト", "あなたは", "指示に従")
TASK_HINTS = (                                               # lang-ok: 比對用的詞表
    "部署", "上線", "上线", "重啟", "重启", "隊列", "队列", "端口", "容器", "鏡像", "镜像",
    "commit", "repo", "推庫", "推库", "API", "SSE", "token", "顯存", "显存", "改成", "改為",
    "改为", "做成", "接入", "接到", "跑一次", "測試", "测试", "配置", "環境變數", "環境変数",
)
#: 疑問／未定（問題不是事實）。
QUESTION_HINTS = (                                           # lang-ok: 比對用的詞表
    "詢問", "询问", "問い合わせ", "是否", "要不要", "かどうか", "嗎？", "吗？", "？", "?")

#: 近重複門檻（同 entity 內，二字重疊 ≥ 這個比例就當同一條）。
NEAR_DUP_RATIO = 0.8


class Distiller:
    def __init__(self, *, memory, raw_log, entities=(), min_conf: float = MIN_CONFIDENCE,
                 max_sessions: int = MAX_SESSIONS, clock=time.time,
                 base_url: str = "", model: str = "", api_key: str = "",
                 timeout: float = 60.0, transport=None, state_path=None,
                 run_dir=None, names=None, vectors=None) -> None:
        self.memory = memory
        #: 向量倉（挑「已經記得的相關事實」給模型看用；`memory/distill_known.py`）。None＝退回字面
        self.vectors = vectors
        self.raw_log = raw_log
        self.entities = [str(item).strip() for item in entities if str(item).strip()]
        self.min_conf = float(min_conf)
        self.max_sessions = max(1, int(max_sessions))
        self._clock = clock
        from agent.brain.config import llm as llm_config
        settings = llm_config.settings_for("distill")
        self.base_url = (base_url or settings["base_url"]).rstrip("/")
        self.model = (model or settings["model"]).strip()
        self.api_key = (api_key or settings["api_key"]).strip()
        self.temperature = float(settings.get("temperature") or 0.2)
        self.max_tokens = int(settings.get("max_tokens") or 800)
        self.timeout = float(timeout)
        self._transport = transport or self._post
        root = pathlib.Path(getattr(raw_log, "root", "."))
        self.state_path = pathlib.Path(state_path or (root.parent / "memory" / "distill" / "state.json"))
        self.run_dir = pathlib.Path(run_dir or (root.parent / "memory" / "distill"))
        #: 實體 id → 顯示名（把 `mira` 寫成「ミラ」；owner 一律「主人」）
        self.names = {str(k): str(v) for k, v in (names or {}).items()}
        self._lock = threading.RLock()
        self.last_result: dict = {}
        #: 本輪已寫入的事實（跨窗口去重用）
        self._inserted: dict[str, list[str]] = {}

    # ── 窗口切分（owner 2026-10-23：提煉的最小單位是「窗口」，不是一句話）────────
    def windows(self, entity: str) -> list[dict]:
        """把 raw 切成**連續窗口**：同 thread、間隔 ≤ `SESSION_GAP_S` 為一段，
        小於 `MIN_TURNS` 的段併進相鄰段（同一條線），超過 `MAX_TURNS` 的切開。

        為什麼要併：兩三輪的碎段拿去做提煉，等於在抽「單句」，抽出來的東西沒有上下文、
        而且同一件事會被前後兩段各抽一次（實測首輪就是這樣出現重複）。
        """
        rows = sorted(self.raw_log._iter_rows(entity), key=lambda row: float(row.get("at") or 0))
        segments: list[list[dict]] = []
        current: list[dict] = []
        for row in rows:
            if current and (float(row.get("at") or 0) - float(current[-1].get("at") or 0) > SESSION_GAP_S
                            or str(row.get("thread")) != str(current[-1].get("thread"))):
                segments.append(current)
                current = []
            current.append(row)
        if current:
            segments.append(current)

        merged: list[list[dict]] = []
        for segment in segments:
            if merged and (len(segment) < MIN_TURNS
                           and str(merged[-1][0].get("thread")) == str(segment[0].get("thread"))):
                merged[-1].extend(segment)
            else:
                merged.append(list(segment))

        windows: list[dict] = []
        for segment in merged:
            if len(segment) < MIN_TURNS:
                continue                       # 整條線本來就沒幾輪 ⇒ 這輪不提煉
            step = max(MIN_TURNS, MAX_TURNS)
            for offset in range(0, len(segment), step):
                chunk = segment[offset:offset + step]
                if len(chunk) < MIN_TURNS:
                    continue
                windows.append(self._wrap(entity, chunk))
        return windows

    def sessions(self, entity: str) -> list[dict]:
        """舊名相容（`tools/distill_now.py --dry-run` 用）。"""
        return self.windows(entity)

    @staticmethod
    def _wrap(entity: str, rows: list[dict]) -> dict:
        room = sorted({str(who) for row in rows for who in (row.get("room") or ())})
        return {"key": f"{entity}:{rows[0].get('thread')}:{rows[0].get('turn')}~{rows[-1].get('turn')}",
                # 場景維度（2026-11-11 B3）：這一窗是不是桌（虛構）——判定在 `distill_support`
                "game": window_game(rows),
                "entity": entity, "thread": rows[0].get("thread"),
                "started_at": float(rows[0].get("at") or 0),
                "ended_at": float(rows[-1].get("at") or 0),
                "turns": len(rows), "room": room, "rows": rows,
                "participants": [who for who in room if who not in {entity}]}

    # ── 規則過濾 ────────────────────────────────────────────────────────────
    @staticmethod
    def keep_by_rules(session: dict) -> tuple[bool, str]:
        texts = [str(row.get("text") or "").strip() for row in session["rows"]]
        if len(texts) < MIN_TURNS:
            return False, "window_too_small"
        if max((len(text) for text in texts), default=0) < MIN_CHARS:
            return False, "too_short"
        # 復読／空轉窗口（owner 2026-11-03）：判定在 `runtime/memory/window_repeat.py`
        reason = repetition_reason(texts, limit=REPEAT_LIMIT)
        if reason:
            return False, reason
        return True, ""

    # ── LLM ────────────────────────────────────────────────────────────────
    def _post(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature, "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }).encode()
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:   # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        return str((payload.get("choices") or [{}])[0].get("message", {}).get("content") or "")

    def _render(self, session: dict) -> str:
        lines = []
        if int(session.get("game") or 0):
            lines.append(GAME_HEADER)      # 2026-11-11 B3：桌的窗口要明說「這是遊戲」
        for row in session["rows"]:
            lines.append(f"[{row.get('turn')}] {row.get('speaker') or row.get('role')}: {row.get('text')}")
        return "\n".join(lines)

    def _parse(self, raw: str) -> tuple[dict, list[dict]]:
        """模型輸出 → `(窗口摘要, 候選事實)`。壞了就回空，不硬猜。"""
        text = str(raw or "").strip()
        if not text:
            return {}, []
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}, []
        try:
            body = json.loads(match.group(0))
        except ValueError:
            return {}, []
        window = body.get("window") if isinstance(body.get("window"), dict) else {}
        items = body.get("candidates")
        return window, [item for item in (items or []) if isinstance(item, dict)]

    def _save_window(self, session: dict, window: dict, candidates: list[dict],
                     inserted: list[str], run_id: str) -> None:
        """把「這段時間講了什麼」落檔：`data/memory/windows/<entity>/<window_id>.json`。"""
        # ⚠️ 2026-11-01：空摘要不落庫。實測一天落了 3 條 `summary` 空、`topic` 空、
        # `turn_ids` 全 "None" 的窗口（`str(None)` 那種老坑），它們在檢索／承接裡
        # 等於一段「有紀錄但什麼都沒說」的記憶，比沒有更糟。
        if not str(window.get("summary") or "").strip():
            print(f"  distill: 窗口摘要空 ⇒ 不落庫（{session.get('key')}）", flush=True)
            return
        ids = [str(row.get("turn")) for row in session["rows"]]
        if not any(token and token not in {"None", "none", "null", ""} for token in ids):
            print(f"  distill: 窗口沒有任何原文門牌 ⇒ 不落庫（{session.get('key')}）", flush=True)
            return
        folder = self.run_dir.parent / "windows" / session["entity"]
        folder.mkdir(parents=True, exist_ok=True)
        name = str(session["key"]).replace(":", "_").replace("~", "-").replace("/", "_")
        record = {"window_id": name, "entity": session["entity"],
                  "thread": session["thread"],
                  "started_at": session["started_at"], "ended_at": session["ended_at"],
                  "turns": session["turns"], "room": session["room"],
                  "turn_ids": ids,
                  # 門牌：這一段摘要要能回到原文那幾行（沒有它就只是轉述，不是記憶）
                  "ptr": make_pointer(session["entity"],
                                      [str(row.get("turn")) for row in session["rows"]]),
                  "topic": str(window.get("topic") or ""),
                  "summary": str(window.get("summary") or ""),
                  "people": list(window.get("people") or []),
                  "open_loops": list(window.get("open_loops") or []),
                  "candidates": len(candidates), "inserted": inserted,
                  "run_id": run_id, "at": self._clock(),
                  # 場景維度（2026-11-11 B3）：1＝桌（虛構）；心跳／主動消息只讀 game=0 的窗口
                  "game": int(session.get("game") or 0)}
        path = folder / f"{name}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._index_window(record)

    def _index_window(self, record: dict) -> None:
        """窗口摘要進 SQLite（檢索與承接用；檔案仍是正本）。"""
        from agent.brain.runtime.memory.window_store import WindowStore
        WindowStore(self._db_path()).upsert(record)

    # ── 水位 ────────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ── 履歷 ────────────────────────────────────────────────────────────────
    def _db_path(self) -> str:
        return memory_db_path(self.memory)

    def _history(self, rows: list[dict]) -> None:
        FactHistory(self._db_path()).append(rows, model=self.model)


    # ── 收緊（owner 2026-10-23「收緊」）────────────────────────────────────
    def normalize_names(self, text: str) -> str:
        """`owner` → 主人；實體 id → 顯示名。同一件事不該有兩種寫法。"""
        out = str(text or "")
        out = re.sub(r"\bowner\b", "主人", out, flags=re.I)
        for ident, name in self.names.items():
            if not ident or ident == name:
                continue
            out = re.sub(rf"(?<![0-9A-Za-z]){re.escape(ident)}(?![0-9A-Za-z])", name, out)
        return out.strip()

    @staticmethod
    def noise_reason(text: str) -> str:
        """規則層擋噪音：任務／工程指令、疑問與未定。回空字串＝可以進下一步。"""
        body = str(text or "").strip()
        if not body:
            return "empty"
        if any(hint in body for hint in QUESTION_HINTS):
            return "question_not_fact"
        hits = [hint for hint in TASK_HINTS if hint in body]
        if hits:
            return "task_instruction"
        return ""

    def _near_duplicate(self, entity: str, text: str) -> str:
        """同一 entity 內有 ≥ NEAR_DUP_RATIO 重疊的舊事實就當重複。回那條舊事實。"""
        try:
            local = getattr(self.memory, "local", None) or self.memory
            rows = local.snapshot(None, entity=entity) if hasattr(local, "snapshot") else {}
        except Exception:                                   # noqa: BLE001
            return ""
        wanted = grams(text)
        if not wanted:
            return ""
        for row in rows.get("facts", []):
            other = grams(str(row.get("text") or ""))
            if not other:
                continue
            ratio = len(wanted & other) / max(1, len(wanted))
            if ratio >= NEAR_DUP_RATIO:
                return str(row.get("text") or "")
        return ""

    # ── 一輪 ────────────────────────────────────────────────────────────────
    def run_once(self, *, now: float | None = None,
                 only_keys: list[str] | None = None) -> dict:
        """`only_keys`＝只跑這些窗口（補資料／驗收用；不給就照常掃全部）。"""
        with self._lock:
            run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            self._inserted = {}
            state = self._load_state()
            summary = {"run_id": run_id, "model": self.model, "min_conf": self.min_conf,
                       "sessions_total": 0, "sessions_done": 0, "sessions_skipped": 0,
                       "facts_added": 0, "facts_skipped": 0, "errors": []}
            history: list[dict] = []
            decided: list[dict] = []
            for entity in (self.entities or [""]):
                if not entity:
                    continue
                for session in self.windows(entity):
                    if only_keys and session["key"] not in only_keys:
                        continue
                    summary["sessions_total"] += 1
                    if summary["sessions_done"] >= self.max_sessions:
                        break
                    signature = window_signature(session)
                    previous = state.get(session["key"]) or {}
                    # 指紋不同 ⇒ 這個窗口的內容變了（例如原文回填），必須重跑；
                    # 舊版只比 key，於是「回填前判過 too_short/done」的窗口永遠不會再被看一次。
                    if previous and previous.get("sig") == signature:
                        summary["sessions_skipped"] += 1
                        continue
                    keep, reason = self.keep_by_rules(session)
                    if not keep:
                        state[session["key"]] = {"at": session["ended_at"], "verdict": reason,
                                                 "sig": signature, "turns": session["turns"]}
                        summary["sessions_skipped"] += 1
                        continue
                    try:
                        game = int(session.get("game") or 0)
                        rendered = self._render(session)
                        # 槽位名＋已經記得的相關事實（同一件事別換說法再寫一次，B.Agent.3）
                        hint = slot_hint(self.memory, entity, game) + known_hint(related_known(
                            self.memory, entity, game, rendered, vectors=self.vectors))
                        raw = self._transport(with_slot_hint(PROMPT, hint) + rendered)
                        window_meta, candidates = self._parse(raw)
                    except Exception as exc:                 # noqa: BLE001
                        summary["errors"].append(f"{session['key']}: {type(exc).__name__}: {exc}")
                        continue                                  # 不記水位 ⇒ 下輪重試
                    summary["sessions_done"] += 1
                    touched = apply_candidates(self, session, candidates, entity=entity,
                                               run_id=run_id, history=history, summary=summary)
                    self._save_window(session, window_meta, candidates, touched, run_id)
                    state[session["key"]] = {"at": session["ended_at"], "verdict": "done",
                                              "sig": signature, "turns": session["turns"],
                                              "candidates": len(candidates),
                                              "topic": str(window_meta.get("topic") or "")}
                    decided.append({"window": session["key"], "turns": session["turns"],
                                    "topic": str(window_meta.get("topic") or ""),
                                    "summary": str(window_meta.get("summary") or ""),
                                    "candidates": len(candidates),
                                    "inserted": len(touched)})
            self._history(history)
            self._save_state(state)
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / f"{run_id}.json").write_text(
                json.dumps({**summary, "sessions": decided, "history": history},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            self.last_result = summary
            return summary

    def _near_inserted(self, entity: str, text: str) -> str:
        """同一輪裡前面窗口已經寫過的近似事實（避免兩個窗口各抽一次同一件事）。"""
        wanted = grams(text)
        if not wanted:
            return ""
        for prior in self._inserted.get(entity, []):
            other = grams(prior)
            if other and len(wanted & other) / max(1, len(wanted)) >= NEAR_DUP_RATIO:
                return prior
        return ""

    def _exists(self, entity: str, text: str) -> bool:
        key = normalize_text(text)
        try:
            local = getattr(self.memory, "local", None) or self.memory
            rows = local.snapshot(None, entity=entity) if hasattr(local, "snapshot") else {}
            for row in rows.get("facts", []):
                if normalize_text(str(row.get("text") or "")) == key:
                    return True
        except Exception:                                   # noqa: BLE001 - 查重失敗就當沒有
            return False
        return False

    def _resolve_supersedes(self, entity: str, texts, *, game: int = 0) -> list[str]:
        """模型說「這條取代了哪些舊事實」→ 門牌清單（認不出來的一律丟掉，不猜）。

        模型寫的是**舊事實的原文**，不是門牌 ⇒ 這裡回庫裡找：先比正規化全等，
        再退到二字重疊 ≥ `NEAR_DUP_RATIO`。兩步都找不到就當它沒說（
        寧可留一條舊的，也不要指錯——錯的指針會讓「以前是什麼」永遠查不到）。
        已經被別人取代的舊事實不再收（一條只認一個取代者）。
        """
        wanted = [str(item or "").strip() for item in (texts or ())]
        wanted = [item for item in wanted if item]
        if not wanted:
            return []
        try:
            local = getattr(self.memory, "local", None) or self.memory
            rows = local.snapshot(None, entity=entity) if hasattr(local, "snapshot") else {}
        except Exception:                                   # noqa: BLE001
            return []
        pool = [row for row in rows.get("facts", [])   # 只在**同場景**裡找：桌上的不取代現實的
                if row.get("key") and int(row.get("game") or 0) == int(game or 0)]
        keys: list[str] = []
        for text in wanted:
            norm = normalize_text(text)
            grams_of = grams(text)
            found = ""
            for row in pool:
                if norm and normalize_text(str(row.get("text") or "")) == norm:
                    found = str(row["key"])
                    break
            if not found and grams_of:
                best, ratio_best = "", 0.0
                for row in pool:
                    other = grams(str(row.get("text") or ""))
                    if not other or row.get("superseded_by"):
                        continue
                    ratio = len(grams_of & other) / max(1, len(grams_of))
                    if ratio >= NEAR_DUP_RATIO and ratio > ratio_best:
                        best, ratio_best = str(row["key"]), ratio
                found = best
            if found and found not in keys:
                keys.append(found)
        return keys


def start_worker(distiller: Distiller, *, interval_s: float = 600.0) -> threading.Thread:
    """背景執行緒：每 `interval_s` 跑一次 `run_once`（失敗只記在 last_result）。"""
    def loop() -> None:
        while True:
            time.sleep(max(30.0, float(interval_s)))
            try:
                distiller.run_once()
            except Exception as exc:                        # noqa: BLE001
                distiller.last_result = {"error": f"{type(exc).__name__}: {exc}"}
    thread = threading.Thread(target=loop, name="distill", daemon=True)
    thread.start()
    return thread
