"""動作決策：牌面渲染成文字、從她說的話裡挑一個合法動作（確定性規則，不強迫她吐 JSON）。"""

from __future__ import annotations



#: 從她說的話裡認「我要做什麼」。順序＝優先級（先認攻擊，再認移動）。
#: 2026-11-09：拿掉光禿的「打」（「打ち合わせ」也會中）；「逃げる／下がる」算移動。
_ACTION_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fight", ("攻撃", "攻击", "fight", "attack", "打倒", "打つ", "叩", "斬", "斩", "射", "殴", "擊", "倒す")),
    ("search", ("探", "調べ", "サーチ", "search", "搜", "找", "見つけ", "発見")),
    ("pray", ("祈", "願", "愿", "願い")),
    ("show", ("渡す", "あげる", "見せる", "給", "分享")),
    ("use", ("使う", "飲む", "道具", "回復", "skill", "スキル")),
    ("move", ("行く", "進む", "向かう", "移動", "走", "去", "行こう", "逃げ", "下が", "退")),
)

#: 「這一手先不動」：等、看看情況、休息、交棒（以前沒有 ⇒ 她說「待とう」會被挑成第一個合法動作＝攻擊）
_PASS_WORDS: tuple[str, ...] = ("待つ", "待と", "待って", "待ち", "様子を見", "休む", "休み", "休も",
                                "見送", "パス", "何もしない", "手番を渡", "じっと", "pass", "wait",
                                "等一下", "等等", "先不")
_PASS_ACTIONS = ("end_turn", "pass", "wait")
#: 否定：關鍵字後面（跳過は／を／も）接這些 ⇒ 她說的是「不做」
_NEG_AFTER = ("しない", "しません", "せず", "ない", "ません", "ず", "やめ", "なんて")
_NEG_BEFORE = ("不", "別", "别", "没", "不要")  # lang-ok: 比對她台詞用的否定字（中文台詞也認），不進 prompt


def _negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 2):start]
    if any(before.endswith(word) for word in _NEG_BEFORE):
        return True
    tail = text[end:end + 6].lstrip("はをもにで、 ")
    if tail.startswith(_NEG_AFTER):
        return True
    # 「探さない」「調べない」：一個活用字＋ない
    return len(tail) >= 3 and tail[1:3] in {"ない", "ませ"}


def _says(text: str, words) -> bool:
    """`words` 裡有沒有哪一個在她的話裡**以肯定的意思**出現（「攻撃はしない」不算要攻擊）。"""
    lower = text.lower()
    for word in words:
        probe = word.lower()
        start = lower.find(probe)
        while start >= 0:
            if not _negated(lower, start, start + len(probe)):
                return True
            start = lower.find(probe, start + 1)
    return False


#: 引擎常用的動作別名（allowed_actions 裡可能是 `fight`／`attack`，兩種都吃）。
_ACTION_ALIASES = {
    "fight": ("fight", "attack"),
    "search": ("search", "look", "investigate"),
    "pray": ("pray", "wish"),
    "show": ("show", "give", "share"),
    "use": ("use", "item", "cast", "skill"),
    "move": ("move", "go"),
}


def render_view(view: dict, seat: str = "") -> str:
    """把牌面壓成她讀得懂的一段（欄位與現役 `agents_play.situation()` 對齊）。

    **全部日語**（review 2026-10-28：以前「第 N 輪，輪到…（你＝…）」「剛剛說話」「可用動作」
    整段繁中直接進每一手的 prompt、桌外匯報與主對話的遊戲段）。名稱也先取 `jp`，沒有才退 `cn`。
    """
    table = view.get("table") or {}
    you = view.get("you") or {}
    turn = view.get("turn") or {}
    board = view.get("board") or {}
    here = board.get("you_are") or {}
    lines = [f"第 {table.get('roundNo') or table.get('round_no') or '?'} ラウンド、"
             f"手番は {turn.get('seat')}（あなた＝{you.get('seat') or seat}）"]
    if turn.get("seconds_left") is not None:
        # 桌端每一手有 deadline；不告訴她，她會慢慢想然後超時（超時＝白掉一手）
        lines.append(f"この手の残り時間：{turn['seconds_left']} 秒")
    if you:
        lines.append(f"あなた：{you.get('name')} HP {you.get('hp')}/{you.get('max_hp')} "
                     f"記憶 {you.get('memory')}/{you.get('max_memory')} "
                     f"星貨 {you.get('star_coins')}")
        items = [item for item in (you.get("items") or []) if item]
        if items:
            lines.append("道具：" + "、".join(
                f"{_jp(item, 'name_jp', 'name_cn') or item.get('id')}×{item.get('count')}"
                for item in items))
        skills = [sk for sk in (you.get("skills") or []) if sk and (sk.get("known", True))]
        if skills:
            lines.append("技能：" + "、".join(
                f"{_jp(sk, 'name_jp', 'name_cn') or sk.get('id')}({sk.get('id')})"
                for sk in skills))
    if here:
        lines.append(f"現在地：{_jp(here, 'jp', 'cn')}（{here.get('id')}）")
        if here.get("stellaroids_left"):
            lines.append(f"このマスには星骸が {here['stellaroids_left']} 体いる")
        if here.get("wish_box"):
            lines.append("このマスには祈願箱がある")
    moves = [f"{_jp(item, 'jp', 'cn')}({item.get('to')})"
             for item in (board.get("can_go") or [])]
    # 入口的「還剩幾輪會關」——search 開的門有期限，不告訴她就會在門口來回走
    left = board.get("entrance_open_rounds_left")
    if left is not None:
        lines.append(f"入口：あと {left} ラウンドで閉まる（閉まる前に move で中へ入る）")
    if moves:
        lines.append("行ける場所：" + "、".join(moves))
    party = [f"{item.get('name')}@{_jp(item, 'where_jp', 'where_cn') or item.get('node')}"
             for item in (board.get("party") or [])]
    if party:
        lines.append("仲間：" + "、".join(party))
    says = [row for row in (view.get("log") or []) if row.get("type") in ("say", "gm_say")][-4:]
    if says:
        lines.append("さっきの発言：")
        for row in says:
            who = "GM" if row.get("seat") == "gm" else ("あなた" if row.get("seat") == seat
                                                        else str(row.get("seat")))
            lines.append(f"  {who}：「{str(row.get('text') or '')[:80]}」")
    log = (view.get("log") or [])[-6:]
    if log:
        lines.append("最近の出来事：" + " / ".join(
            str(row.get("text") or row.get("note") or row.get("type"))[:60] for row in log))
    goal = view.get("your_goal") or {}
    if goal:
        lines.append(f"あなたの秘めた願い：{goal.get('title') or goal.get('id') or ''}")
    acts = view.get("allowed_actions") or []
    if acts:
        lines.append("使える行動：" + "、".join(acts))
    return "\n".join(lines)


def _jp(row: dict, jp_key: str, cn_key: str) -> str:
    """日文名優先，沒有才退中文名（引擎資料兩種都可能給）。"""
    return str(row.get(jp_key) or row.get(cn_key) or "")


def choose_action(view: dict, text: str) -> dict:
    """從她那句話挑一個**合法**動作。

    回 `{"action": ..., "target": ...}`；`target` 只在 move（格子）／fight（目標）時給。
    2026-11-09 檢查後：否定（「攻撃はしない」）不算要做；「待とう／休みたい」＝這手先不動；
    什麼都沒說清楚時**不替她出手攻擊**（以前挑第一個非交棒的合法動作，常常就是 fight）。
    """
    allowed = [str(item) for item in (view.get("allowed_actions") or [])]
    said = str(text or "")
    board = view.get("board") or {}
    moves = [str((item or {}).get("to") or "") for item in (board.get("can_go") or [])]
    #: 每個可去格子的「怎麼叫她認得」：id ＋ 中／日文名（她說「アモールへ」也要認得）
    move_names = [(str((item or {}).get("to") or ""),
                   " ".join(str((item or {}).get(key) or "")
                            for key in ("to", "cn", "jp", "name")))
                  for item in (board.get("can_go") or [])]

    def legal(kind: str) -> str:
        for alias in _ACTION_ALIASES.get(kind, (kind,)):
            for name in allowed:
                if name.lower() == alias:
                    return name
        return ""

    def named_place() -> str:
        return next((node for node, names in move_names
                     if node and any(part and _says(said, (part,)) for part in names.split())), "")

    for kind, words in _ACTION_WORDS:
        if not _says(said, words):
            continue
        action = legal(kind)
        if not action:
            continue
        if kind == "move":
            # 她的話裡提到哪個格子就去那裡；沒提到就去第一個可去的
            return {"action": action, "target": named_place() or (moves[0] if moves else "")}
        if kind == "fight":
            target = str((board.get("you_are") or {}).get("stellaroid_uid") or "")
            return {"action": action, "target": target or None}
        return {"action": action, "target": None}

    # 她只是**提到一個地名**（「アモールへ」）也算要移動——說出要去哪就是想去那
    place = named_place()
    if place and legal("move"):
        return {"action": legal("move"), "target": place}

    passing = next((name for name in allowed if name.lower() in _PASS_ACTIONS), "end_turn")
    if _says(said, _PASS_WORDS):
        return {"action": passing, "target": None}

    # 沒有明確意圖：挑第一個**不是交棒、也不是攻擊**的合法動作；只剩攻擊就交棒（不替她動手打）
    for name in allowed:
        lowered = name.lower()
        if lowered in _PASS_ACTIONS or lowered in _ACTION_ALIASES["fight"]:
            continue
        if lowered in _ACTION_ALIASES["move"] and moves:
            return {"action": name, "target": moves[0]}
        return {"action": name, "target": None}
    return {"action": passing, "target": None}
