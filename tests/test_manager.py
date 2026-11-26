import unittest
from types import SimpleNamespace
from unittest.mock import patch
import manager

class ManagerTests(unittest.TestCase):
    def test_rejects_arbitrary_units(self):
        for unit in ("ssh.service", "--help", "../secret", "iptv-gateway.service; id"):
            with patch("manager.run_fixed") as run:
                with self.assertRaises(ValueError):
                    manager.service_status(unit)
                with self.assertRaises(ValueError):
                    manager.journal_summary(unit)
                run.assert_not_called()

    def test_minutes_bounds(self):
        for value in (0, -1, 1441, True, "60"):
            with self.assertRaises(ValueError):
                manager.journal_summary("iptv-admin.service", value)

    def test_no_raw_log_messages(self):
        raw = '{"PRIORITY":"3","__REALTIME_TIMESTAMP":"1000000","MESSAGE":"secret-token"}'
        with patch("manager.run_fixed", return_value=SimpleNamespace(stdout=raw)):
            result = manager.journal_summary("iptv-admin.service")
        self.assertEqual(result["priority_counts"], {"3": 1})
        self.assertNotIn("secret-token", str(result))

    def test_failure_is_explicit(self):
        with patch("manager.run_fixed", return_value=None):
            self.assertFalse(manager.service_status("iptv-admin.service")["available"])
            self.assertFalse(manager.journal_summary("iptv-admin.service")["available"])

    def test_restart_rejects_unknown_and_unconfirmed(self):
        with patch("manager._restart") as restart:
            with self.assertRaises(ValueError):
                manager.restart_service("ssh.service", manager.CONFIRM)
            with self.assertRaises(ValueError):
                manager.restart_service("iptv-admin.service", "wrong")
            restart.assert_not_called()

    def test_write_disabled(self):
        with patch("manager.os.geteuid", return_value=1000), patch("manager.WRITE_FLAG") as flag:
            flag.is_file.return_value = False
            with self.assertRaises(PermissionError):
                manager.deploy_project(manager.CONFIRM)

    def test_failed_tests_do_not_restart(self):
        from contextlib import nullcontext
        good = SimpleNamespace(returncode=0)
        with patch("manager.require_write"), patch("manager.operation_lock", return_value=nullcontext()), \
             patch("manager.check_checkout"), patch("manager.revision", side_effect=["a"*40, "b"*40]), \
             patch("manager.git", return_value=good), patch("manager._test", return_value={"ok": False}), \
             patch("manager._restart") as restart:
            result = manager.deploy_project(manager.CONFIRM)
            self.assertEqual(result["stage"], "tests")
            restart.assert_not_called()

    def test_health_does_not_claim_playback(self):
        with patch("manager.service_status", return_value={"properties":{"ActiveState":"active"}}), \
             patch("manager.urllib.request.urlopen", side_effect=OSError):
            self.assertFalse(manager.health_check()["ok"])

if __name__ == "__main__":
    unittest.main()
