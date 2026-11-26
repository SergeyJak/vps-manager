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

if __name__ == "__main__":
    unittest.main()
