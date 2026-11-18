"""`runtime/ops/`：維運：每日資料快照、日誌輪替與每日護欄、回合遙測、告警。

2026-11-16 從 `runtime/` 頂層收進來（架構審核 E5：頂層 60 個平鋪模組頂到棘輪）。
這個子包只依賴自己與 `runtime/fileperm.py` 這種不依賴任何 runtime 模組的葉子工具，
見 `agent/tests/test_package_layering.py::test_runtime_subpackages_do_not_reach_sideways`。
"""
