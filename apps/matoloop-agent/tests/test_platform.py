import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from matoloop_agent.config import AgentConfig
from matoloop_agent.connection import MatoLoopAgent
from matoloop_agent.errors import AgentError
from matoloop_agent.platform import RuntimeServices
from matoloop_agent.protocol import Request

AGENT_ID = "2ea504cc-e3c6-4e0d-aa05-080328100fe8"
REQUEST_ID = "bbc46aa8-698d-4554-92c2-1cd5c6cc33a6"


class FakePlatform:
    platform_id = "win32" if sys.platform == "win32" else "test"

    def maintenance_running(self, config):
        return False

    def inhibit_idle_sleep(self):
        raise AssertionError("A system info query must not inhibit sleep")

    def computer_name(self):
        return "test-computer"

    def system_info(self, current_directory, filesystem):
        return {"agentVersion": "1.2.3", "platform": self.platform_id}


class FakeFilesystem:
    def resolve_directory(self, path):
        return {"path": path}


class PlatformBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_runtime_controls_hello_and_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AgentConfig.model_validate(
                {
                    "homeDirectory": Path(temporary),
                    "backendUrl": "ws://localhost/api/execution-agents/connect",
                    "agentId": AGENT_ID,
                    "allowInsecure": True,
                }
            )
            runtime = RuntimeServices(
                platform=FakePlatform(),
                filesystem=FakeFilesystem(),
                cache=SimpleNamespace(),
                process=SimpleNamespace(),
                terminal=SimpleNamespace(),
                search=SimpleNamespace(),
                browser=SimpleNamespace(),
                openscad=SimpleNamespace(),
                transfer=SimpleNamespace(),
                operations=("system.info",),
            )
            agent = MatoLoopAgent(config, "secret", runtime=runtime)

            hello = agent.hello()
            self.assertEqual(hello.platform, FakePlatform.platform_id)
            self.assertEqual(hello.computer_name, "test-computer")
            self.assertEqual(hello.operations, ["system.info"])
            self.assertEqual(
                await agent.dispatch(
                    Request(
                        type="request",
                        id=REQUEST_ID,
                        operation="system.info",
                        arguments={},
                    )
                ),
                {"agentVersion": "1.2.3", "platform": FakePlatform.platform_id},
            )
            with self.assertRaises(AgentError) as raised:
                await agent.dispatch(
                    Request(
                        type="request",
                        id=REQUEST_ID,
                        operation="filesystem.stat",
                        arguments={"path": "/tmp"},
                    )
                )
            self.assertEqual(raised.exception.code, "operation.unsupported")

    def test_common_modules_import_without_linux_implementation(self) -> None:
        script = """
import matoloop_agent
import matoloop_agent.__main__
import matoloop_agent.connection
assert 'matoloop_agent.platform_linux' not in sys.modules
assert 'matoloop_agent.credentials_linux' not in sys.modules
assert 'matoloop_agent.filesystem' not in sys.modules
"""
        subprocess.run(
            [sys.executable, "-c", "import sys\n" + script],
            check=True,
            cwd=Path(__file__).parent.parent,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )


if __name__ == "__main__":
    unittest.main()
