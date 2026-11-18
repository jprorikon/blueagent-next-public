---
name: web-research
description: 委任モードでネットを調べるときに使う（自前 Firecrawl の検索・本文・サイト crawl、JS ページは VM ブラウザ）
scope: work
requires: [tool:mcp_firecrawl_firecrawl_search, tool:mcp_firecrawl_firecrawl_scrape, tool:mcp_firecrawl_firecrawl_map, tool:mcp_firecrawl_firecrawl_crawl, tool:terminal]
owner_only: false
category: web
---

# ネットで調べる（work 技能）

## いつ使う

委任モードで主人に頼まれた仕事に、外の資料（ルール、設定、文書、一般知識）が要るとき。

## 手順

1. どこにあるか分からない ⇒ `mcp_firecrawl_firecrawl_search`（タイトル／要約／URL を見る）。
2. 一つを最後まで読みたい ⇒ `mcp_firecrawl_firecrawl_scrape`（検索は要約しか返さない）。
3. サイトの構成 ⇒ `mcp_firecrawl_firecrawl_map`、複数ページが必要なら `mcp_firecrawl_firecrawl_crawl`。
4. JS がないと中身が見えないサイト ⇒ `terminal` で自分の VM の `web text "URL" --wait 2500` を使う。必要なら `web shot`／`web links`／`web eval`。
5. 報告するときは出どころの URL を言う。

## 守ること

- **「調べた」と書けるのは道具が返した内容だけ**。調べていないなら印象だと言う。
- 結果が空なら見つからなかったと言う。補わない。
- Firecrawl の要約機能は使わず、返った markdown を自分でまとめる。
