"""`runtime/delivery/`：出站：事件匯流排、回合結果的發件箱（未送達的回覆／分段）、按信道投遞（App／Discord／語音頻道）。

2026-11-16 從 `runtime/` 頂層收進來（架構審核 E5：頂層 60 個平鋪模組頂到棘輪）。
這個子包只依賴自己與 `runtime/fileperm.py` 這種不依賴任何 runtime 模組的葉子工具，
見 `agent/tests/test_package_layering.py::test_runtime_subpackages_do_not_reach_sideways`。
"""
