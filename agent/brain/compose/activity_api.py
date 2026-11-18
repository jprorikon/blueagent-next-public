"""活動控制：開始/暫停/繼續/重置/模擬重啟。**按角色**（每條 LifeLoop 各自一條）。

owner 2026-10-21：「这些角色都在线，各自走自己的一套东西」——所以每個操作都要指名角色；
不指名就用預設角色（`entities.default()`），但**絕不會**去動別人的活動。
"""

from __future__ import annotations

from agent.brain.runtime.life import build_activity
from agent.brain.runtime.life_loop import LifeLoop


def _loop(state, entity: str = ""):
    app = state.app
    target = str(entity or "").strip()
    if not target:
        default = app.entities.default()
        target = default.id if default is not None else "mira"
    loop = app.lives.get(target)
    if loop is None:
        raise KeyError(f"unknown entity: {target}")
    return target, loop


def start(state, *, entity: str = "", principal: str = "owner") -> dict:
    target, loop = _loop(state, entity)
    # 手動「開始做事」是主人下的令，**不受每日上限／休息間隔限制**：輪換回 None 就退回
    # 「不看輪換」的挑法（照樣避開現在這一件），不要讓主人按了沒反應。
    activity = build_activity(target, rotation=getattr(state.app, "rotation", None),
                              after=_current_key(loop))
    if activity is None:
        activity = build_activity(target, rotation=None, after=_current_key(loop))
    loop.start(activity)
    return {"ok": True, "entity": target, "life": loop.snapshot(),
            "lives": {key: item.snapshot() for key, item in state.app.lives.items()},
            "state": state.snapshot(principal=principal, entity=target)}


def _current_key(loop) -> str:
    """現在這一件是哪一套（手動換手時不要排到同一套）。"""
    from agent.brain.runtime.life import preset_key_of
    try:
        return preset_key_of(loop.current())
    except Exception:                                  # noqa: BLE001
        return ""


def pause(state, *, entity: str = "") -> dict:
    target, loop = _loop(state, entity)
    loop.pause("manual")
    return {"ok": True, "entity": target, "life": loop.snapshot(),
            "lives": {key: item.snapshot() for key, item in state.app.lives.items()},
            "state": state.snapshot(entity=target)}


def resume(state, *, entity: str = "") -> dict:
    target, loop = _loop(state, entity)
    loop.resume()
    return {"ok": True, "entity": target, "life": loop.snapshot(),
            "lives": {key: item.snapshot() for key, item in state.app.lives.items()},
            "state": state.snapshot(entity=target)}


def reset(state, *, entity: str = "") -> dict:
    target, loop = _loop(state, entity)
    loop.reset()
    return {"ok": True, "entity": target, "life": loop.snapshot(),
            "lives": {key: item.snapshot() for key, item in state.app.lives.items()},
            "state": state.snapshot(entity=target)}


def reload(state, *, entity: str = "") -> dict:
    """模擬「進程重啟」：只重建那一個角色的 LifeLoop，其他角色不受影響。"""
    app = state.app
    target, loop = _loop(state, entity)
    loop.shutdown()
    # 重建時要把「排下一件」接回去，否則她做完手上這件就停住（2026-10-24 修）
    fresh = LifeLoop(target, app.activity_store, app.step_fn,
                     step_pause=getattr(loop, "_step_pause", 0.15),   # 沿用原本的留白（別在正式機退回 0.15 秒）
                     next_activity=getattr(app, "next_activity", None))
    app.lives[target] = fresh
    return {"ok": True, "entity": target, "life": fresh.snapshot(),
            "lives": {key: item.snapshot() for key, item in app.lives.items()},
            "state": state.snapshot(entity=target)}
