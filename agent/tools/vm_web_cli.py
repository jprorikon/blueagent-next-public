#!/usr/bin/env python3
"""她的「看网页」工具（跑在自己的 VM 裡）：`web text|shot|links|eval`

為什麼要有這支：VM 裡只裝了瀏覽器本體，Playwright 的 python 包與入口都沒有 ⇒
她根本不知道怎麼開網頁（實測 import playwright 失敗）。給她一支固定入口，
不用每次自己拼 playwright 的樣板，也不會因為忘記關瀏覽器而卡住。
"""

from __future__ import annotations

import argparse
import sys


def _open(url: str, wait_ms: int, timeout_ms: int):
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    return playwright, browser, page


def main() -> int:
    parser = argparse.ArgumentParser(prog="web", description="网页を読む／撮る")
    parser.add_argument("action", choices=["text", "shot", "links", "eval"])
    parser.add_argument("url")
    parser.add_argument("target", nargs="?", default="", help="shot 的輸出檔／eval 的 JS")
    parser.add_argument("--chars", type=int, default=4000, help="text 最多印幾個字")
    parser.add_argument("--wait", type=int, default=800, help="載入後再等幾毫秒（JS 站要 2000+）")
    parser.add_argument("--timeout", type=int, default=30000)
    parser.add_argument("--full", action="store_true", help="shot：整頁（不只視窗）")
    args = parser.parse_args()

    playwright = browser = None
    try:
        playwright, browser, page = _open(args.url, args.wait, args.timeout)
        if args.action == "text":
            body = page.inner_text("body")
            print("\n".join(line.strip() for line in body.splitlines() if line.strip())[: args.chars])
        elif args.action == "shot":
            out = args.target or "page.png"
            page.screenshot(path=out, full_page=bool(args.full))
            print(f"saved: {out}")
        elif args.action == "links":
            rows = page.eval_on_selector_all(
                "a[href]", "els => els.slice(0, 40).map(e => [e.innerText.trim().slice(0,40), e.href])")
            for label, href in rows:
                print(f"- {label or '(no text)'} → {href}")
        else:
            print(page.evaluate(args.target or "document.title"))
        return 0
    except Exception as exc:                                  # noqa: BLE001 - 錯誤要能回給她看
        print(f"web: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 2
    finally:
        try:
            if browser is not None:
                browser.close()
            if playwright is not None:
                playwright.stop()
        except Exception:                                     # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
