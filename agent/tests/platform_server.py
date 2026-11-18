"""启动真实 App Server 并给旧 Agent 入口注入远程凭证的测试夹具。"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import threading
import unittest.mock

from app.server.config import AppConfig
from app.server.http import make_server


def start_platform(test, accounts, *, entities=()):
    """accounts 为 (id, kind, token)；entities 为 (id, token)。返回运行中的服务。"""
    temporary = tempfile.TemporaryDirectory(prefix="ba-platform-fixture-")
    test.addCleanup(temporary.cleanup)
    root = pathlib.Path(temporary.name)
    rows = [{"id": identity, "kind": kind, "display": identity,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
            for identity, kind, token in accounts]
    rows.extend({"id": identity, "kind": "entity", "display": identity,
                 "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
                for identity, token in entities)
    accounts_path = root / "accounts.json"
    accounts_path.write_text(json.dumps({"accounts": rows}), encoding="utf-8")
    server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 2.0)
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    base = f"http://127.0.0.1:{server.server_port}"
    tokens = {identity: token for identity, token in entities}
    env = unittest.mock.patch.dict(os.environ, {
        "BLUEAGENT_APP_SERVER_URL": base,
        "BLUEAGENT_APP_SERVER_TOKENS": json.dumps(tokens),
    })
    env.start()
    test.addCleanup(env.stop)
    server.test_base_url = base
    return server
