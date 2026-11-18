#!/usr/bin/env python3
"""Firecrawl 的 MCP 伺服器（stdio）——把 Mac mini 上的自架 Firecrawl 當「外部能力」接給她。

為什麼要這一支：container 裡沒有 node，跑不了 `npx firecrawl-mcp`；而 Firecrawl 自架版是
純 REST（`POST /v1/scrape|search|map|crawl`）。所以這裡做一個**協議外殼**：對內用 MCP
（`initialize` / `tools/list` / `tools/call`），對外用 REST 打 Mac mini。

環境變數：
    FIRECRAWL_URL   預設 http://localhost.local:3002
    FIRECRAWL_API_KEY 自架版固定 fc-self-hosted（要帶 Bearer）
    FIRECRAWL_TIMEOUT_S 預設 60

⚠️ 已知限制（舊橋實測就如此，不是壞了）：自架版沒填 OpenAI key ⇒ `formats:["summary"]` 與
aiExtract 會 UNKNOWN_ERROR。markdown / html / links / search / map / crawl 都不受影響。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# 直接 `python3 agent/tools/mcp_firecrawl_server.py` 啟動時，sys.path 只有 tools/；
# 把倉庫根放回去，才能沿用全站同一套公網 URL 檢查。
ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.adapter.netguard import blocked_reason  # noqa: E402

BASE = str(os.environ.get("FIRECRAWL_URL") or "http://localhost.local:3002").rstrip("/")
#: 備援（mDNS 名字解析不了時用 IP；DHCP 會漂，所以兩邊都留）
FALLBACK = str(os.environ.get("FIRECRAWL_FALLBACK_URL") or "").rstrip("/")
KEY = str(os.environ.get("FIRECRAWL_API_KEY") or "fc-self-hosted")
TIMEOUT = float(os.environ.get("FIRECRAWL_TIMEOUT_S") or 60)

TOOLS = [
    {
        "name": "firecrawl_scrape",
        "description": "抓一個網頁，回 markdown（可指定多種格式）。問「這個連結寫什麼」時用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓的網址"},
                "formats": {"type": "array", "items": {"type": "string"},
                            "description": "預設 [markdown]" },
                "only_main_content": {"type": "boolean", "description": "預設 true"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "firecrawl_search",
        "description": "用網路搜尋（回標題／網址／摘要）。查資料、找來源時用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "預設 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "firecrawl_map",
        "description": "列出一個網站有哪些頁面（只回網址清單，不抓內容）。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"},
                           "limit": {"type": "integer", "description": "預設 50"}},
            "required": ["url"],
        },
    },
    {
        "name": "firecrawl_crawl",
        "description": "多頁爬取（從一個網址往下走）。慢，只有明確要整站時才用。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"},
                           "limit": {"type": "integer", "description": "預設 5"}},
            "required": ["url"],
        },
    },
]


def _post_once(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{base}/v1/{path.lstrip('/')}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _post(path: str, body: dict) -> dict:
    """主 URL 掛了（尤其 `.local` 解析不到）就換備援 URL 再試一次。"""
    try:
        return _post_once(BASE, path, body)
    except urllib.error.URLError as exc:
        if FALLBACK and FALLBACK != BASE:
            return _post_once(FALLBACK, path, body)
        raise exc


def _text(body) -> str:
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False)[:20000]


def _public_url(args: dict) -> tuple[str, dict | None]:
    """只把公網 URL 交給 Firecrawl；內網／回環／解析失敗一律在送出前擋下。

    這道閘覆蓋起始 URL；抓取後的重定向與子資源仍要由 Firecrawl 服務端自己的網路策略把關。
    """
    url = str(args.get("url") or "").strip()
    if not url:
        return "", {"isError": True, "content": [{"type": "text", "text": "url 是必填"}]}
    reason = blocked_reason(url)
    if reason:
        return "", {"isError": True, "content": [{"type": "text",
                                                 "text": f"url_blocked:{reason}"}]}
    return url, None


def run_tool(name: str, args: dict) -> dict:
    args = dict(args or {})
    if name in {"firecrawl_scrape", "firecrawl_map", "firecrawl_crawl"}:
        url, error = _public_url(args)
        if error is not None:
            return error
    if name == "firecrawl_scrape":
        body = {"url": url,
                "formats": args.get("formats") or ["markdown"],
                "onlyMainContent": bool(args.get("only_main_content", True))}
        data = _post("scrape", body)
        page = (data.get("data") or {}) if isinstance(data, dict) else {}
        text = str(page.get("markdown") or page.get("html") or "") or _text(data)
        meta = {key: page.get(key) for key in ("title", "url", "description") if page.get(key)}
        return {"content": [{"type": "text", "text": text[:20000]},
                            {"type": "text", "text": "meta=" + json.dumps(meta, ensure_ascii=False)}]}
    if name == "firecrawl_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "query 是必填"}]}
        data = _post("search", {"query": query, "limit": int(args.get("limit") or 5)})
        hits = (data.get("data") or []) if isinstance(data, dict) else []
        lines = []
        for hit in hits[:20]:
            if not isinstance(hit, dict):
                continue
            lines.append(f"- {hit.get('title') or '(無標題)'}｜{hit.get('url') or ''}\n"
                         f"  {str(hit.get('description') or hit.get('markdown') or '')[:200]}")
        return {"content": [{"type": "text", "text": "\n".join(lines) or "（沒有結果）"}]}
    if name == "firecrawl_map":
        data = _post("map", {"url": url, "limit": int(args.get("limit") or 50)})
        links = (data.get("links") or []) if isinstance(data, dict) else []
        return {"content": [{"type": "text", "text": "\n".join(
            str(link if isinstance(link, str) else link.get("url") or link) for link in links[:200])}]}
    if name == "firecrawl_crawl":
        data = _post("crawl", {"url": url, "limit": int(args.get("limit") or 5)})
        pages = (data.get("data") or []) if isinstance(data, dict) else []
        lines = [f"## {page.get('metadata', {}).get('sourceURL') or page.get('url') or ''}\n"
                 f"{str(page.get('markdown') or '')[:1500]}" for page in pages if isinstance(page, dict)]
        return {"content": [{"type": "text", "text": "\n\n".join(lines) or _text(data)}]}
    return {"isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}


def handle(message: dict) -> dict | None:
    method = str(message.get("method") or "")
    rid = message.get("id")
    try:
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "firecrawl-bridge", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = run_tool(str(params.get("name") or ""), params.get("arguments") or {})
        elif method in {"notifications/initialized", "ping"}:
            result = {}
        else:
            raise ValueError(f"unknown method: {method}")
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"isError": True, "content": [{"type": "text",
                                                         "text": f"firecrawl HTTP {exc.code}: {detail}"}]}}
    except Exception as exc:                                # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"isError": True,
                           "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = handle(message)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
