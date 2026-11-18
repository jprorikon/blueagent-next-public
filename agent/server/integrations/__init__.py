"""外部整合的啟動裝配（Discord、TRPG 席位、告警、主動發言信道）。

review.md（2026-10-25）P1：`server/main.main()` 原本 347 行，把這些整合的內部細節全攬在身上。
現在每個整合一個模組、一個 `start_*` 入口；裡面的回呼都是有名字的工廠（可單獨測）。
整合的實作一律**延遲 import**：沒開 Discord 就不會載入 TRPG／Discord 信道那些模組。
"""
