import unittest
from unittest.mock import MagicMock, patch

import requests

from netdisk.api import BaiduNetdiskAPI, NetdiskError
from netdisk.downloader import DirectLinkDownloader


class DownloadLinkTests(unittest.TestCase):
    def test_probe_recovers_from_transient_tls_failure(self):
        downloader = DirectLinkDownloader("https://example.com/file", "unused-test-file")
        downloader._probe_once = MagicMock(side_effect=[
            requests.exceptions.SSLError("transient EOF"),
            requests.exceptions.SSLError("transient EOF"),
            (123, True),
        ])
        with patch("netdisk.downloader.time.sleep"):
            self.assertEqual(downloader.probe(), (123, True))
        self.assertEqual(downloader._probe_once.call_count, 3)

    def test_probe_stops_after_bounded_retries(self):
        downloader = DirectLinkDownloader("https://example.com/file", "unused-test-file")
        downloader._probe_once = MagicMock(side_effect=requests.exceptions.SSLError("EOF"))
        with patch("netdisk.downloader.time.sleep"):
            with self.assertRaises(requests.exceptions.SSLError):
                downloader.probe()
        self.assertEqual(downloader._probe_once.call_count, 4)

    def make_api(self):
        session = MagicMock()
        session.headers = {"User-Agent": "test-browser"}
        api = BaiduNetdiskAPI("test-bduss", session=session)
        session.headers["User-Agent"] = "logged-in-browser"
        api._bdstoken, api._uk = "test-token", 123
        return api, session

    def test_personal_download_uses_file_endpoint_and_browser_identity(self):
        api, session = self.make_api()
        response = session.get.return_value
        response.status_code = 302
        response.headers = {"Location": "https://example.com/download"}
        self.assertEqual(api.get_direct_link("/test file.txt"), "https://example.com/download")
        args, kwargs = session.get.call_args
        self.assertIn("/pcs/file?method=download", args[0])
        self.assertEqual(kwargs["headers"]["User-Agent"], "logged-in-browser")
        self.assertEqual(api.download_headers("/test file.txt")["User-Agent"], "logged-in-browser")

    def test_pcs_error_code_is_preserved(self):
        api, session = self.make_api()
        session.get.return_value.status_code = 400
        session.get.return_value.json.return_value = {"error_code": 31064}
        with self.assertRaisesRegex(NetdiskError, "31064"):
            api.get_direct_link("/test.txt")

    def test_missing_share_link_does_not_misdiagnose_login(self):
        api, _ = self.make_api()
        api._resolve_share_meta = MagicMock(return_value=("123", "456"))
        api._share_list_raw = MagicMock(return_value=[{"path": "/test.txt", "size": 1}])
        with self.assertRaisesRegex(NetdiskError, "转存到自己的网盘"):
            api.get_share_dlink("https://pan.baidu.com/s/1test", "/test.txt")


if __name__ == "__main__":
    unittest.main()
