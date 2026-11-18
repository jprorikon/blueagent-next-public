"""應用層（組合根）：`bootstrap` 接線、`state` 對外的應用狀態、對話管線的各階段（`stages`）。

只有這一層與 `server/` 可以把 adapters／runtime／persona 全部接在一起；下層不准 import 這裡。
"""
