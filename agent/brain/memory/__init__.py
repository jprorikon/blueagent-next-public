"""記憶核心：事實模型與披露規則（`facts`）、原文落盤（`fact_store`）、
從一輪對話抽事實（`fact_rules`）、記憶體實作（`in_memory`）。

只依賴 `domain`；索引後端（SQLite／Hindsight）在 `adapters/`，它們依賴這裡，反過來不行。
"""
