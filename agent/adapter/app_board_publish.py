"""Agent 公开活动 → App Server 公共区的显式发布器。

为什么有这一支（2026-11-07）：S4-e 把公共区真值搬到 App Server 后，旧的「实时读取 activity 投影」
被删掉，设计改成「Agent 显式发布」（S4-e 切换后的同步边界，见 `docs/ADR-app-social-platform-20261105.md` §6）。但没有任何东西真的去发布——
生活回路的白名单里没有 `post`，也没有收尾钩子——于是从 11/6 起角色一条帖子都没有。

这里补上「显式发布」的那一半，规则沿用 M2 的投影（`612d502` 的 `project_activity`）：
- 一件活动（同一个 `activity_id`）只发**最后一步**，而且那一步要是 public、是「…完了：」收尾；
  最终步是 private 或还没做完 ⇒ 不发。
- 她对人说的话不是帖子（`heartbeat`／通话／TRPG 报告）：贴 ≠ 对话（PLAN §2.4′）。
- 以该角色自己的 App Server 账号发（`BLUEAGENT_APP_SERVER_TOKENS`），`source_ref=activity:<角色>:<id>`；
  切换前已迁移的投影用的是同一个 source_ref ⇒ 服务端回 409，视为「已经在公共区」，不会重复。
- 定期扫描而不是挂在写日志的路径上：App Server 暂时不通、限流、Agent 重启都只是「下一轮再补」，
  不会拖慢或卡住生活回路。

不发图（活动日志里的 media 是 Agent 侧的旧 id，App Server 只认自己账号上传的 media）；
也不发「作品」整篇（最长 2000 字的卡片会把首页撑爆，等卡片有折叠再说）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import re
import threading
import time

from agent.adapter.app_protocol import BoardError

#: 她对人说的话、通话：不是公开活动
SKIP_KINDS = {"heartbeat", "voice", "trpg-report", "call", "call-log", "voice-call"}
MAX_BODY = 2000
#: 「ミラ『タイトル』2/3 完了：」——收尾那一步的外壳（与 life_step 写日志的格式对齐）。
#: ⚠️ 开头必须**锚死**（`^[^「：]*`＝角色名里不许出现「或：），不能用 `^.*?`：
#: 那样会回溯越过步骤标头，把「アスター『整理』2/3 まとめる：「下書き」完了：次は清書」这种
#: **中间步**当成收尾提前发出去（2026-11-07 REVIEW #6，真机日志实见），
#: 而同一 source_ref 一旦发过，真正收尾那一步就再也发不出来了。
_FINISH = re.compile(r"^[^「：]*[「\"][^」\"]*[」\"](?:\s*\d+\s*/\s*\d+)?\s*(?:完了|完成了)\s*[：:]\s*")
_SOURCE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,200}")
#: 这些业务错误＝公共区里已经有这一件（切换前迁移的投影、或被主人删掉了）⇒ 不再重试
_SETTLED = {"source_ref_conflict", "source_ref_deleted"}


def finished_body(text: str) -> str:
    """是收尾那一步才回正文（去掉「角色『活动』n/m 完了：」外壳）；不是收尾回空字串。"""
    raw = str(text or "").strip()
    if not _FINISH.match(raw):
        return ""
    return _FINISH.sub("", raw, count=1).strip()


#: 帖子正文里的 markdown 记号（她的成品步常带表格／小标题；公共区是纯文本流）
_MD_HEAD = re.compile(r"^\s*#{1,6}\s*")
_MD_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$")


def clean_post_body(text: str) -> str:
    """把成品步里的 markdown 记号清掉（表 → 「・a、b」），公共区只吃纯文本。

    owner 2026-11-08 实见：「###観測表|日時|活動|…」这种表格原样贴上去了。
    """
    out: list[str] = []
    for raw in str(text or "").replace("\r", "").splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if _MD_RULE.match(line):                 # |---|---| 分隔行整行丢掉
            continue
        line = _MD_HEAD.sub("", line)
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:                      # 表格行 ⇒ 一条「・a、b、c」
            out.append("・" + "、".join(cells))
        elif cells:
            out.append(cells[0])
    body = "\n".join(out)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def compose_post(activity: str, body: str) -> str:
    """帖子的组成＝「何の話か」一行 ＋ 正文。

    owner 2026-11-08：「感觉有点没头没尾的」——只贴收尾那句，读者不知道是哪件事。
    活动名放最前面当标题，正文保持她的原话（只清 markdown）。
    """
    title = " ".join(str(activity or "").split())[:40]
    text = clean_post_body(body)
    if not title:
        return text
    return f"【{title}】\n{text}" if text else f"【{title}】"


def _read_rows(path: pathlib.Path) -> list[dict]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        else:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def activity_files(logs_root, entity: str, since: float) -> list[pathlib.Path]:
    """活档＋还在回看窗口里的日档（`daily/activity-<角色>-YYYYMMDD.jsonl[.gz]`）。"""
    root = pathlib.Path(logs_root)
    first_day = time.strftime("%Y%m%d", time.localtime(max(0.0, since - 86400)))
    found = []
    for path in sorted((root / "daily").glob(f"activity-{entity}-*.jsonl*")):
        stamp = path.name[len(f"activity-{entity}-"):][:8]
        if stamp.isdigit() and stamp >= first_day:
            found.append(path)
    live = root / f"activity-{entity}.jsonl"
    if live.is_file():
        found.append(live)
    return found


def publishable(logs_root, entity: str, since: float) -> list[dict]:
    """这个角色在 `since` 之后**做完**的公开活动：每件一条 {source_ref, at, body, activity}。"""
    groups: dict[str, list[dict]] = {}
    for path in activity_files(logs_root, entity, since):
        for row in _read_rows(path):
            activity_id = str(row.get("activity_id") or "").strip()
            if not activity_id or str(row.get("entity") or entity) != entity:
                continue
            groups.setdefault(activity_id, []).append(row)
    out = []
    for activity_id, rows in groups.items():
        last = max(rows, key=lambda row: (int(row.get("step") or 0), float(row.get("at") or 0.0)))
        at = float(last.get("at") or 0.0)
        if at < since or last.get("visibility") != "public":
            continue
        if str(last.get("kind") or "") in SKIP_KINDS:
            continue
        body = finished_body(last.get("text") or "")
        if not body or not _SOURCE_PART.fullmatch(activity_id):
            continue
        body = compose_post(str(last.get("activity") or ""), body)
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY - 1].rstrip() + "…"
        out.append({"source_ref": f"activity:{entity}:{activity_id}", "at": at, "body": body,
                    "activity": str(last.get("activity") or "")})
    out.sort(key=lambda item: item["at"])
    return out


def mutation_id(source_ref: str) -> str:
    """同一件活动重试永远是同一个幂等键（重启、超时重发都不会多一帖）。"""
    return "pub_" + hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:40]


class PublicActivityPublisher:
    """定期把各角色做完的公开活动发到 App Server 公共区；发过的记在本地，不重复打服务端。"""

    def __init__(self, logs_root, clients: dict, state_path, *, lookback_s: float = 3 * 86400,
                 interval_s: float = 300.0, clock=time.time, log=None) -> None:
        self.logs_root = pathlib.Path(logs_root)
        self.clients = dict(clients)
        self.state_path = pathlib.Path(state_path)
        self.lookback_s = float(lookback_s)
        self.interval_s = max(30.0, float(interval_s))
        self._clock = clock
        self._log = log or (lambda message: print(f"  {message}", flush=True))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(key): value for key, value in (data.get("published") or {}).items()
                if isinstance(value, dict)}

    def _save(self, since: float) -> None:
        # 只留回看窗口里的，档案不会无限长
        keep = {key: value for key, value in self._done.items()
                if float(value.get("at") or 0.0) >= since - 86400}
        self._done = keep
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"published": keep}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def sweep(self) -> dict:
        """扫一轮；回 {posted, settled, skipped, failed}。单个角色限流／出错只影响它自己这一轮。"""
        with self._lock:
            since = float(self._clock()) - self.lookback_s
            counts = {"posted": 0, "settled": 0, "skipped": 0, "failed": 0}
            for entity, client in sorted(self.clients.items()):
                for item in publishable(self.logs_root, entity, since):
                    ref = item["source_ref"]
                    if ref in self._done:
                        counts["skipped"] += 1
                        continue
                    try:
                        reply = client.board_call("post", {
                            "text": item["body"], "media": [],
                            "client_mutation_id": mutation_id(ref), "source_ref": ref,
                            # ⑦ REVIEW 2026-11-07：帖子时间用活动**实际做完**的时刻，
                            # 不然回看补发／停机后迟发的帖子全变成「刚发」（都显示 n分前，
                            # 「今日の投稿」一次跳几十条）。服务端会夹在回看窗口内。
                            "occurred_at": float(item["at"] or 0.0),
                        })
                    except BoardError as exc:
                        code = str(getattr(exc, "code", "") or exc)
                        if code in _SETTLED or getattr(exc, "status", 0) == 409:
                            self._done[ref] = {"at": item["at"], "post": "", "state": code}
                            counts["settled"] += 1
                            continue
                        counts["failed"] += 1
                        self._log(f"board-publish[{entity}]: {ref} 未发出（{code}），下一轮再试")
                        break                          # 限流／权限问题：这个角色这一轮先停
                    except (OSError, RuntimeError, ValueError, TypeError) as exc:
                        counts["failed"] += 1
                        self._log(f"board-publish[{entity}]: App Server 不通（{type(exc).__name__}），"
                                  "下一轮再试")
                        break
                    post = (reply or {}).get("post") or {}
                    self._done[ref] = {"at": item["at"], "post": str(post.get("id") or ""),
                                       "state": "posted"}
                    counts["posted"] += 1
                    self._log(f"board-publish[{entity}]: 「{item['activity']}」→ {post.get('id') or '?'}")
            self._save(since)
            return counts

    def start(self, first_delay_s: float = 20.0) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            delay = max(0.0, float(first_delay_s))
            while not self._stop.wait(delay):
                try:
                    self.sweep()
                except Exception as exc:              # noqa: BLE001 - 发布失败不能拖垮 Agent
                    self._log(f"board-publish: 本轮失败（{type(exc).__name__}: {exc}）")
                delay = self.interval_s

        self._thread = threading.Thread(target=run, name="board-publish", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)


def build_board_publisher(state, env=None) -> PublicActivityPublisher | None:
    """**預設不開**（owner 2026-11-08：發帖要跟「事情收束」解耦）。

    以前：一件活動**收尾**那一步寫完 ⇒ 自動把收尾那句貼上去（發帖依附於收束）。
    現在：發帖是她自己的動作（自主動作用 `mcp_board_post`，1 天 3 本、按次計價），
    這條自動路只留給「想退回舊行為」的情況 ⇒ `BLUEAGENT_BOARD_AUTOPUBLISH=1` 才開。
    """
    from agent.adapter.app_server import AppServerClient, _tokens

    env = os.environ if env is None else env
    if str(env.get("BLUEAGENT_BOARD_AUTOPUBLISH") or "0").strip().lower() \
            not in {"1", "on", "true", "yes"}:
        return None
    base_url = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if not base_url or not tokens:
        return None
    data = pathlib.Path(state.app.data_dir)
    lookback_days = float(env.get("BLUEAGENT_BOARD_PUBLISH_LOOKBACK_DAYS") or 3)
    return PublicActivityPublisher(
        data / "logs", {entity: AppServerClient(base_url, token) for entity, token in tokens.items()},
        data / "app-adapter" / "board-published.json",
        lookback_s=lookback_days * 86400, log=getattr(state, "_log_command", None))


__all__ = ["PublicActivityPublisher", "activity_files", "build_board_publisher",
           "clean_post_body", "compose_post", "finished_body", "mutation_id", "publishable"]
