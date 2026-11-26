import io
import subprocess
import sys
import unittest
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.response import addinfourl
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

    def test_health_does_not_claim_playback(self):
        with patch("manager.service_status", return_value={"properties":{"ActiveState":"active"}}), \
             patch("urllib.request.HTTPSHandler.https_open", side_effect=OSError):
            result = manager.health_check()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["admin_http_status"])

    def test_health_preserves_http_error(self):
        response = self.http_response(manager.HEALTH_URL, 503)
        with patch("manager.service_status", return_value={"properties": {"ActiveState": "active"}}), \
             patch("urllib.request.HTTPSHandler.https_open", return_value=response):
            result = manager.health_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["admin_http_status"], 503)

    def test_health_does_not_follow_redirect(self):
        next_url = manager.HEALTH_URL + "other"
        headers = Message()
        headers["Location"] = next_url
        responses = [self.http_response(manager.HEALTH_URL, 302, headers),
                     self.http_response(next_url, 200)]
        with patch("manager.service_status", return_value={"properties": {"ActiveState": "active"}}), \
             patch("urllib.request.HTTPSHandler.https_open", side_effect=responses) as request:
            result = manager.health_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["admin_http_status"], 302)
        request.assert_called_once()

    @staticmethod
    def http_response(url, code, headers=None):
        response = addinfourl(io.BytesIO(), headers or Message(), url, code)
        response.msg = "Test response"
        return response


class DeployTests(unittest.TestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        root = Path(resources.enter_context(TemporaryDirectory()))
        remote = root / "upstream"
        remote.mkdir()
        self.project = root / "live"
        self.git(remote, "init", "-q", "-b", "main")
        self.before = self.commit(remote, "old")
        self.git(root, "clone", "-q", remote.as_posix(), str(self.project))
        (remote / "tests").mkdir()
        (remote / "tests" / "test_sample.py").write_text(
            "import unittest\n"
            "\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_version(self):\n"
            "        self.fail('fixture intentionally rejects the target')\n",
            encoding="ascii",
        )
        self.git(remote, "add", "tests")
        self.target = self.commit(remote, "new")

        flag = root / ".config" / "vps-manager" / "enable-writes"
        flag.parent.mkdir(parents=True)
        flag.touch()
        resources.enter_context(patch.multiple(manager, PROJECT=self.project, REMOTE=remote.as_posix(),
                                                 WRITE_FLAG=flag))
        resources.enter_context(patch("manager.git", side_effect=lambda *args: self.git(self.project, *args)))
        resources.enter_context(patch("manager.require_write"))

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    def commit(self, repo, content):
        (repo / "app.txt").write_text(content, encoding="ascii")
        self.git(repo, "add", "app.txt")
        self.git(repo, "-c", "user.name=Deploy Test", "-c", "user.email=test@example.invalid",
                 "-c", "commit.gpgSign=false", "commit", "-qm", content)
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    def deploy(self):
        return manager.deploy_project(manager.CONFIRM)

    def test_failed_tests_leave_live_checkout_untouched(self):
        observed = []

        def execute_python(args, checkout, timeout):
            observed.append((checkout, (checkout / "app.txt").read_text(), manager.revision()))
            return subprocess.run([sys.executable, *args[1:]], cwd=checkout, timeout=timeout,
                                  capture_output=True, text=True, check=False)

        with patch("manager.execute", side_effect=execute_python), patch("manager._restart") as restart:
            result = self.deploy()
        self.assertEqual(result["stage"], "tests")
        self.assertEqual(result["tests"], {"ok": False, "exit_code": 1})
        self.assertEqual(manager.revision(), self.before)
        self.assertEqual((self.project / "app.txt").read_text(), "old")
        checkout, content, live_revision = observed[0]
        self.assertEqual((content, live_revision), ("new", self.before))
        self.assertNotEqual(checkout, self.project)
        self.assertFalse(checkout.exists())
        self.assertEqual(self.git(self.project, "worktree", "list", "--porcelain").stdout.count("worktree "), 1)
        restart.assert_not_called()

    def test_retry_after_failed_restart_completes_deploy(self):
        with patch("manager._test", return_value={"ok": True}) as tests, \
             patch("manager._restart", side_effect=[False, True]) as restart, \
             patch("manager.health_check", return_value={"ok": True}):
            first = self.deploy()
            second = self.deploy()
        self.assertEqual(first["stage"], "restart")
        self.assertEqual(manager.revision(), self.target)
        self.assertTrue(second["ok"])
        self.assertEqual(tests.call_count, 2)
        self.assertEqual(restart.call_count, 2)

    def test_up_to_date_deploy_reports_current_health(self):
        with patch("manager._test", return_value={"ok": True}) as tests, \
             patch("manager._restart", return_value=True) as restart, \
             patch("manager.health_check", side_effect=[{"ok": True}, {"ok": False}]):
            first = self.deploy()
            second = self.deploy()
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertFalse(second["changed"])
        tests.assert_called_once()
        restart.assert_called_once()

    def test_deploy_waits_for_health_after_restart(self):
        clock = SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None)
        with patch("manager.time", clock, create=True), \
             patch("manager._test", return_value={"ok": True}), \
             patch("manager._restart", return_value=True) as restart, \
             patch("manager.health_check", side_effect=[{"ok": False}, {"ok": True}]) as health:
            result = self.deploy()
        self.assertTrue(result["ok"])
        self.assertEqual(health.call_count, 2)
        restart.assert_called_once()

    def test_deploy_stops_waiting_and_can_retry_after_unhealthy_release(self):
        now = 0

        def sleep(seconds):
            nonlocal now
            now += seconds

        clock = SimpleNamespace(monotonic=lambda: now, sleep=sleep)
        with patch("manager.time", clock, create=True), \
             patch("manager._test", return_value={"ok": True}), \
             patch("manager._restart", return_value=True) as restart, \
             patch("manager.health_check", return_value={"ok": False}) as health:
            first = self.deploy()
            self.assertFalse(first["ok"])
            self.assertEqual(now, 30)
            self.assertGreater(health.call_count, 1)
            health.return_value = {"ok": True}
            second = self.deploy()
        self.assertTrue(second["ok"])
        self.assertEqual(restart.call_count, 2)

if __name__ == "__main__":
    unittest.main()
