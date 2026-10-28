"""「粘贴完整 Cookie 登录」接口的离线测试。

用假 provider 替换真实百度请求, 验证:
1. 解析并保存完整 Cookie (配置只在成功后落盘)
2. 失败时原样带回百度 errno, 且不回显 Cookie 值
3. BDUSS 失败后自动用 BDUSS_BFESS 重试
4. 缺登录态时给出可操作提示
5. 仍然只允许本机 + JSON 请求
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netdisk.browser_login import BrowserLogin  # noqa: E402
from netdisk.config import AppConfig  # noqa: E402
from netdisk import webui  # noqa: E402


class FakeApi:
    """假的百度客户端 (既当工厂又当实例)。"""

    def __init__(self, results, error="", uk=42):
        self._results = list(results)
        self.last_error = error
        self._uk = uk
        self.calls = []  # 收到的 AppConfig
        self.seen_bduss = []

    def __call__(self, name, cfg):
        self.calls.append(cfg)
        self._bduss = cfg.bduss
        return self

    def check_login(self):
        self.seen_bduss.append(self._bduss)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class CookieLoginEndpointTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(bduss="previous")
        self.patches = [
            patch.object(webui, "_cfg", self.cfg),
            patch.object(webui, "_api", None),
            patch.object(webui, "_LAST_AUTH_ERROR", ""),
            patch.object(webui, "_browser_login", BrowserLogin()),
            patch.object(webui, "save_config"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = webui.app.test_client()

    def post(self, payload, **options):
        return self.client.post("/api/login/baidu/cookie", json=payload, **options)

    def test_success_saves_full_cookie_set(self):
        api = FakeApi([True])
        with patch.object(webui, "create_provider", api):
            result = self.post({"cookie": "BDUSS=abc; STOKEN=def; BAIDUID=zzz"})
        self.assertEqual(result.status_code, 200)
        data = result.get_json()["data"]
        self.assertEqual(data["cookies"], ["BDUSS", "STOKEN", "BAIDUID"])
        self.assertEqual(data["uk"], 42)
        saved = webui.save_config.call_args[0][0]
        self.assertEqual(saved.bduss, "abc")
        self.assertEqual([c["name"] for c in saved.baidu_cookies], ["BDUSS", "STOKEN", "BAIDUID"])
        self.assertIs(webui._cfg, saved)

    def test_failure_reports_errno_and_never_echoes_values(self):
        api = FakeApi([False], error="百度返回 errno=-6(身份验证失败)，登录态没有通过校验")
        with patch.object(webui, "create_provider", api):
            result = self.post({"cookie": "BDUSS=secret-value; STOKEN=secret-stoken"})
        self.assertEqual(result.status_code, 400)
        body = result.get_json()
        self.assertIn("errno=-6", body["error"])
        self.assertIn("BDUSS", body["error"])
        self.assertNotIn("secret-value", result.get_data(as_text=True))
        self.assertNotIn("secret-stoken", result.get_data(as_text=True))
        webui.save_config.assert_not_called()
        self.assertEqual(webui._cfg.bduss, "previous")

    def test_bduss_bfess_is_retried_when_bduss_fails(self):
        api = FakeApi([False, True])
        with patch.object(webui, "create_provider", api):
            result = self.post({"cookie": "BDUSS=main-token; BDUSS_BFESS=alt-token"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(api.calls[0].bduss, "main-token")
        self.assertEqual(api.calls[-1].bduss, "alt-token")
        self.assertEqual(api.seen_bduss, ["main-token", "alt-token"])

    def test_missing_login_cookie_is_explained(self):
        api = FakeApi([True])
        with patch.object(webui, "create_provider", api):
            result = self.post({"cookie": "BAIDUID=zzz"})
        self.assertEqual(result.status_code, 400)
        self.assertIn("BDUSS", result.get_json()["error"])
        webui.save_config.assert_not_called()

    def test_blank_payload_is_rejected(self):
        for payload in ({}, {"cookie": ""}, {"cookie": "   "}):
            self.assertEqual(self.post(payload).status_code, 400)

    def test_rejects_remote_and_cross_origin_and_non_json(self):
        options = (
            {"environ_overrides": {"REMOTE_ADDR": "10.0.0.1"}},
            {"headers": {"Origin": "https://example.com"}},
            {"base_url": "http://evil.example"},
        )
        for option in options:
            self.assertEqual(self.post({"cookie": "BDUSS=abc"}, **option).status_code, 403)
        raw = self.client.post("/api/login/baidu/cookie", data="BDUSS=abc")
        self.assertEqual(raw.status_code, 415)


class ManualLoginMessageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(bduss="")
        for p in (
            patch.object(webui, "_cfg", self.cfg),
            patch.object(webui, "_api", None),
            patch.object(webui, "_LAST_AUTH_ERROR", ""),
            patch.object(webui, "_browser_login", BrowserLogin()),
            patch.object(webui, "save_config"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = webui.app.test_client()

    def test_rejected_bduss_points_to_cookie_import(self):
        api = MagicMock()
        api.check_login.return_value = False
        api.last_error = "百度返回 errno=-6(身份验证失败)，登录态没有通过校验"
        with patch.object(webui, "create_provider", return_value=api):
            result = self.client.post("/api/login", json={"provider": "baidu", "bduss": "bad"})
        self.assertEqual(result.status_code, 400)
        message = result.get_json()["error"]
        self.assertIn("errno=-6", message)
        self.assertIn("粘贴完整 Cookie", message)
        webui.save_config.assert_not_called()

    def test_non_string_last_error_is_ignored(self):
        api = MagicMock()
        api.check_login.return_value = False
        api.last_error = MagicMock()
        with patch.object(webui, "create_provider", return_value=api):
            result = self.client.post("/api/login", json={"provider": "baidu", "bduss": "bad"})
        self.assertEqual(result.status_code, 400)
        self.assertIn("粘贴完整 Cookie", result.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
