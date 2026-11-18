"""启动独立 App Server：python3 -m app.server。"""

from __future__ import annotations

from app.server.config import AppConfig
from app.server.http import make_server


def main() -> int:
    server = make_server(AppConfig.from_env())
    host, port = server.server_address[:2]
    print(f"BlueAgent App Server · http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
