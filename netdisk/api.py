"""百度网盘 API 客户端。

基于公开逆向出的网盘接口实现（技术路线参考 BaiduPCS-Go 等开源项目）：
- 使用 BDUSS 会话凭证进行认证
- 列目录、浏览文件
- 通过 PCS superfile2 接口获取可支持 Range 分段的下载直链

注意：本模块仅用于个人学习与数据备份场景，请遵守百度网盘用户协议。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests

# 通用请求头, 下载直链对 UA / Referer 有要求
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# PCS 直链接口所用的 app_id
PCS_APP_ID = "250528"

API_BASE = "https://pan.baidu.com"
PCS_BASE = "https://d.pcs.baidu.com"


class NetdiskError(Exception):
    """网盘 API 业务错误。"""


class AuthError(NetdiskError):
    """认证失败。"""


@dataclass
class NetdiskFile:
    """网盘中的文件或目录。"""

    fs_id: int
    path: str
    name: str
    size: int = 0
    is_dir: bool = False
    server_ctime: int = 0
    server_mtime: int = 0
    md5: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def size_human(self) -> str:
        return human_size(self.size)

    @classmethod
    def from_api(cls, data: dict) -> "NetdiskFile":
        is_dir = data.get("isdir", 0) == 1
        return cls(
            fs_id=int(data.get("fs_id", 0)),
            path=data.get("path", ""),
            name=data.get("server_filename", data.get("path", "").rsplit("/", 1)[-1] or ""),
            size=int(data.get("size", 0) or 0),
            is_dir=is_dir,
            server_ctime=int(data.get("server_ctime", 0) or 0),
            server_mtime=int(data.get("server_mtime", 0) or 0),
            md5=data.get("md5", ""),
            raw=data,
        )


def human_size(num: float) -> str:
    """把字节数格式化为人类可读的字符串。"""
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} B"


class BaiduNetdiskAPI:
    """百度网盘 API 客户端。

    用法::

        api = BaiduNetdiskAPI(bduss="你的 BDUSS")
        files = api.list_files("/我的资源")
        link = api.get_direct_link("/我的资源/example.zip")
    """

    def __init__(self, bduss: str, session: Optional[requests.Session] = None) -> None:
        if not bduss:
            raise AuthError("BDUSS 不能为空, 请先登录获取会话凭证")

        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.cookies.set("BDUSS", bduss, domain="pan.baidu.com")

        self._bdstoken: Optional[str] = None
        self._uk: Optional[int] = None

    # ------------------------------------------------------------------
    # 认证与元信息
    # ------------------------------------------------------------------
    def _ensure_auth_info(self) -> None:
        """获取 bdstoken 与 uk (网盘会话校验所需)。"""
        if self._bdstoken and self._uk:
            return

        url = f"{API_BASE}/api/gettemplatevariable"
        params = {
            "clienttype": 0,
            "app_id": PCS_APP_ID,
            "web": 1,
            "fields": json.dumps(["bdstoken", "uk", "isdocuser"]),
        }
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            if errno == -6:
                raise AuthError("BDUSS 已失效, 请重新登录")
            raise NetdiskError(f"获取会话信息失败: errno={errno}")

        self._bdstoken = data["result"].get("bdstoken") or ""
        self._uk = int(data["result"].get("uk", 0) or 0)

    def check_login(self) -> bool:
        """校验当前 BDUSS 是否有效。"""
        try:
            self._ensure_auth_info()
            return True
        except (AuthError, NetdiskError, requests.RequestException):
            return False

    # ------------------------------------------------------------------
    # 文件浏览
    # ------------------------------------------------------------------
    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        """列出指定目录下的文件与子目录。"""
        self._ensure_auth_info()
        dir_path = dir_path or "/"
        if not dir_path.startswith("/"):
            dir_path = "/" + dir_path
        if not dir_path.endswith("/"):
            dir_path += "/"

        params = {
            "order": "name",
            "desc": 0,
            "showempty": 0,
            "web": 1,
            "page": 1,
            "num": 100,
            "dir": dir_path,
            "t": int(time.time()),
            "channel": "chunlei",
            "clienttype": 0,
            "app_id": PCS_APP_ID,
            "bdstoken": self._bdstoken,
        }
        resp = self.session.get(f"{API_BASE}/api/list", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        errno = data.get("errno", 0)
        if errno != 0:
            if errno == -6:
                raise AuthError("BDUSS 已失效, 请重新登录")
            raise NetdiskError(f"获取目录列表失败: errno={errno}")

        return [NetdiskFile.from_api(item) for item in data.get("list", [])]

    def get_file_info(self, path: str) -> Optional[NetdiskFile]:
        """查询单个文件/目录的信息。"""
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        for f in self.list_files(parent):
            if f.name == name:
                return f
        return None

    # ------------------------------------------------------------------
    # 直链获取
    # ------------------------------------------------------------------
    def get_direct_link(self, path: str, fs_id: Optional[int] = None) -> str:
        """获取文件下载直链 (支持 Range 分段请求)。

        通过 PCS superfile2 接口, 请求携带会话凭证与合法的 UA,
        服务器返回 302 重定向到实际的 CDN 直链地址。
        """
        self._ensure_auth_info()
        if not path.startswith("/"):
            path = "/" + path

        encoded = urllib.parse.quote(path, safe="/")
        url = (
            f"{PCS_BASE}/rest/2.0/pcs/superfile2?method=download"
            f"&path={encoded}&ver=2&app_id={PCS_APP_ID}"
        )
        headers = {
            "Referer": f"{API_BASE}/disk/home",
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }

        # allow_redirects=False, 手动解析出真实直链
        resp = self.session.get(url, headers=headers, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303, 307, 308):
            return resp.headers.get("Location", "")

        # 非重定向说明失败, 尝试解析错误信息
        try:
            data = resp.json()
            errno = data.get("errno", 0)
            if errno == -6:
                raise AuthError("BDUSS 已失效, 请重新登录")
            msg = data.get("error_msg") or data.get("msg") or f"errno={errno}"
        except ValueError:
            msg = f"HTTP {resp.status_code}"
        raise NetdiskError(f"获取直链失败: {msg}")

    def get_download_plan(self, path: str, fs_id: Optional[int] = None) -> dict:
        """获取文件的下载信息: 直链、文件大小、文件名。

        返回::

            {"url": str, "size": int, "name": str}
        """
        info = self.get_file_info(path) if fs_id is None else None
        if info is None and fs_id is None:
            raise NetdiskError(f"未找到文件: {path}")

        url = self.get_direct_link(path, fs_id)
        size = int(info.size) if info else 0
        name = info.name if info else path.rsplit("/", 1)[-1]
        return {"url": url, "size": size, "name": name}
