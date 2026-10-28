"""Offline regression coverage for official login and credential isolation."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from netdisk.browser_login import BrowserLogin, login_dependency_error, open_login_panel
from netdisk.config import AppConfig
from netdisk import webui


class LoginTests(unittest.TestCase):
    def test_official_home_opens_password_login(self):
        page = MagicMock()
        self.assertTrue(open_login_panel(page))
        self.assertEqual([c.args[0] for c in page.get_by_text.call_args_list], ["去登录", "账号登录"])
        self.assertEqual(page.get_by_text.return_value.click.call_count, 2)

    def test_changed_homepage_leaves_manual_login_available(self):
        page = MagicMock()
        page.get_by_text.return_value.click.side_effect = RuntimeError("not found")
        self.assertFalse(open_login_panel(page))

    def test_dependency_error_identifies_running_interpreter(self):
        message = login_dependency_error(ModuleNotFoundError(name="greenlet"))
        self.assertIn(sys.executable, message)
        self.assertIn("greenlet", message)
        self.assertIn("start-web.cmd", message)

    def test_frozen_error_explains_install_does_not_update_exe(self):
        with patch.object(sys, "frozen", True, create=True):
            message = login_dependency_error(ModuleNotFoundError(name="playwright"))
        self.assertIn("EXE", message)
        self.assertNotIn("-m pip", message)

    def setUp(self):
        self.cfg = AppConfig(bduss="previous")
        self.patches = [patch.object(webui, "_cfg", self.cfg),
                        patch.object(webui, "_api", None),
                        patch.object(webui, "_browser_login", BrowserLogin()),
                        patch.object(webui, "save_config")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = webui.app.test_client()

    def test_failed_manual_login_preserves_config(self):
        api = MagicMock()
        api.check_login.return_value = False
        with patch.object(webui, "create_provider", return_value=api):
            result = self.client.post("/api/login", json={"provider": "baidu", "bduss": "invalid"})
        self.assertEqual(result.status_code, 400)
        self.assertIs(webui._cfg, self.cfg)
        self.assertEqual(webui._cfg.bduss, "previous")
        webui.save_config.assert_not_called()

    def test_browser_login_rejects_remote_and_cross_origin(self):
        for options in ({"environ_overrides": {"REMOTE_ADDR": "10.0.0.1"}},
                        {"headers": {"Origin": "https://example.com"}},
                        {"base_url": "http://evil.example"}):
            self.assertEqual(self.client.post("/api/login/baidu/start", json={}, **options).status_code, 403)
        self.assertEqual(self.client.post("/api/login/baidu/start").status_code, 415)

    def test_commit_only_after_save(self):
        webui.save_config.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            webui._commit_browser_login("new", SimpleNamespace(_browser_credentials=AppConfig()))
        self.assertIs(webui._cfg, self.cfg)
        self.assertIsNone(webui._api)

    def run_browser(self, cancel_during_validation=False, pages=True):
        login = BrowserLogin()
        browser = MagicMock()
        context = browser.new_context.return_value
        context.pages = [object()] if pages else []
        context.cookies.return_value = [{"name": "BDUSS", "value": "test-session"}]
        pw = MagicMock()
        pw.chromium.launch.return_value = browser
        factory = MagicMock()
        factory.return_value.__enter__.return_value = pw
        commit = MagicMock()
        def validate(value):
            self.assertEqual(value["bduss"], "test-session")
            self.assertEqual(value["cookies"][0]["name"], "BDUSS")
            if cancel_during_validation:
                login.cancel()
            return object()
        with patch.dict(sys.modules, {"playwright.sync_api": SimpleNamespace(sync_playwright=factory)}):
            login._run(validate, commit, "")
        browser.close.assert_called_once()
        self.assertNotIn("test-session", str(login.snapshot()))
        return login, commit

    def test_success_imports_session(self):
        login, commit = self.run_browser()
        self.assertEqual(login.snapshot()["status"], "success")
        commit.assert_called_once()

    def test_cancel_during_validation_cannot_commit(self):
        login, commit = self.run_browser(cancel_during_validation=True)
        self.assertEqual(login.snapshot()["status"], "cancelled")
        commit.assert_not_called()

    def test_closed_window_does_not_commit(self):
        login, commit = self.run_browser(pages=False)
        self.assertEqual(login.snapshot()["status"], "cancelled")
        commit.assert_not_called()

    def test_cancel_state_cannot_be_overwritten(self):
        login = BrowserLogin()
        login._set("starting", "opening")
        login.cancel()
        login._set("waiting", "waiting")
        self.assertEqual(login.snapshot()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
