"""管線層：`Stage` 是插件，`Pipeline` 是宣告，`blueprints` 是形狀表。

管線只透過 `ba.ports` 的協議與舊世界接縫；不 import `deps`，不自己起執行緒。
"""
