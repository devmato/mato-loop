import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from matoloop_agent.__main__ import _execute, main
from matoloop_agent.config import ConnectionConfig
from matoloop_agent.credentials import NotPairedError, PairingTemporaryError, ResolvedCredential

BACKEND_URL = "wss://mato.example/api/execution-agents/connect"
AGENT_ID = "2ea504cc-e3c6-4e0d-aa05-080328100fe8"


class CliTests(unittest.IsolatedAsyncioTestCase):
    def config(self) -> SimpleNamespace:
        return SimpleNamespace(
            data_dir=Path("/data"),
            backend_url=None,
            agent_id=None,
            allow_insecure=False,
        )

    async def test_pair_stores_credential_without_starting_agent(self) -> None:
        output = StringIO()
        with (
            patch(
                "matoloop_agent.__main__.resolve_credential",
                new=AsyncMock(return_value=ResolvedCredential(BACKEND_URL, AGENT_ID, "access-key")),
            ) as resolve,
            patch("matoloop_agent.__main__.MatoLoopAgent") as agent,
            redirect_stdout(output),
        ):
            await _execute(
                "pair",
                self.config(),
                ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
                "pairing-token",
            )

        resolve.assert_awaited_once()
        agent.assert_not_called()
        self.assertEqual(output.getvalue(), "MatoLoop Agent paired successfully\n")

    async def test_run_waits_until_pair_command_stores_credential(self) -> None:
        run_forever = AsyncMock()
        with (
            patch(
                "matoloop_agent.__main__.resolve_credential",
                new=AsyncMock(
                    side_effect=[
                        NotPairedError("not paired"),
                        ResolvedCredential(BACKEND_URL, AGENT_ID, "access-key"),
                    ]
                ),
            ) as resolve,
            patch("matoloop_agent.__main__.sleep", new=AsyncMock()) as sleep,
            patch("matoloop_agent.__main__.MatoLoopAgent") as agent,
        ):
            agent.return_value.run_forever = run_forever
            await _execute("run", self.config(), None, None)

        self.assertEqual(resolve.await_count, 2)
        sleep.assert_awaited_once_with(1)
        run_forever.assert_awaited_once()
        self.assertTrue(callable(run_forever.await_args.args[0]))

    async def test_run_retries_temporary_automatic_pairing_after_30_seconds(self) -> None:
        run_forever = AsyncMock()
        with (
            patch(
                "matoloop_agent.__main__.resolve_credential",
                new=AsyncMock(
                    side_effect=[
                        PairingTemporaryError("temporarily unavailable"),
                        ResolvedCredential(BACKEND_URL, AGENT_ID, "access-key"),
                    ]
                ),
            ) as resolve,
            patch("matoloop_agent.__main__.sleep", new=AsyncMock()) as sleep,
            patch("matoloop_agent.__main__.MatoLoopAgent") as agent,
        ):
            agent.return_value.run_forever = run_forever
            await _execute(
                "run",
                self.config(),
                ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
                "pairing-token",
            )

        self.assertEqual(resolve.await_count, 2)
        sleep.assert_awaited_once_with(30)
        run_forever.assert_awaited_once()


class CliMainTests(unittest.TestCase):
    def test_config_argument_defaults_to_native_data_directory(self) -> None:
        config = CliTests().config()
        with (
            patch.object(sys, "argv", ["matoloop-agent", "run"]),
            patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}, clear=True),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ) as load,
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
        ):
            main()

        load.assert_called_once_with(Path("/xdg/matoloop-agent/config.json"))
        execute.assert_awaited_once_with("run", config, None, None)

    def test_pair_prompts_for_complete_connection(self) -> None:
        config = CliTests().config()
        with (
            patch.object(sys, "argv", ["matoloop-agent", "pair"]),
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=[BACKEND_URL, AGENT_ID]),
            patch(
                "matoloop_agent.__main__.getpass.getpass",
                return_value="pairing-token",
            ),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ),
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
        ):
            main()

        execute.assert_awaited_once_with(
            "pair",
            config,
            ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
            "pairing-token",
        )

    def test_pair_does_not_prompt_for_explicit_backend_url(self) -> None:
        config = CliTests().config()
        with (
            patch.object(
                sys,
                "argv",
                ["matoloop-agent", "pair", "--backend-url", BACKEND_URL],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value=AGENT_ID) as prompt,
            patch(
                "matoloop_agent.__main__.getpass.getpass",
                return_value="pairing-token",
            ),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ),
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
        ):
            main()

        prompt.assert_called_once()
        execute.assert_awaited_once_with(
            "pair",
            config,
            ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
            "pairing-token",
        )

    def test_pair_prompts_for_explicit_empty_backend_url(self) -> None:
        config = CliTests().config()
        with (
            patch.object(
                sys,
                "argv",
                ["matoloop-agent", "pair", "--backend-url", ""],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=[BACKEND_URL, AGENT_ID]) as prompt,
            patch(
                "matoloop_agent.__main__.getpass.getpass",
                return_value="pairing-token",
            ),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ),
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
        ):
            main()

        self.assertEqual(prompt.call_count, 2)
        execute.assert_awaited_once_with(
            "pair",
            config,
            ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
            "pairing-token",
        )

    def test_run_pairs_automatically_only_with_complete_environment(self) -> None:
        config = CliTests().config()
        environment = {
            "MATOLOOP_AGENT_BACKEND_URL": BACKEND_URL,
            "MATOLOOP_AGENT_ID": AGENT_ID,
            "MATOLOOP_AGENT_PAIRING_TOKEN": "pairing-token",
        }
        with (
            patch.object(sys, "argv", ["matoloop-agent", "run"]),
            patch.dict(os.environ, environment, clear=True),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ),
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
        ):
            main()

        execute.assert_awaited_once_with(
            "run",
            config,
            ConnectionConfig(backendUrl=BACKEND_URL, agentId=AGENT_ID),
            "pairing-token",
        )

    def test_run_rejects_incomplete_automatic_pairing_environment(self) -> None:
        config = CliTests().config()
        environment = {
            "MATOLOOP_AGENT_BACKEND_URL": BACKEND_URL,
            "MATOLOOP_AGENT_PAIRING_TOKEN": "pairing-token",
        }
        with (
            patch.object(sys, "argv", ["matoloop-agent", "run"]),
            patch.dict(os.environ, environment, clear=True),
            patch(
                "matoloop_agent.__main__.AgentConfig.load",
                return_value=config,
            ),
            patch("matoloop_agent.__main__._execute", new=AsyncMock()) as execute,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit),
        ):
            main()

        execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
