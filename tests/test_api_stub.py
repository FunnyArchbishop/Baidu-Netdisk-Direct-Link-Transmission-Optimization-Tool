"""API 客户端自包含测试 (不依赖真实网络)。

注入假 requests, 验证:
1. 路径规范化
2. 文件管理操作的 opera / filelist 参数构造正确
3. 错误码处理

运行: python tests/test_api_stub.py
"""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RequestException(Exception):
    pass


class FakeResp:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestException(f"HTTP {self.status_code}")


class FakeCookies:
    def __init__(self):
        self._d = {}

    def set(self, name, value, domain=None):
        self._d[name] = value


class FakeHeaders(dict):
    def update(self, *a, **kw):
        dict.update(self, *a, **kw)


class FakeSession:
    def __init__(self):
        self.headers = FakeHeaders()
        self.cookies = FakeCookies()
        self.calls = []  # (method, url, kwargs)

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append(("get", url, {"params": params, **kw}))
        if "gettemplatevariable" in url:
            return FakeResp({"errno": 0, "result": {"bdstoken": "tok123", "uk": 42}})
        if "/api/list" in url:
            return FakeResp({"errno": 0, "list": []})
        return FakeResp({"errno": 0})

    def post(self, url, params=None, data=None, timeout=None, **kw):
        self.calls.append(("post", url, {"params": params, "data": data, **kw}))
        return FakeResp({"errno": 0})


_fake = types.ModuleType("requests")
_fake.RequestException = RequestException
_fake.Session = FakeSession
sys.modules["requests"] = _fake

from netdisk.api import BaiduNetdiskAPI, NetdiskError, AuthError  # noqa: E402


def main() -> int:
    # 路径规范化
    assert BaiduNetdiskAPI._norm_path("a/b") == "/a/b"
    assert BaiduNetdiskAPI._norm_path("/x") == "/x"
    print("[PASS] 测试1: 路径规范化正确")

    session = FakeSession()
    api = BaiduNetdiskAPI("dummy-bduss", session=session)

    # 触发认证
    api.mkdir("/我的资料/新目录")
    last = session.calls[-1]
    assert last[0] == "post", "mkdir 应使用 POST"
    fl = json.loads(last[2]["data"]["filelist"])
    assert last[2]["params"]["opera"] == "create"
    assert fl[0]["path"] == "/我的资料/新目录" and fl[0]["isdir"] == "1"

    api.delete("/a.txt", "/b.txt")
    last = session.calls[-1]
    fl = json.loads(last[2]["data"]["filelist"])
    assert last[2]["params"]["opera"] == "delete"
    assert len(fl) == 2 and fl[0]["path"] == "/a.txt" and fl[1]["path"] == "/b.txt"

    api.rename("/a.txt", "c.txt")
    last = session.calls[-1]
    fl = json.loads(last[2]["data"]["filelist"])
    assert last[2]["params"]["opera"] == "rename"
    assert fl[0]["path"] == "/a.txt" and fl[0]["newname"] == "c.txt"

    api.move("/a.txt", "/我的资料")
    last = session.calls[-1]
    fl = json.loads(last[2]["data"]["filelist"])
    assert last[2]["params"]["opera"] == "move"
    assert fl[0]["dest"] == "/我的资料" and fl[0]["newname"] == "a.txt"

    api.copy("/a.txt", "/我的资料")
    last = session.calls[-1]
    fl = json.loads(last[2]["data"]["filelist"])
    assert last[2]["params"]["opera"] == "copy"
    assert fl[0]["dest"] == "/我的资料" and fl[0]["newname"] == "a.txt"
    print("[PASS] 测试2: 文件管理 opera/filelist 参数构造正确")

    # 错误码处理: errno=-6 应抛 AuthError
    session.post = lambda url, **kw: FakeResp({"errno": -6})
    try:
        api.mkdir("/x")
        assert False, "应抛出 AuthError"
    except AuthError:
        pass
    print("[PASS] 测试3: errno=-6 正确映射为 AuthError")

    # ---- 测试 4: 分享链接解析 ----
    surl, pwd = BaiduNetdiskAPI.parse_share_url("https://pan.baidu.com/s/1AbCdEf_12?pwd=xy9z")
    assert surl == "1AbCdEf_12" and pwd == "xy9z"
    surl2, pwd2 = BaiduNetdiskAPI.parse_share_url("https://pan.baidu.com/s/1AbCdEf_12")
    assert surl2 == "1AbCdEf_12" and pwd2 == ""
    try:
        BaiduNetdiskAPI.parse_share_url("https://example.com/foo")
        assert False, "应抛出 NetdiskError"
    except NetdiskError:
        pass
    print("[PASS] 测试4: 分享链接 shorturl/pwd 解析正确")

    # ---- 测试 5: surl 前缀处理 ----
    assert BaiduNetdiskAPI._strip_surl("1HNhOw9EU2c9muXSPv5RsCA") == "HNhOw9EU2c9muXSPv5RsCA"
    assert BaiduNetdiskAPI._strip_surl("AbCdEf") == "AbCdEf"
    print("[PASS] 测试5: 分享 surl 前缀 '1' 剥离正确")

    print("\n全部 API 核心测试通过!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
