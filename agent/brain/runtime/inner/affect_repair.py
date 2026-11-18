"""情緒層的「保養」與「換尺」——不改 vendored 核心，只在適配層動手（owner 2026-11-09 稽核）。

為什麼獨立一支：`affect.py` 已經頂到 600 行上限（`test_code_size`），而下面幾件事都是
**對既有 state 的修補**，跟「判定／投影」不是同一種職責：

  · `migrate_policy()`：把 state 裡存的 policy 換成程式的新值（核心的 `project()` 讀 state，
    不是讀常數 ⇒ 只改 `DEFAULT_POLICY` 對跑起來的角色完全沒用）。
  · `reproject()`：換核心後舊 view 對不上時的一次性重算（`show()` 會擋成 STATE_CORRUPT）。
  · `rearm_absence()`：缺席 episode 過 ttl 被剪掉後，核心的 `facts.absence_episode_id` 還指著它
    ⇒ 缺席永遠不會再觸發（稽核第 4 組 E）。這裡只清那個守衛變數。
  · `scale_delta()`：核心的 `con = 2·dv/conf` 會把信心約掉（信心只留在分母稀釋），
    所以送進去之前先乘一次，讓低信心的推力真的變小（稽核第 3 組 B）。
  · `idle_s()`：獨處時間以**持久化**的 `facts.last_user_message_at` 為準（進程內 `_last_touch`
    重啟就歸零，跟核心的缺席計時打架；稽核第 5 組 G）。

全部吃 `AffectService` 當第一個參數，不 import 它（避免循環）。
"""

from __future__ import annotations

import re

from agent.brain.vendor import aem_v2


def _slug(value, fallback: str = "unknown") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean or fallback


def scale_delta(delta_v: float, delta_a: float, confidence: float) -> tuple[float, float]:
    """信心 → 推力。核心只把信心當權重（分子裡被約掉），所以在**送進去之前**乘一次。

    實測（2026-11-09）：兩筆 dv=0.4（信心 1.0／0.3）合計 0.142 = 全按 1.0 算；
    乘過之後應為 0.092（＝兩筆各自 0.4×conf 的加權）。
    """
    conf = max(0.0, min(1.0, float(confidence)))
    return (max(-1.0, min(1.0, float(delta_v))) * conf,
            max(0.0, min(1.0, float(delta_a))) * conf)


def migrate_policy(affect, entity: str) -> None:
    """把既有 store 的政策換成新的（owner 2026-10-24）。

    AEM 的 `project()` 讀的是**存在 state 裡**的 policy，不是程式裡的預設值——
    所以只改 `DEFAULT_POLICY` 對已經跑起來的角色完全沒用（她的 `affect_ttl_h`
    會一直停在 48h）。這裡在第一次碰到時比對並就地換掉，revision +1、重算 sha。
    """
    slug = _slug(entity)
    with affect._lock:
        if slug in affect._migrated:
            return
        affect._migrated.add(slug)
    try:
        store = affect._store(entity)
        state = store.show()
    except Exception:                                   # noqa: BLE001
        return
    current = (state.get("policy") or {}).get("values") or {}
    wanted = aem_v2.validate_policy(affect.policy)
    if all(current.get(key) == value for key, value in wanted.items()):
        return
    state["policy"] = {
        "revision": int((state.get("policy") or {}).get("revision") or 1) + 1,
        "sha256": aem_v2.digest(wanted),
        "values": wanted,
    }
    # ⚠ `show()` 會驗「存下來的 view 等於現在重算的 view」⇒ 換政策之後一定要重算，
    # 不然下一次讀檔直接 STATE_CORRUPT。
    try:
        state["view"] = aem_v2.project(state, float(state.get("as_of") or affect._clock()))
        store.save(state)
        affect._log(f"affect: 政策已更新（{slug}）ttl={wanted.get('affect_ttl_h')}h")
    except Exception as exc:                            # noqa: BLE001
        affect._log(f"affect: 政策更新失敗（{type(exc).__name__}: {exc}）")


def reproject(affect, entity: str) -> dict:
    """換核心後的一次性重投影（owner 2026-11-01 升 1,215 行那顆）。

    新核心的 `project()` 多了 `affect.composition` ⇒ 舊 `state.json` 裡存的 `view`
    一定對不上，`show()` 會擋成 `STATE_CORRUPT`（那是自檢，不是檔案壞了）。
    這裡只**重算 view 並存回**：episodes／receipts／policy／revision 一個都不動。
    """
    store = affect._store(entity)
    raw = aem_v2.load_json(store.path)
    raw["view"] = aem_v2.project(raw, float(raw.get("as_of") or affect._clock()))
    store.save(raw)
    affect._log(f"affect: 重投影（{_slug(entity)}）——view 多了 composition")
    return store.read()


def rearm_absence(affect, entity: str) -> None:
    """缺席 episode 過期 ⇒ 就地「續期」，讓長期不在也留著牽掛（稽核第 4 組 E）。

    以前只靠「主人說話」清 `facts.absence_episode_id`：一次長假（>12h）之後，episode 被
    `age()` 剪掉、貢獻歸零，但 flag 還指著它 ⇒ 她再也不會「想他」，直到他回來為止。
    只清 flag 沒用：缺席 episode 的 id 是由**最後那條 message** 推出來的，清掉之後核心會用
    同一個 id 重建 ⇒ 撞號、什麼都不會發生（2026-11-09 實測）。所以改成**把那集的時間基準
    挪到現在**（occurred_at=now、expires_at=now+ttl）：事件沒變，只是「他還是不在」被重新確認。

    ⚠ 讀改寫全程在 `store.locked()` 裡：心跳與回話執行緒會同時跑 advance，
    沒有鎖的 read-then-save 會把別人的交易蓋掉（2026-11-09 全套測試實測）。
    """
    try:
        store = affect._store(entity)
        with store.locked():
            state = aem_v2.load_json(store.path)
            ep_id = (state.get("facts") or {}).get("absence_episode_id")
            if not ep_id:
                return
            now = float(affect._clock())
            target = next((ep for ep in state.get("episodes") or [] if ep.get("id") == ep_id), None)
            if target is not None and float((target.get("affect") or {}).get("expires_at") or 0.0) > now:
                return                                   # 還在有效期，不動
            if target is None:
                state["facts"]["absence_episode_id"] = None   # 集不見了 ⇒ 放掉守衛讓規則重建
            else:
                ttl = float(((state.get("policy") or {}).get("values") or {}).get("affect_ttl_h") or 12)
                target["occurred_at"] = now
                target.setdefault("affect", {})["expires_at"] = now + ttl * 3600.0
            state["revision"] = int(state.get("revision") or 0) + 1
            state["as_of"] = now
            state["view"] = aem_v2.project(state, now)
            store.save(state)
            affect._log(f"affect: 缺席續期（{_slug(entity)}）——舊集已過期，重新確認一次")
    except Exception as exc:                            # noqa: BLE001 - 續期失敗不能擋心跳
        affect._log(f"affect: 缺席續期略過（{type(exc).__name__}: {exc}）")


def idle_s(affect, entity: str, *, at: float | None = None) -> float:
    """多久沒跟她說話（以主人那條為準）。

    進程內 `_last_touch` 只當「這次啟動後的新鮮度」，重啟就沒了；持久化的
    `facts.last_user_message_at` 才是真的時鐘（2026-11-09 實測：重啟後管理台顯示
    「剛剛還在說話／寂寞 0%」，而核心的缺席 episode 已經在報）。
    """
    now = float(at if at is not None else affect._clock())
    with affect._lock:
        memory = affect._last_touch.get(_slug(entity))
    stored = None
    try:
        state = affect._store(entity).show()
        stored = (state.get("facts") or {}).get("last_user_message_at")
    except Exception:                                   # noqa: BLE001
        stored = None
    last = max([value for value in (memory, stored) if value is not None], default=None)
    if last is None:
        return 0.0
    return max(0.0, now - float(last))


__all__ = ["idle_s", "migrate_policy", "rearm_absence", "reproject", "scale_delta"]
