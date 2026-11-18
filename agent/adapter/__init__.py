"""BlueAgent 外部渠道接入器；实现不隶属于手机 App 或 brain 主控包。

只有组合根可以接入外部渠道；domain／runtime／router／pipelines 不得反向依赖本包。
TRPG、Discord、App Server、语音、记忆与 MCP 等 canonical 实现统一住在这里。
"""

from __future__ import annotations

__all__: list[str] = []
