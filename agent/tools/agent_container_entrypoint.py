"""Load mounted Docker env files, then exec the Agent without logging secrets."""

from __future__ import annotations

import glob
import os
import pathlib
import sys
from collections.abc import Iterable, MutableMapping


def load_env_files(paths: Iterable[str], environ: MutableMapping[str, str]) -> None:
    """Load env-file values in order; later files override earlier files."""
    for path in paths:
        try:
            lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                environ[key] = value


def main() -> None:
    paths = sorted(glob.glob("/run/blueagent-secrets/*.env"))
    paths += ["/run/trpg-seat.env", "/run/app-secrets/agent.env"]
    load_env_files(paths, os.environ)

    log_path = os.environ.get("BLUEAGENT_STARTUP_LOG")
    if log_path:
        log = open(log_path, "a", buffering=1, encoding="utf-8")
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
    port = os.environ.get("BLUEAGENT_CONTAINER_PORT", "18790")
    os.execvp("python", ["python", "-m", "agent.run", "--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    main()
