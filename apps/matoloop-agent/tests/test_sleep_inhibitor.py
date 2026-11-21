import asyncio
import errno
import os
import sys
import unittest
from unittest.mock import Mock, patch

from matoloop_agent.sleep_inhibitor import SleepInhibitor


class SleepInhibitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_work_and_new_work_during_grace_share_one_assertion(self):
        release = Mock()
        acquire = Mock(return_value=release)
        inhibitor = SleepInhibitor(acquire)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "call_later") as schedule:
            with inhibitor.hold():
                with inhibitor.hold():
                    acquire.assert_called_once_with()
                schedule.assert_not_called()
            schedule.assert_called_once_with(300, inhibitor.close)

            with inhibitor.hold():
                schedule.return_value.cancel.assert_called_once_with()
                release.assert_not_called()
            self.assertEqual(schedule.call_count, 2)
            acquire.assert_called_once_with()

            schedule.call_args.args[1]()
            release.assert_called_once_with()
            inhibitor.close()
            release.assert_called_once_with()

    async def test_denied_assertion_does_not_interrupt_work_and_can_be_retried(self):
        release = Mock()
        acquire = Mock(side_effect=[PermissionError("denied"), release])
        inhibitor = SleepInhibitor(acquire)
        with self.assertLogs("matoloop_agent.sleep_inhibitor", level="WARNING"), inhibitor.hold():
            release.assert_not_called()
        with inhibitor.hold():
            self.assertEqual(acquire.call_count, 2)
        inhibitor.close()
        release.assert_called_once_with()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux logind adapter")
class LinuxSleepInhibitorTests(unittest.TestCase):
    def test_permission_denial_falls_back_to_idle_only(self):
        from matoloop_agent.platform_linux import LinuxPlatform

        lib = Mock()
        lib.sd_bus_open_system.return_value = 0
        lib.sd_bus_set_method_call_timeout.return_value = 0
        lib.sd_bus_call_method.side_effect = [-errno.EACCES, 0]
        lib.sd_bus_message_read.return_value = 1
        with (
            patch.dict(os.environ, {"MATOLOOP_AGENT_CONTAINER": "0"}),
            patch("matoloop_agent.platform_linux.ctypes.CDLL", return_value=lib),
            patch("matoloop_agent.platform_linux.os.dup"),
        ):
            LinuxPlatform().inhibit_idle_sleep()
        self.assertEqual(
            [request.args[8] for request in lib.sd_bus_call_method.call_args_list],
            [b"idle:sleep", b"idle"],
        )
