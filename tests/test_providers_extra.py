"""阿里云盘 / 夸克 / 讯飞 提供者的请求构造测试 (mock session)。

验证三个 provider 的认证、列目录、直链解析是否正确构造请求与解析响应。
运行: python tests/test_providers_extra.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netdisk.api import AuthError  # noqa: E402
from netdisk.providers.aliyun import AliyunProvider  # noqa: E402
from netdisk.providers.iflytek import IflytekProvider  # noqa: E402
from netdisk.providers.quark import QuarkProvider  # noqa: E402


class FakeResp:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """按 URL 路径返回预置响应, 记录所有请求。"""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def _resp(self, url: str):
        for key, data in self.responses.items():
            if key in url:
                if data.get("__status"):
                    return FakeResp(data["__status"], data)
                return FakeResp(200, data)
        return FakeResp(404, {"code": "NotFound", "message": "not found"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        return self._resp(url)

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(("GET", url, params, headers))
        return self._resp(url)


def test_aliyun() -> None:
    p = AliyunProvider("refresh-token-123")
    s = FakeSession()
    s.responses = {
        "/oauth/access_token": {"access_token": "tok123", "expires_in": 7200},
        "/adrive/v1.0/user/getDriveInfo": {"default_drive_id": "drv1"},
        "/adrive/v1.0/openFile/list": {"items": [{"file_id": "f1", "name": "a.txt", "type": "file", "size": 100}]},
        "/adrive/v1.0/openFile/getDownloadUrl": {"url": "https://cdn.example/a.txt"},
    }
    p.session = s

    assert p.check_login() is True
    files = p.list_files("/")
    assert len(files) == 1 and files[0].name == "a.txt" and files[0].size == 100

    # 校验 token 换取 + 鉴权头
    token_call = next(c for c in s.calls if c[0] == "POST" and "access_token" in c[1])
    assert token_call[2] == {"grant_type": "refresh_token", "refresh_token": "refresh-token-123"}
    list_call = next(c for c in s.calls if c[0] == "POST" and "openFile/list" in c[1])
    assert list_call[2]["drive_id"] == "drv1" and list_call[2]["parent_file_id"] == "root"
    assert list_call[3]["Authorization"] == "Bearer tok123"

    url = p.get_direct_link("/a.txt")
    assert url == "https://cdn.example/a.txt"
    dl_call = next(c for c in s.calls if c[0] == "POST" and "getDownloadUrl" in c[1])
    assert dl_call[2]["file_id"] == "f1"
    print("[PASS] 测试1: 阿里云盘 认证/列目录/直链解析 正确")


def test_quark() -> None:
    p = QuarkProvider("cookie=abc")
    s = FakeSession()
    s.responses = {
        "/file/sort": {"status": 200, "data": {"list": [{"fid": "f1", "file_name": "b.txt", "dir": False, "size": 50}]}},
    }
    p.session = s

    assert p.check_login() is True
    files = p.list_files("/")
    assert len(files) == 1 and files[0].name == "b.txt" and files[0].size == 50
    sort_call = next(c for c in s.calls if "file/sort" in c[1])
    assert sort_call[2].get("pdir_fid") == "0"
    print("[PASS] 测试2: 夸克网盘 认证/列目录 正确")


def test_iflytek() -> None:
    p = IflytekProvider("tok", url="https://pan.iflytek.example.com")
    s = FakeSession()
    s.responses = {
        "/file/list": {"code": 0, "data": {"list": [{"id": "i1", "name": "c.txt", "is_dir": False, "size": 20}]}},
    }
    p.session = s

    assert p.check_login() is True
    files = p.list_files("/")
    assert len(files) == 1 and files[0].name == "c.txt" and files[0].size == 20
    print("[PASS] 测试3: 讯飞网盘 认证/列目录 正确")


def test_missing_credentials() -> None:
    for factory in (lambda: AliyunProvider(""), lambda: QuarkProvider(""), lambda: IflytekProvider("", url="")):
        try:
            factory()
            assert False, "应抛出 AuthError"
        except AuthError:
            pass
    print("[PASS] 测试4: 缺凭证时抛 AuthError")


def main() -> int:
    test_aliyun()
    test_quark()
    test_iflytek()
    test_missing_credentials()
    print("\n全部 provider 构造测试通过!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
