#!/usr/bin/env python3
"""Python 3.10+, POSIX local filesystem, standard library only."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

DEFAULT_ROOT = Path.home() / ".local" / "state" / "aem-v2"
DAY = 86400
RETENTION = 7 * DAY
KINDS = {
    "concern", "pleasure", "frustration", "interest",
    "uncertainty", "unclassified",
    # 2026-10-11：照「情緒→行為」文獻（Frijda 的行動傾向）加的三個
    "repair",      # 罪惡感／修復：道歉、改正、補償
    "gratitude",   # 感激：付出成本的行為
    "absence",     # 他不在：重新連結的動機
}
TERMINAL = {"sent", "failed", "unknown", "cancelled", "expired"}
CONSTRAINTS = ["NO_REPLY_DEMAND", "NO_GUILT_APPEAL"]
POLICY_KEYS = {
    "half_life_min", "gain", "affect_ttl_h", "proactive_threshold",
    "scan_intervals_s", "absence", "allowed_action_kinds",
    "action_ttl_s", "respect_do_not_interrupt", "max_proactive_24h",
    "min_proactive_gap_s", "reservation_ttl_s", "watch_ttl_s",
    "task_ttl_s", "interruptibility_ttl_s", "appraisal_window_s",
    "max_state_bytes", "rules", "expression_constraints", "dynamics",
}


class AEMError(Exception):
    def __init__(self, code, exit_code=2):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def require(condition, code="INVALID_INPUT", exit_code=2):
    if not condition:
        raise AEMError(code, exit_code)


def number(value, low=None, high=None):
    require(type(value) in (int, float))
    require(math.isfinite(value))
    if low is not None:
        require(value >= low)
    if high is not None:
        require(value <= high)
    return value


def integer(value, low=0, high=None):
    require(type(value) is int)
    return number(value, low, high)


def ident(value):
    require(isinstance(value, str))
    require(re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,200}", value) is not None)
    return value


def canonical(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "INVALID_JSON")
            result[key] = value
        return result

    def constant(_):
        raise AEMError("INVALID_JSON")

    try:
        return json.loads(
            text, object_pairs_hook=pairs, parse_constant=constant,
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AEMError("INVALID_JSON") from exc


def load_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        return strict_json(stream.read())


def validate_policy(value):
    require(isinstance(value, dict), "INVALID_POLICY")
    p = copy.deepcopy(value)
    p.pop("_note", None)
    try:
        require(set(p) == POLICY_KEYS)
        require(set(p["half_life_min"]) == {"v", "a"})
        require(set(p["gain"]) == {"v", "a"})
        for dim in ("v", "a"):
            number(p["half_life_min"][dim], 0.001, 1000000)
            number(p["gain"][dim], 0, 1)
        number(p["affect_ttl_h"], 0.001, 168)
        number(p["proactive_threshold"], 0, 1)
        intervals = p["scan_intervals_s"]
        require(isinstance(intervals, list) and len(intervals) == 3)
        for item in intervals:
            integer(item, 1, DAY)
        require(intervals[0] >= intervals[1] >= intervals[2])
        require(set(p["absence"]) == {"enabled", "after_s"})
        require(type(p["absence"]["enabled"]) is bool)
        after = p["absence"]["after_s"]
        if after is not None:
            integer(after, 1, RETENTION)
        require(not p["absence"]["enabled"] or after is not None)
        allowed = p["allowed_action_kinds"]
        require(isinstance(allowed, list))
        require(len(allowed) == len(set(allowed)))
        require(set(allowed) <= {"report_blocker", "check_in"})
        for key in (
            "action_ttl_s", "reservation_ttl_s", "watch_ttl_s",
            "task_ttl_s", "interruptibility_ttl_s", "appraisal_window_s",
        ):
            integer(p[key], 1, RETENTION)
        require(p["reservation_ttl_s"] <= 60)
        require(p["appraisal_window_s"] <= 120)
        require(type(p["respect_do_not_interrupt"]) is bool)
        integer(p["max_proactive_24h"], 0, 1000)
        integer(p["min_proactive_gap_s"], 0, RETENTION)
        integer(p["max_state_bytes"], 16384, 100000000)
        require(p["expression_constraints"] == CONSTRAINTS)
        # 動力學（2026-10-11 老師批「換聚合」）：聚合形狀與中性先驗 q₀。
        # `"sum"` 留著 ⇒ 隨時可以切回舊行為（可回退）。
        # `arousal_kernel`（2026-10-11 老師批 Q1=a「只給 arousal 加慣性」）：缺省＝一階。
        require(set(p["dynamics"]) <= {"valence", "arousal", "q0", "arousal_kernel"})
        require({"valence", "arousal", "q0"} <= set(p["dynamics"]))
        require(p["dynamics"]["valence"] in {"sum", "avg"})
        require(p["dynamics"]["arousal"] in {"sum", "peak"})
        number(p["dynamics"]["q0"], 0.0, 1000.0)
        _ak = p["dynamics"].get("arousal_kernel")
        if _ak is not None:
            require(set(_ak) == {"type", "tau1_s", "tau2_s"})
            require(_ak["type"] == "2nd")
            number(_ak["tau1_s"], 1.0, 86400.0)
            number(_ak["tau2_s"], 1.0, 86400.0)
            require(_ak["tau2_s"] > _ak["tau1_s"])   # 非振盪的必要條件
        require(set(p["rules"]) == {"task_blocked", "absence"})
        for rule in p["rules"].values():
            require(set(rule) == {"delta_v", "delta_a", "urgency"})
            number(rule["delta_v"], -1, 1)
            number(rule["delta_a"], 0, 1)
            number(rule["urgency"], 0, 1)
    except (AEMError, TypeError, KeyError, AttributeError) as exc:
        raise AEMError("INVALID_POLICY") from exc
    return p


# ── provenance（2026-10-19 與 hermes 定案）──────────────────────────────
# 三個 optional 欄位：核心**只驗形狀、不讀內容、不參與投影**。
# 目的是讓「delta 是怎麼算出來的」「判定時看到哪一版關係」「重激活指回哪一集」
# 都能留在事件流裡 ⇒ 同一串事件在兩邊 replay 會得到同一個狀態。
PROVENANCE_KEYS = ("transform_ref", "context_ref", "ref_episode_id")
PROVENANCE_MAX_BYTES = 4096
# 低於此信心的判定＝「判了但不用」：收事件、關回條、不建 episode（2026-10-19）
MIN_APPRAISAL_CONF = 0.25


def validate_provenance(e):
    tr = e.get("transform_ref")
    if tr is not None:
        require(isinstance(tr, dict))
        require(set(tr) <= {"name", "v", "inputs"})
        require(isinstance(tr.get("name"), str) and 0 < len(tr["name"]) <= 64)
        require(type(tr.get("v")) is int and tr["v"] >= 1)
        if "inputs" in tr and tr["inputs"] is not None:
            require(isinstance(tr["inputs"], dict))
        require(len(canonical(tr)) <= PROVENANCE_MAX_BYTES)
    cr = e.get("context_ref")
    if cr is not None:
        require(isinstance(cr, dict) and set(cr) == {"rel_rev"})
        require(type(cr["rel_rev"]) is int)
    ref = e.get("ref_episode_id")
    if ref is not None:
        ident(ref)


def validate_event(raw, now, receipts):
    require(isinstance(raw, dict))
    e = copy.deepcopy(raw)
    kind = e.get("kind")
    fields = {
        "message": {"len", "text", "text_sha256"},
        "appraisal": {
            "source_event_id", "episode_kind", "target",
            "delta_v", "delta_a", "confidence", "model", "contract_version",
            "control",
        },
        "judge_failure": {"source_event_id", "failure_code"},
        "outcome": {
            "episode_kind", "target", "delta_v", "delta_a",
            "confidence", "model", "contract_version", "control",
        },
        "task": {"task_id", "status"},
        "interruptibility": {"value", "source"},
    }
    require(kind in fields)
    require(set(e) <= fields[kind] | {"event_id", "kind", "occurred_at"} | set(PROVENANCE_KEYS))
    validate_provenance(e)
    ident(e.get("event_id"))
    previous = next(
        (r for r in receipts if r["event_id"] == e["event_id"]), None,
    )
    if "occurred_at" not in e:
        e["occurred_at"] = (
            previous["occurred_at"] if previous is not None else now
        )
    number(e["occurred_at"], 0)
    require(e["occurred_at"] <= now, "FUTURE_EVENT")
    require(e["occurred_at"] > now - RETENTION, "EVENT_TOO_OLD")

    if kind == "message":
        e.setdefault("len", None)
        if e["len"] is not None:
            integer(e["len"], 0)
        text = e.pop("text", None)
        if text is not None:
            require(isinstance(text, str))
            hashed = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if "text_sha256" in e:
                require(e["text_sha256"] == hashed)
            e["text_sha256"] = hashed
        e.setdefault("text_sha256", None)
        if e["text_sha256"] is not None:
            require(
                isinstance(e["text_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", e["text_sha256"]) is not None
            )

    elif kind == "appraisal":
        ident(e.get("source_event_id"))
        require(e.get("contract_version") in {"appraisal-v1", "appraisal-v2"})
        require(e.get("episode_kind") in KINDS)
        target = e.get("target")
        require(isinstance(target, dict) and set(target) == {"type", "id"})
        require(target["type"] in {"owner", "task", "conversation"})
        ident(target["id"])
        for key, low, high in (
            ("delta_v", -1, 1), ("delta_a", 0, 1), ("confidence", 0, 1),
        ):
            e.setdefault(key, None)
            if e[key] is not None:
                number(e[key], low, high)
        e.setdefault("model", None)
        if e["model"] is not None:
            ident(e["model"])
        # v2：control＝這件事誰能動（aster／owner／neither）。只記錄，不建立動作。
        e.setdefault("control", None)
        if e["control"] is not None:
            require(e["control"] in {"aster", "owner", "neither"})

    elif kind == "outcome":
        # 自我事件（2026-10-11）：我自己這邊發生的事（例：打牌輸了），直接帶我的判讀。
        # 不掛在 message 上——所以不動 owner 的 facts（message_count／last_user_message_at／absence）。
        require(e.get("contract_version") in {"appraisal-v2", "outcome-v1"})
        require(e.get("episode_kind") in KINDS)
        target = e.get("target")
        require(isinstance(target, dict) and set(target) == {"type", "id"})
        require(target["type"] in {"owner", "task", "conversation"})
        ident(target["id"])
        for key, low, high in (
            ("delta_v", -1, 1), ("delta_a", 0, 1), ("confidence", 0, 1),
        ):
            e.setdefault(key, None)
            if e[key] is not None:
                number(e[key], low, high)
        e.setdefault("model", None)
        if e["model"] is not None:
            ident(e["model"])
        e.setdefault("control", None)
        if e["control"] is not None:
            require(e["control"] in {"aster", "owner", "neither"})

    elif kind == "judge_failure":
        ident(e.get("source_event_id"))
        require(e.get("failure_code") in {
            "JUDGE_EXCEPTION", "JUDGE_TIMEOUT",
            "JUDGE_INVALID", "JUDGE_UNAVAILABLE",
        })

    elif kind == "task":
        ident(e.get("task_id"))
        require(e.get("status") in {"active", "blocked", "done", "cancelled"})

    elif kind == "interruptibility":
        require(e.get("value") in {
            "unknown", "available", "do_not_interrupt",
        })
        ident(e.get("source"))
    return e


def find_action(state, action_id):
    for ep in state["episodes"]:
        action = ep["action"]
        if action is not None and action["id"] == action_id:
            return action
    raise AEMError("ACTION_NOT_FOUND", 3)


def find_reservation(state, reservation_id):
    for reservation in state["budget"]["reservations"]:
        if reservation["id"] == reservation_id:
            return reservation
    raise AEMError("RESERVATION_NOT_FOUND", 3)


def close_pending(action, now, reason, status="cancelled"):
    if action is not None and action["status"] == "pending":
        action["status"] = status
        action["settled_at"] = now
        action["settle_reason"] = reason


def make_episode(state, source, occurred, kind, target, dv, da,
                 appraisal, rule=None, action_kind=None):
    p = state["policy"]["values"]
    ep = {
        "id": "ep:" + source,
        "source_event_id": source,
        "occurred_at": occurred,
        "kind": kind,
        "target": target,
        "appraisal": appraisal,
        "affect": {
            "delta_valence": dv,
            "delta_arousal": da,
            "expires_at": occurred + p["affect_ttl_h"] * 3600,
        },
        "watch_until": occurred + p["watch_ttl_s"],
        "action": None,
    }
    if action_kind is not None:
        ep["action"] = {
            "id": "action:" + source,
            "kind": action_kind,
            "urgency": rule["urgency"],
            "ground_event_ids": [source],
            "created_at": occurred,
            "expires_at": occurred + p["action_ttl_s"],
            "status": "pending",
            "reservation_id": None,
            "settled_at": None,
            "settle_reason": None,
        }
    state["episodes"].append(ep)
    return ep


def cancel_task_actions(state, now, task_id=None):
    for ep in state["episodes"]:
        if ep["target"]["type"] == "task":
            if task_id is None or ep["target"]["id"] == task_id:
                close_pending(ep["action"], now, "task_closed")
                ep["watch_until"] = None


def age(state, now):
    facts = state["facts"]
    task = facts["task"]
    if task is not None and task["expires_at"] <= now:
        cancel_task_actions(state, now, task["id"])
        facts["task"] = None
    interrupt = facts["interruptibility"]
    if interrupt is not None and interrupt["expires_at"] <= now:
        facts["interruptibility"] = None

    for ep in state["episodes"]:
        action = ep["action"]
        if action is None:
            continue
        if action["status"] == "reserved":
            reservation = find_reservation(state, action["reservation_id"])
            if reservation["expires_at"] <= now:
                reservation["outcome"] = "unknown"
                reservation["settled_at"] = now
                action["status"] = "unknown"
                action["settled_at"] = now
                action["settle_reason"] = "reservation_timeout"
        if action["status"] == "pending" and action["expires_at"] <= now:
            close_pending(action, now, "action_timeout", "expired")

    cutoff = now - RETENTION
    state["receipts"] = [
        r for r in state["receipts"] if r["occurred_at"] > cutoff
    ]
    state["budget"]["reservations"] = [
        r for r in state["budget"]["reservations"]
        if r["created_at"] > cutoff or r["outcome"] == "reserved"
    ]
    state["episodes"] = [
        ep for ep in state["episodes"]
        if not (
            ep["occurred_at"] <= cutoff
            and ep["affect"]["expires_at"] <= now
            and (ep["watch_until"] is None or ep["watch_until"] <= now)
            and (ep["action"] is None or ep["action"]["status"] in TERMINAL)
        )
    ]


def absence_rule(state, now):
    p = state["policy"]["values"]
    facts = state["facts"]
    last = facts["last_user_message_at"]
    if (
        not p["absence"]["enabled"]
        or last is None
        or facts["absence_episode_id"] is not None
    ):
        return
    occurred = last + p["absence"]["after_s"]
    if now < occurred:
        return
    source = "absence:" + facts["last_user_message_id"]
    rule = p["rules"]["absence"]
    ep = make_episode(
        state, source, occurred, "concern",
        {"type": "owner", "id": "owner"},
        rule["delta_v"], rule["delta_a"],
        {"source": "rule", "contract_version": "rule-v1",
         "model": None, "confidence": None},
        rule, "check_in",
    )
    facts["absence_episode_id"] = ep["id"]


def _second_order(t, tau1_s, tau2_s):
    """非振盪二階（兩段一階串接）的脈衝響應，**正規化到峰值 1**。

    老師 2026-10-11 批（Q1=a）：**只給 arousal 加慣性**，valence 維持一階慢層。
    刻意不選欠阻尼：只借慣性（達峰延遲），不借振盪——振盪會讓正的事件過衝成負，
    等於偷偷新增「好事之後可以自己變壞」這條語意。

    **為什麼要正規化**：不正規化時峰值會隨 `τ₂/τ₁` 落在 0.67~0.72（τ=3/20 分是 0.7155），
    那樣換 τ 就會順便動到靈敏度——把「時間形狀」和「值域大小」兩個參數綁在一起。
    q₀/gain 那一格才剛拆過同一種糾纏，不在這裡重犯。
    """
    if t < 0:
        return 0.0
    if abs(tau2_s - tau1_s) < 1e-6:
        return (t / tau1_s) * math.exp(1.0 - t / tau1_s)
    k = tau2_s / (tau2_s - tau1_s)
    tp = tau1_s * tau2_s * math.log(tau2_s / tau1_s) / (tau2_s - tau1_s)
    peak = k * (math.exp(-tp / tau2_s) - math.exp(-tp / tau1_s))
    return k * (math.exp(-t / tau2_s) - math.exp(-t / tau1_s)) / peak


def _a_decay(dyn, elapsed, hl_min):
    """arousal 的時間核。預設一階 `2^(-t/HL)`；`dynamics.arousal_kernel` 指定 2nd 時走二階。

    缺參數＝一階 ⇒ **隨時可以切回舊行為**（可回退，跟 `dynamics.valence="sum"` 同一個做法）。
    """
    k = dyn.get("arousal_kernel")
    if not k or k.get("type") != "2nd":
        return 2.0 ** (-elapsed / (hl_min * 60.0))
    return _second_order(elapsed, float(k["tau1_s"]), float(k["tau2_s"]))


def project(state, now, time_scale=None):
    """`time_scale(t0, t1) -> 有效秒數`（選填）：睡眠時段的時間流速不同時用（見
    `runtime/inner/sleep_decay.py`）。不給＝與舊行為逐位元相同（`t1 - t0`）。"""
    p = state["policy"]["values"]
    v_terms, a_terms, ranked = [], [], []
    evidence = False
    watches, pending = [], []

    # 動力學（2026-10-11 老師批）：valence 預設走「帶中性先驗的衰減加權平均」、
    # arousal 走「去重事件的衰減峰值」。`dynamics` 切回 "sum" 就是舊行為（可回退）。
    # ⚠ `gain.v` 在 "avg" 下不使用——平均會自己歸一化，乘上去會被分子分母約掉，只會弄髒語意。
    dyn = p["dynamics"]
    q0 = float(dyn["q0"])
    v_num, v_den = 0.0, 0.0
    # 旁路（2026-10-19，Astra 審過的最小版）：valence 按 kind 的貢獻分解。
    # 只讀不寫上面的生產值；任何例外都吞掉，不影響 v/a，也不設拒載。
    comp_num, comp_den = {}, {}
    for ep in state["episodes"]:
        affect = ep["affect"]
        v, a = 0.0, 0.0
        if ep["occurred_at"] <= now < affect["expires_at"]:
            elapsed = (float(time_scale(ep["occurred_at"], now))
                       if time_scale is not None else now - ep["occurred_at"])
            dv, da = affect["delta_valence"], affect["delta_arousal"]
            # 信心：None＝沒給（用中性 0.5）；低於門檻＝「判了但不用」，
            # **valence 與 arousal 都不採**，但 watch／pending 照常處理（不能 continue）。
            conf = (ep.get("appraisal") or {}).get("confidence")
            low_conf = conf is not None and conf < MIN_APPRAISAL_CONF
            if conf is None:
                conf = 0.5
            if dv is not None and not low_conf:
                evidence = True
                v = p["gain"]["v"] * dv * 2 ** (
                    -elapsed / (p["half_life_min"]["v"] * 60)
                )
                if dyn["valence"] == "avg":
                    con = 2.0 * dv / conf          # delta_v = 0.5 × con × conf
                    w = conf * 2 ** (-elapsed / (p["half_life_min"]["v"] * 60))
                    v_num += w * con
                    v_den += w
                    try:
                        _k = ep["kind"]
                        comp_num[_k] = comp_num.get(_k, 0.0) + w * con
                        comp_den[_k] = comp_den.get(_k, 0.0) + w
                    except Exception:
                        pass
                else:
                    v_terms.append(v)
            if da is not None and not low_conf:
                evidence = True
                decay_a = _a_decay(dyn, elapsed, p["half_life_min"]["a"])
                a = p["gain"]["a"] * da * decay_a
                if dyn["arousal"] == "peak":
                    a_terms.append(da * decay_a)
                else:
                    a_terms.append(a)
            if (dv is not None or da is not None) and not low_conf:
                ranked.append((abs(v) + a, ep["id"]))
        if ep["watch_until"] is not None and ep["watch_until"] > now:
            watches.append(ep["id"])
        action = ep["action"]
        if (
            action is not None
            and action["status"] == "pending"
            and action["expires_at"] > now
        ):
            pending.append(action)

    # Sorting plus fsum avoids arrival-order-dependent summation.
    if dyn["valence"] == "avg":
        v = v_num / (q0 + v_den) if (q0 + v_den) else 0.0
    else:
        v = math.fsum(sorted(v_terms))
    v = max(-1.0, min(1.0, v))
    if dyn["arousal"] == "peak":
        a = max(a_terms) if a_terms else 0.0
    else:
        a = math.fsum(sorted(a_terms))
    a = max(0.0, min(1.0, a))
    charged = [
        r for r in state["budget"]["reservations"]
        if r["created_at"] > now - DAY
        and r["outcome"] in {"reserved", "sent", "unknown"}
    ]
    global_blocks = []
    interrupt = state["facts"]["interruptibility"]
    if (
        p["respect_do_not_interrupt"]
        and interrupt is not None
        and interrupt["expires_at"] > now
        and interrupt["value"] == "do_not_interrupt"
    ):
        global_blocks.append("DO_NOT_INTERRUPT")
    if len(charged) >= p["max_proactive_24h"]:
        global_blocks.append("BUDGET_EXHAUSTED")
    if any(
        now < r["created_at"] + p["min_proactive_gap_s"] for r in charged
    ):
        global_blocks.append("MIN_GAP")

    eligible, blockers = [], set()
    if len(pending) == 0:
        blockers.add("NO_PENDING_ACTION")
    for action in pending:
        codes = list(global_blocks)
        if action["kind"] not in p["allowed_action_kinds"]:
            codes.append("ACTION_KIND_DISABLED")
        if action["urgency"] < p["proactive_threshold"]:
            codes.append("BELOW_THRESHOLD")
        if len(codes) == 0:
            eligible.append(action["id"])
        blockers.update(codes)

    high = any(
        action["urgency"] >= p["proactive_threshold"] for action in pending
    )
    interval = p["scan_intervals_s"][2 if high else 1 if watches else 0]
    profile = "gentle" if v < 0 else "attentive" if a > 0 else "neutral"

    # 旁路：valence 按 kind 的分解。status 只保證「實作一致」（clip(Σc)==valence），
    # 不保證情緒有效性——那不是這條線能回答的。失敗就給 None，不影響任何既有欄位。
    composition = None
    if dyn["valence"] == "avg":
        try:
            _denom = q0 + v_den
            if _denom > 0:
                _by_kind = {
                    _k: {"c": comp_num[_k] / _denom, "w": comp_den[_k]}
                    for _k in sorted(comp_num)
                }
                _sum = math.fsum(x["c"] for x in _by_kind.values())
                _clipped = max(-1.0, min(1.0, _sum))
                composition = {
                    "by_kind": _by_kind,
                    "denominator": _denom,
                    "sum_c": _sum,
                    "deviation": v - _clipped,
                    "status": "ok" if abs(v - _clipped) <= 1e-9 else "mismatch",
                }
        except Exception:
            composition = None

    return {
        "revision": state["revision"],
        "policy_revision": state["policy"]["revision"],
        "as_of": now,
        "refresh_by": now + interval,
        "affect": {
            "valence": v,
            "arousal": a,
            "evidence_status": "estimated" if evidence else "prior_only",
            "composition": composition,
        },
        "dominant_episode_ids": [
            ep_id for _, ep_id in sorted(ranked, key=lambda x: (-x[0], x[1]))[:3]
        ],
        "attention": {
            "watch_episode_ids": sorted(watches),
            "requested_scan_interval_s": interval,
        },
        "expression": {
            "profile": profile,
            "constraints": list(p["expression_constraints"]),
        },
        "proactive": {
            "eligible_action_ids": sorted(eligible),
            "blocker_codes": sorted(blockers),
        },
    }


def apply_event(state, e, now):
    p = state["policy"]["values"]
    facts = state["facts"]
    kind, occurred = e["kind"], e["occurred_at"]
    receipt = {
        "event_id": e["event_id"],
        "kind": kind,
        "payload_sha256": digest(e),
        "occurred_at": occurred,
        "judge_status": "pending" if kind == "message" else "not_applicable",
        "judge_failure_code": None,
        "episode_id": None,
        "len": e.get("len"),
        "text_sha256": e.get("text_sha256"),
    }
    # provenance：沒帶就不寫（舊事件不補寫）；帶了就照抄，核心不讀內容。
    for _key in PROVENANCE_KEYS:
        if _key in e:
            receipt[_key] = e[_key]

    if kind == "message":
        facts["message_count"] += 1
        last = facts["last_user_message_at"]
        if last is None or occurred >= last:
            facts["last_user_message_at"] = occurred
            facts["last_user_message_id"] = e["event_id"]
            facts["last_message_len"] = e["len"]
            absence_id = facts["absence_episode_id"]
            for ep in state["episodes"]:
                if ep["id"] == absence_id:
                    close_pending(ep["action"], now, "owner_returned")
                    ep["watch_until"] = None
            facts["absence_episode_id"] = None

    elif kind == "outcome":
        ep = make_episode(
            state, e["event_id"], occurred, e["episode_kind"], e["target"],
            e["delta_v"], e["delta_a"],
            {"source": "self", "contract_version": e["contract_version"],
             "model": e["model"], "confidence": e["confidence"],
             "control": e.get("control")},
        )
        receipt["episode_id"] = ep["id"]

    elif kind in {"appraisal", "judge_failure"}:
        source = next(
            (r for r in state["receipts"]
             if r["event_id"] == e["source_event_id"]
             and r["kind"] == "message"), None,
        )
        require(source is not None, "SOURCE_NOT_FOUND", 3)
        require(source["judge_status"] == "pending", "JUDGE_ALREADY_FINAL", 3)
        if kind == "judge_failure":
            source["judge_status"] = "failed"
            source["judge_failure_code"] = e["failure_code"]
        else:
            require(
                now <= source["occurred_at"] + p["appraisal_window_s"],
                "APPRAISAL_TOO_LATE", 3,
            )
            # 低信心＝「判了但不用」（2026-10-19 定案）：收下事件、把回條關成 failed，
            # **不建 episode**。這樣「完全沒把握」(0) 不會被當成中性權重進場，
            # 也不會讓回條永遠卡在 pending。
            _conf = e["confidence"]
            if _conf is not None and _conf < MIN_APPRAISAL_CONF:
                source["judge_status"] = "failed"
                source["judge_failure_code"] = "CONFIDENCE_TOO_LOW"
                receipt["judge_failure_code"] = "CONFIDENCE_TOO_LOW"
                receipt["judge_status"] = "failed"
            else:
                ep = make_episode(
                    state, source["event_id"], source["occurred_at"],
                    e["episode_kind"], e["target"], e["delta_v"], e["delta_a"],
                    {"source": "judge", "contract_version": e["contract_version"],
                     "model": e["model"], "confidence": e["confidence"],
                     "control": e.get("control"),
                     **{k: e[k] for k in PROVENANCE_KEYS if k in e}},
                )
                source["judge_status"] = "applied"
                source["episode_id"] = ep["id"]
                receipt["episode_id"] = ep["id"]

    elif kind == "task":
        task = facts["task"]
        if task is not None:
            require(occurred >= task["observed_at"], "STALE_FACT", 3)
            if task["id"] != e["task_id"]:
                cancel_task_actions(state, now)
        facts["task"] = {
            "id": e["task_id"], "status": e["status"],
            "observed_at": occurred,
            "expires_at": occurred + p["task_ttl_s"],
        }
        if e["status"] != "blocked":
            cancel_task_actions(state, now, e["task_id"])
        else:
            # A refreshed blocked fact replaces an unreserved older notice.
            cancel_task_actions(state, now, e["task_id"])
            rule = p["rules"]["task_blocked"]
            ep = make_episode(
                state, e["event_id"], occurred, "frustration",
                {"type": "task", "id": e["task_id"]},
                rule["delta_v"], rule["delta_a"],
                {"source": "rule", "contract_version": "rule-v1",
                 "model": None, "confidence": None},
                rule, "report_blocker",
            )
            receipt["episode_id"] = ep["id"]

    elif kind == "interruptibility":
        old = facts["interruptibility"]
        if old is not None:
            require(occurred >= old["observed_at"], "STALE_FACT", 3)
        facts["interruptibility"] = {
            "value": e["value"], "source": e["source"],
            "observed_at": occurred,
            "expires_at": occurred + p["interruptibility_ttl_s"],
        }

    state["receipts"].append(receipt)


def reduce(state, command, now):
    """Pure state transition. Returns (state, result); duplicate returns input."""
    number(now, 0)
    require(now >= state["as_of"], "TIME_REVERSED", 3)
    op = command["op"]
    require(op in {"apply", "advance", "reserve", "settle", "install-policy"})
    result = {"code": "OK"}

    if op in {"reserve", "install-policy"}:
        integer(command["expected_revision"], 0)
        require(
            command["expected_revision"] == state["revision"],
            "REVISION_CONFLICT", 3,
        )

    event = None
    if op == "apply":
        event = validate_event(command["event"], now, state["receipts"])
        old = next(
            (r for r in state["receipts"]
             if r["event_id"] == event["event_id"]), None,
        )
        if old is not None:
            require(
                old["payload_sha256"] == digest(event),
                "EVENT_ID_CONFLICT", 3,
            )
            return state, {"code": "DUPLICATE", "event_id": event["event_id"]}
        result["event_id"] = event["event_id"]

    if op == "settle":
        action = find_action(state, command["action_id"])
        outcome = command["outcome"]
        reservation_id = command.get("reservation_id")
        require(outcome in {"sent", "failed", "unknown", "handled_in_reply"})
        if outcome == "handled_in_reply":
            require(reservation_id is None, "RESERVATION_MISMATCH", 3)
            if (
                action["status"] == "cancelled"
                and action["settle_reason"] == "handled_in_reply"
            ):
                return state, {"code": "DUPLICATE", "action_id": action["id"]}
        else:
            require(reservation_id is not None, "RESERVATION_REQUIRED", 3)
            require(
                reservation_id == action["reservation_id"],
                "RESERVATION_MISMATCH", 3,
            )
            if action["status"] == outcome:
                return state, {"code": "DUPLICATE", "action_id": action["id"]}

    new = copy.deepcopy(state)
    age(new, now)

    if op == "apply":
        apply_event(new, event, now)

    elif op == "install-policy":
        values = validate_policy(command["policy"])
        new["policy"] = {
            "revision": new["policy"]["revision"] + 1,
            "sha256": digest(values),
            "values": values,
        }
        if not values["absence"]["enabled"]:
            absence_id = new["facts"]["absence_episode_id"]
            for ep in new["episodes"]:
                if ep["id"] == absence_id:
                    close_pending(ep["action"], now, "policy_disabled")
                    ep["watch_until"] = None

    # Qualification always uses the current time, not an old published view.
    absence_rule(new, now)
    age(new, now)

    if op == "reserve":
        action = find_action(new, command["action_id"])
        eligible = project(new, now)["proactive"]["eligible_action_ids"]
        require(action["id"] in eligible, "ACTION_INELIGIBLE", 3)
        reservation_id = ident(command["reservation_id"])
        require(
            all(r["id"] != reservation_id
                for r in new["budget"]["reservations"]),
            "RESERVATION_CONFLICT", 3,
        )
        reservation = {
            "id": reservation_id,
            "action_id": action["id"],
            "created_at": now,
            "expires_at": min(
                action["expires_at"],
                now + new["policy"]["values"]["reservation_ttl_s"],
            ),
            "outcome": "reserved",
            "settled_at": None,
        }
        new["budget"]["reservations"].append(reservation)
        action["status"] = "reserved"
        action["reservation_id"] = reservation_id
        result.update({
            "action_id": action["id"],
            "reservation_id": reservation_id,
            "expires_at": reservation["expires_at"],
        })

    elif op == "settle":
        action = find_action(new, command["action_id"])
        outcome = command["outcome"]
        if outcome == "handled_in_reply":
            require(action["status"] == "pending", "ACTION_NOT_PENDING", 3)
            close_pending(action, now, "handled_in_reply")
        else:
            require(action["status"] == "reserved", "ACTION_NOT_RESERVED", 3)
            reservation = find_reservation(new, command["reservation_id"])
            require(
                reservation["id"] == action["reservation_id"],
                "RESERVATION_MISMATCH", 3,
            )
            reservation["outcome"] = outcome
            reservation["settled_at"] = now
            action["status"] = outcome
            action["settled_at"] = now
            action["settle_reason"] = outcome
        result["action_id"] = action["id"]

    new["revision"] += 1
    new["as_of"] = now
    new["view"] = project(new, now)
    return new, result


def parse_at(value):
    try:
        result = float(value)
    except ValueError:
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            require(stamp.tzinfo is not None, "INVALID_TIME")
            result = stamp.timestamp()
        except (ValueError, OverflowError) as exc:
            raise AEMError("INVALID_TIME") from exc
    number(result, 0)
    return result


class Store:
    def __init__(self, root=DEFAULT_ROOT, agent="aster"):
        require(
            isinstance(agent, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent) is not None
        )
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / agent
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"

    @contextmanager
    def locked(self, create=False):
        require(os.name == "posix", "UNSUPPORTED_PLATFORM", 4)
        import fcntl

        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(self.directory.is_dir(), "NOT_INITIALIZED", 4)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        stream = os.fdopen(fd, "a+b")
        deadline = time.monotonic() + 0.050
        try:
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AEMError("LOCK_TIMEOUT", 5)
                    time.sleep(0.002)
            yield
        finally:
            # Closing the descriptor releases the lock, including on exceptions.
            stream.close()

    def show(self):
        require(os.name == "posix", "UNSUPPORTED_PLATFORM", 4)
        try:
            state = load_json(self.path)
        except FileNotFoundError as exc:
            raise AEMError("NOT_INITIALIZED", 4) from exc
        except (AEMError, UnicodeError) as exc:
            raise AEMError("STATE_CORRUPT", 4) from exc
        try:
            require(isinstance(state, dict))
            stored_hash = state["state_sha256"]
            body = {k: v for k, v in state.items() if k != "state_sha256"}
            require(digest(body) == stored_hash)
            require(state["schema_version"] == 2)
            integer(state["revision"], 0)
            number(state["as_of"], 0)
            require(state["view"]["revision"] == state["revision"])
            require(state["view"]["as_of"] == state["as_of"])
            p = validate_policy(state["policy"]["values"])
            require(digest(p) == state["policy"]["sha256"])
            require(project(state, state["as_of"]) == state["view"])
        except (AEMError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise AEMError("STATE_CORRUPT", 4) from exc
        return state

    def read(self):
        return self.show()["view"]

    def save(self, state, fault=None):
        body = {k: v for k, v in state.items() if k != "state_sha256"}
        body["state_sha256"] = digest(body)
        data = canonical(body) + b"\n"
        require(
            len(data) <= body["policy"]["values"]["max_state_bytes"],
            "STATE_CAPACITY", 4,
        )
        if fault is not None:
            require(self.root != DEFAULT_ROOT.resolve(), "FAULT_FORBIDDEN")
            require(fault in {"before_replace", "after_replace"})
        fd, temporary = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=self.directory,
        )
        replaced = False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if fault == "before_replace":
                raise AEMError("INJECTED_BEFORE_REPLACE", 6)
            os.replace(temporary, self.path)
            replaced = True
            if fault == "after_replace":
                raise AEMError("COMMIT_UNCERTAIN", 6)
            directory_fd = os.open(
                self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise AEMError(
                "COMMIT_UNCERTAIN" if replaced else "IO_ERROR", 6,
            ) from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return body

    def init(self, policy, now, fault=None):
        number(now, 0)
        values = validate_policy(policy)
        with self.locked(create=True):
            require(not self.path.exists(), "ALREADY_INITIALIZED", 3)
            state = {
                "schema_version": 2,
                "revision": 0,
                "created_at": now,
                "as_of": now,
                "policy": {
                    "revision": 1,
                    "sha256": digest(values),
                    "values": values,
                },
                "facts": {
                    "last_user_message_at": None,
                    "last_user_message_id": None,
                    "last_message_len": None,
                    "message_count": 0,
                    "absence_episode_id": None,
                    "task": None,
                    "interruptibility": None,
                },
                "receipts": [],
                "episodes": [],
                "budget": {"reservations": []},
                "view": None,
            }
            state["view"] = project(state, now)
            saved = self.save(state, fault)
            return {"result": {"code": "OK"}, "view": saved["view"]}

    def transact(self, command, now, fault=None):
        with self.locked():
            old = self.show()
            new, result = reduce(old, command, now)
            if new is not old:
                new = self.save(new, fault)
            return {"result": result, "view": new["view"]}

    def apply(self, event, now, fault=None):
        return self.transact({"op": "apply", "event": event}, now, fault)

    def advance(self, now, fault=None):
        return self.transact({"op": "advance"}, now, fault)

    def reserve(self, action_id, expected_revision, now, fault=None):
        return self.transact({
            "op": "reserve",
            "action_id": action_id,
            "expected_revision": expected_revision,
            "reservation_id": "reservation:" + uuid.uuid4().hex,
        }, now, fault)

    def settle(self, action_id, reservation_id, outcome, now, fault=None):
        return self.transact({
            "op": "settle", "action_id": action_id,
            "reservation_id": reservation_id, "outcome": outcome,
        }, now, fault)

    def install_policy(self, policy, expected_revision, now, fault=None):
        return self.transact({
            "op": "install-policy", "policy": policy,
            "expected_revision": expected_revision,
        }, now, fault)


def parser():
    p = argparse.ArgumentParser(description="AEM v2; POSIX local state core")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--agent", default="aster")
    p.add_argument("--at", help="Unix seconds or timezone-aware ISO 8601; sandbox only")
    p.add_argument("--fault", choices=["before_replace", "after_replace"])
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--policy", required=True)

    apply = sub.add_parser("apply")
    apply.add_argument("--event", help="JSON file; '-' reads stdin")
    apply.add_argument("--kind", choices=[
        "message", "appraisal", "judge_failure", "task", "interruptibility",
        "outcome",
    ])
    apply.add_argument("--event-id")
    apply.add_argument("--occurred-at")
    apply.add_argument("--text")
    apply.add_argument("--len", type=int)
    apply.add_argument("--task-id")
    apply.add_argument("--status", choices=["active", "blocked", "done", "cancelled"])
    apply.add_argument("--value", choices=[
        "unknown", "available", "do_not_interrupt",
    ])
    apply.add_argument("--source")
    apply.add_argument("--source-event-id")
    apply.add_argument("--failure-code", choices=[
        "JUDGE_EXCEPTION", "JUDGE_TIMEOUT", "JUDGE_INVALID", "JUDGE_UNAVAILABLE",
    ])

    sub.add_parser("advance")
    sub.add_parser("read")
    sub.add_parser("show")

    reserve = sub.add_parser("reserve")
    reserve.add_argument("--action-id", required=True)
    reserve.add_argument("--expected-revision", required=True, type=int)

    settle = sub.add_parser("settle")
    settle.add_argument("--action-id", required=True)
    settle.add_argument("--reservation-id")
    settle.add_argument("--outcome", required=True, choices=[
        "sent", "failed", "unknown", "handled_in_reply",
    ])

    policy = sub.add_parser("install-policy")
    policy.add_argument("--policy", required=True)
    policy.add_argument("--expected-revision", required=True, type=int)
    return p


def cli_event(args):
    names = (
        "kind", "event_id", "occurred_at", "text", "len", "task_id",
        "status", "value", "source", "source_event_id", "failure_code",
    )
    if args.event is not None:
        require(all(getattr(args, key) is None for key in names))
        return (
            strict_json(sys.stdin.read())
            if args.event == "-" else load_json(args.event)
        )
    require(args.kind is not None)
    event = {
        key: getattr(args, key)
        for key in names if getattr(args, key) is not None
    }
    event.setdefault("event_id", "event:" + uuid.uuid4().hex)
    if "occurred_at" in event:
        event["occurred_at"] = parse_at(event["occurred_at"])
    return event


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        store = Store(args.root, args.agent)
        if args.at is not None:
            require(store.root != DEFAULT_ROOT.resolve(), "AT_FORBIDDEN")
        if args.fault is not None:
            require(store.root != DEFAULT_ROOT.resolve(), "FAULT_FORBIDDEN")
        # This is the only wall-clock boundary for state time.
        now = time.time() if args.at is None else parse_at(args.at)
        op = args.command
        if op == "read":
            output = store.read()
        elif op == "show":
            output = store.show()
        elif op == "init":
            output = store.init(load_json(args.policy), now, args.fault)
        elif op == "apply":
            output = store.apply(cli_event(args), now, args.fault)
        elif op == "advance":
            output = store.advance(now, args.fault)
        elif op == "reserve":
            output = store.reserve(
                args.action_id, args.expected_revision, now, args.fault,
            )
        elif op == "settle":
            output = store.settle(
                args.action_id, args.reservation_id,
                args.outcome, now, args.fault,
            )
        else:
            output = store.install_policy(
                load_json(args.policy), args.expected_revision, now, args.fault,
            )
        print(canonical(output).decode("utf-8"))
        return 0
    except AEMError as exc:
        print(canonical({"error": exc.code}).decode("utf-8"), file=sys.stderr)
        return exc.exit_code
    except OSError:
        print('{"error":"IO_ERROR"}', file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())
