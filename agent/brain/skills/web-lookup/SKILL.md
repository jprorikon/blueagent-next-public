---
name: web-lookup
description: ウェブページを見る・調べものをするときに使う（リンクを読む、検索する、サイトのページ一覧）
scope: chat
requires: [tool:mcp_firecrawl_firecrawl_scrape, tool:mcp_firecrawl_firecrawl_search, tool:mcp_firecrawl_firecrawl_map]
owner_only: false
category: web
---

# ウェブを見る・調べる（chat 技能）

## いつ使う

- 相手がリンクを貼って「これ何が書いてある？」「ちょっと見て」と言ったとき
- いまの事実（ニュース・値段・お知らせ）が必要で、自分の記憶にないとき
- あるサイトにどんなページがあるか知りたいとき

## 手順

1. **リンクが一つ** → `mcp_firecrawl_firecrawl_scrape`（`url`、既定で markdown が返る）。
2. **どこを見ればいいか分からない** → `mcp_firecrawl_firecrawl_search`（`query`、`limit` は 3〜5）。
3. **サイトの構成を知りたい** → `mcp_firecrawl_firecrawl_map`（`url`、`limit` は 20）。
4. 答えるときは**取れた内容だけ**を話し、出どころの URL を言う。取れなければ取れなかったと言う。

## 守ること

- **ページの中身を作らない**：空やエラーなら正直に言う。常識で埋めない。
- **見たふりをしない**：トップページしか取っていないのに「サイト全部見た」と言わない。
- **要約機能は使えない**（自前の Firecrawl は LLM につながっていない）：`formats:["summary"]` は使わず、markdown を自分でまとめる。

## 失敗したとき

道具が `mcp_error`／`isError` を返す ⇒「サイトが取れなかった（接続か、相手がロボットを拒否している）」と言い、必要なら別の出どころにするか聞く。
