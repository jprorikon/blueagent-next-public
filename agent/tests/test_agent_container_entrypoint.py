import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agent.tools.agent_container_entrypoint import load_env_files, main


class AgentContainerEntrypointTests(unittest.TestCase):
    def test_env_files_load_in_order_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            first = root / "first.env"
            second = root / "second.env"
            first.write_text(
                "# comment\nFIRST=one\nSHARED=old\nQUOTED=\"two words\"\n",
                encoding="utf-8",
            )
            second.write_text("SHARED='new'\nEMPTY=\nINVALID\n", encoding="utf-8")
            environ = {"UNCHANGED": "yes"}
            with mock.patch("sys.stdout.write") as output:
                load_env_files([str(first), str(root / "missing.env"), str(second)], environ)
            self.assertEqual(environ, {
                "UNCHANGED": "yes", "FIRST": "one", "SHARED": "new",
                "QUOTED": "two words", "EMPTY": "",
            })
            output.assert_not_called()

    def test_main_loads_mounted_secrets_then_execs_agent(self):
        def fake_load(paths, environ):
            self.assertEqual(paths[-2:], ["/run/trpg-seat.env", "/run/app-secrets/agent.env"])
            self.assertEqual(paths[:-2], sorted(paths[:-2]))
            environ["BLUEAGENT_CONTAINER_PORT"] = "18791"

        with mock.patch("agent.tools.agent_container_entrypoint.load_env_files", fake_load), \
                mock.patch("agent.tools.agent_container_entrypoint.os.execvp") as execvp, \
                mock.patch.dict(os.environ, {}, clear=True):
            main()
        execvp.assert_called_once_with(
            "python", ["python", "-m", "agent.run", "--host", "0.0.0.0", "--port", "18791"])


if __name__ == "__main__":
    unittest.main()
