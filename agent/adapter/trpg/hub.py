"""通往 TRPG hub 的客戶端（**伺服器側的代理**）。

為什麼要代理而不是讓 App 直接打 hub：

  · 凭据只在伺服器（每個角色一把 account token，`.secrets`／data 裡，600）；
  · App 只認「我這個角色」，不需要知道 hub 在哪、也不需要拿 trpg 的 token；
  · 一個角色一個 account ⇒ Discord／Unity 兩個入口用的是同一把（owner 2026-10-22 定的模型）。

hub 的 GraphQL 面（實測）：
    POST /graphql  { tables { … } }、{ table(id:"…") { … } }、mutation { enter/say/act … }
⚠️ 別打到前台的 `:8080`／`:8971`（那是 Nuxt 前臺，`/api/*` 會被 SPA 吃掉回 HTML）；
    後台 hub 在 `:18971`（`hub_server.py`）。
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request

DEFAULT_HUB = "http://127.0.0.1:18971"
TIMEOUT_S = float(os.environ.get("BLUEAGENT_TRPG_TIMEOUT_S", "12") or "12")


class TrpgError(Exception):
    """hub 回了 errors／連不上：呼叫端回 502。"""


def _token_for(entity: str) -> str:
    """角色的 account token：`BLUEAGENT_TRPG_TOKENS`（`mira=tok;aster=tok`）或
    `$BLUEAGENT_DATA_DIR/trpg_tokens.json`（`{"mira": "tok"}`）。"""
    raw = str(os.environ.get("BLUEAGENT_TRPG_TOKENS") or "").strip()
    if raw:
        for chunk in raw.replace(",", ";").split(";"):
            if not chunk.strip():
                continue
            name, _, value = chunk.partition("=")
            if name.strip() == entity and value.strip():
                return value.strip()
    path = pathlib.Path(os.environ.get("BLUEAGENT_DATA_DIR", "data")) / "trpg_tokens.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get(entity) or "")
    except Exception:                                   # noqa: BLE001
        return ""


def token_available(entity: str) -> bool:
    return bool(_token_for(entity))


class TrpgHub:
    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url = (base_url or os.environ.get("BLUEAGENT_TRPG_HUB_URL") or DEFAULT_HUB).rstrip("/")
        self.token = token

    def query(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base_url + "/graphql", data=body,
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:                        # noqa: BLE001
            raise TrpgError(f"{type(exc).__name__}: {exc}") from exc
        if payload.get("errors"):
            first = payload["errors"][0] if isinstance(payload["errors"], list) else payload["errors"]
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise TrpgError(str(message))
        return payload.get("data") or {}


def hub_for(entity: str) -> TrpgHub:
    """按 entity 拿一把 hub（token 沒設定就回「沒凭证」，呼叫端顯示提示）。"""
    return TrpgHub(token=_token_for(entity))


#: `tables`（目錄，TableInfo）與 `table(id)`（單桌，Table）**欄位不一樣**：
#: TableInfo 有 `moduleName`／`scenarioName`，Table 沒有（實測踩到，Query 直接報 unknown field）。
LIST_FIELDS = "id module moduleName scenario scenarioName name online ended watchable"
# ★ 2026-10-28：`ending`＝這一局的結局（id／kind／名字）——`ended` 只是布林值，
#   app 需要知道「怎麼結束的」（勝敗？撤退？全滅？）。
TABLE_FIELDS = ("id module scenario name online ended ending seats events watchable "
                "owner isOwner archived")


def list_tables(hub: TrpgHub) -> list[dict]:
    data = hub.query("{ tables { " + LIST_FIELDS + " } }")
    return list(data.get("tables") or [])


def table_summary(hub: TrpgHub, table_id: str) -> dict:
    data = hub.query("{ table(id: \"" + table_id.replace('"', "") + "\") { " + TABLE_FIELDS + " } }")
    return data.get("table") or {}


def session_id(hub: TrpgHub, table_id: str) -> str:
    """這一局（run）的 session id：`table { session { id } }`（一副本一 session）。"""
    data = hub.query("query($t: ID!) { table(id: $t) { session { id } } }", {"t": table_id})
    session = ((data.get("table") or {}).get("session") or {})
    return str(session.get("id") or "")


def enter(hub: TrpgHub, table_id: str, *, char_id: str = "", session: str = "") -> dict:
    """進桌：`enter(tableId, session)`。

    沒席位會被擋（「先選角色 /api/pick_char」）⇒ 這裡先 `pickChar` 再 `enter`。
    `char_id` 空的話不挑（給已經有席位的人用）。
    """
    session = session or session_id(hub, table_id)
    if not session:
        raise TrpgError("no session for table")
    out: dict = {}
    if char_id:
        picked = hub.query("mutation($t: ID!, $s: ID!, $c: ID!) { pickChar(tableId: $t, session: $s, charId: $c) }",
                           {"t": table_id, "s": session, "c": char_id})
        out["pick"] = picked.get("pickChar") or {}
    data = hub.query("mutation($t: ID!, $s: ID!) { enter(tableId: $t, session: $s) }",
                     {"t": table_id, "s": session})
    entered = data.get("enter") or {}
    entered.setdefault("session", session)
    return entered


def say(hub: TrpgHub, table_id: str, text: str, seat: str = "", session: str = "") -> dict:
    session = session or session_id(hub, table_id)
    query = ("mutation($t: ID!, $x: String!, $ss: ID!, $st: ID) "
             "{ say(tableId: $t, session: $ss, text: $x, seat: $st) }")
    variables = {"t": table_id, "x": text, "ss": session}
    if seat:
        variables["st"] = seat
    return hub.query(query, variables)


def act(hub: TrpgHub, table_id: str, action: str, *, seat: str = "",
        target: str = "", attr: str = "", dc: int = 0, note: str = "",
        session: str = "") -> dict:
    session = session or session_id(hub, table_id)
    args = "$t: ID!, $a: String!, $ss: ID!" + (", $s: ID" if seat else "")
    call = "act(tableId: $t, action: $a, session: $ss"
    variables = {"t": table_id, "a": action, "ss": session}
    if seat:
        call += ", seat: $s"
        variables["s"] = seat
    if target:
        call += ", target: $tg"
        args += ", $tg: ID"
        variables["tg"] = target
    if attr:
        call += ", attr: $at"
        args += ", $at: String"
        variables["at"] = attr
    if dc:
        call += ", dc: $dc"
        args += ", $dc: Int"
        variables["dc"] = int(dc)
    if note:
        call += ", note: $nt"
        args += ", $nt: String"
        variables["nt"] = note
    call += ")"
    return hub.query("mutation(" + args + ") { " + call + " }", variables)
