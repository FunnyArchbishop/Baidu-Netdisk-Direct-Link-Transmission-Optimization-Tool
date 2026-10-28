"""百度网盘 API 客户端。

基于公开逆向出的网盘接口实现（技术路线参考 BaiduPCS-Go 等开源项目）：
- 使用 BDUSS 会话凭证进行认证
- 列目录、浏览文件
- 通过 PCS file 接口获取可支持 Range 分段的下载直链

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

# 百度错误码 -> 用户能看懂的解释
_ERRNO_TEXT = {
    -6: "百度返回 errno=-6(身份验证失败)",
    -9: "百度返回 errno=-9(文件或目录不存在)",
    -7: "百度返回 errno=-7(文件或目录不存在)",
    2: "百度返回 errno=2(参数错误)",
    4: "百度返回 errno=4(没有权限)",
    12: "百度返回 errno=12(请求过于频繁，请稍后重试)",
    31066: "百度返回 errno=31066(文件不存在或已被删除)",
}

LOGIN_REFRESH_HINT = (
    "获取可用登录态的办法: 先在浏览器登录 https://pan.baidu.com ,按 F12 → 网络(Network) → "
    "任意 pan 请求 → 请求标头里的 Cookie 整行复制,粘贴到「设置 → 网盘设置 → 粘贴完整 Cookie」后点「用 Cookie 登录」。"
    "只填 BDUSS 一个值在部分账号上不够用。"
)


def errno_text(errno: int, action: str = "") -> str:
    """把百度 errno 翻译成可读信息。"""
    base = _ERRNO_TEXT.get(errno) or f"百度返回 errno={errno}"
    return f"{action}失败: {base}" if action else base


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

    def to_dict(self) -> dict:
        """序列化为普通字典 (供 JSON 输出)。"""
        return {
            "fs_id": self.fs_id,
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "is_dir": self.is_dir,
            "md5": self.md5,
            "server_ctime": self.server_ctime,
            "server_mtime": self.server_mtime,
        }

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

    实现 NetdiskProvider 接口 (name="baidu")。

    用法::

        api = BaiduNetdiskAPI(bduss="你的 BDUSS")
        files = api.list_files("/我的资源")

        link = api.get_direct_link("/我的资源/example.zip")
    """

    name = "baidu"

    def download_headers(self, path: str) -> dict:
        """Keep the browser identity used to obtain the signed download URL."""
        return {
            "User-Agent": self.session.headers.get("User-Agent", DEFAULT_HEADERS["User-Agent"]),
            "Referer": f"{API_BASE}/disk/home",
        }

    def __init__(self, bduss: str, session: Optional[requests.Session] = None, proxy: str = "") -> None:
        if not bduss:
            raise AuthError("BDUSS 不能为空, 请先登录获取会话凭证")

        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.cookies.set("BDUSS", bduss, domain="pan.baidu.com")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self._bdstoken: Optional[str] = None
        self._uk: Optional[int] = None
        # 最近一次失败原因, 供上层原样展示 (比布尔值更有诊断价值)
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # 认证与元信息
    # ------------------------------------------------------------------
    def _fail(self, errno: int, action: str = "") -> NetdiskError:
        """构造错误异常: errno=-6 归为认证失败并记录原因。"""
        message = errno_text(errno, action)
        if errno == -6:
            message = f"{message}。{LOGIN_REFRESH_HINT}"
            self.last_error = message
            return AuthError(message)
        return NetdiskError(message)  # type: ignore[return-value]

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
            raise self._fail(errno, "获取会话信息")

        result = data.get("result") or {}
        self._bdstoken = result.get("bdstoken") or ""
        self._uk = int(result.get("uk", 0) or 0)
        self.last_error = ""

    def check_login(self) -> bool:
        """校验当前 BDUSS 是否有效 (失败原因见 ``last_error``)。"""
        try:
            self._ensure_auth_info()
            return True
        except (AuthError, NetdiskError, requests.RequestException, KeyError, ValueError):
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
            raise self._fail(errno, "获取目录列表")

        return [NetdiskFile.from_api(item) for item in data.get("list", [])]

    def get_file_info(self, path: str) -> Optional[NetdiskFile]:
        """查询单个文件/目录的信息。"""
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        for f in self.list_files(parent):
            if f.name == name:
                return f
        return None

    def search(self, keyword: str, dir_path: str = "/", recursion: bool = True) -> list[NetdiskFile]:
        """按名称搜索文件/目录 (默认递归搜索)。"""
        self._ensure_auth_info()
        params = {
            "key": keyword,
            "dir": self._norm_path(dir_path),
            "recursion": 1 if recursion else 0,
            "page": 1,
            "num": 100,
            "web": 1,
            "clienttype": 0,
            "app_id": PCS_APP_ID,
            "bdstoken": self._bdstoken,
        }
        resp = self.session.get(f"{API_BASE}/api/search", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            raise self._fail(errno, "搜索")
        return [NetdiskFile.from_api(item) for item in data.get("list", [])]

    def walk(self, dir_path: str = "/") -> list[NetdiskFile]:
        """递归遍历目录, 返回其中所有文件 (不含目录)。"""
        result: list[NetdiskFile] = []
        stack = [self._norm_path(dir_path)]
        while stack:
            cur = stack.pop()
            for f in self.list_files(cur):
                if f.is_dir:
                    stack.append(f.path)
                else:
                    result.append(f)
        return result

    # ------------------------------------------------------------------
    # 文件管理
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_path(path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _file_operate(self, opera: str, filelist: list[dict], ondup: str = "fail") -> None:
        """调用 PCS filemanager 接口执行批量文件操作。"""
        self._ensure_auth_info()
        url = f"{API_BASE}/api/filemanager"
        params = {
            "opera": opera,
            "async": 2,
            "ondup": ondup,
            "bdstoken": self._bdstoken,
            "clienttype": 0,
            "app_id": PCS_APP_ID,
            "web": 1,
        }
        resp = self.session.post(
            url, params=params, data={"filelist": json.dumps(filelist)}, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            raise self._fail(errno, f"文件操作({opera})")

    def mkdir(self, path: str) -> None:
        """创建目录。"""
        self._file_operate("create", [{"path": self._norm_path(path), "isdir": "1", "size": ""}])

    def delete(self, *paths: str) -> None:
        """删除文件/目录 (可传入多个路径)。"""
        self._file_operate("delete", [{"path": self._norm_path(p)} for p in paths])

    def rename(self, path: str, newname: str) -> None:
        """重命名文件/目录 (newname 仅新文件名, 不含路径)。"""
        self._file_operate("rename", [{"path": self._norm_path(path), "newname": newname}])

    def move(self, src: str, dest_dir: str) -> None:
        """移动文件/目录到目标目录。"""
        src = self._norm_path(src)
        name = src.rstrip("/").rsplit("/", 1)[-1]
        self._file_operate("move", [{"path": src, "dest": self._norm_path(dest_dir), "newname": name}])

    def copy(self, src: str, dest_dir: str) -> None:
        """复制文件/目录到目标目录。"""
        src = self._norm_path(src)
        name = src.rstrip("/").rsplit("/", 1)[-1]
        self._file_operate("copy", [{"path": src, "dest": self._norm_path(dest_dir), "newname": name}])

    # ------------------------------------------------------------------
    # 直链获取
    # ------------------------------------------------------------------
    def get_direct_link(self, path: str, fs_id: Optional[int] = None) -> str:
        """获取文件下载直链 (支持 Range 分段请求)。

        通过 PCS file 接口, 请求携带会话凭证与合法的 UA,
        服务器返回 302 重定向到实际的 CDN 直链地址。
        """
        self._ensure_auth_info()
        if not path.startswith("/"):
            path = "/" + path

        encoded = urllib.parse.quote(path, safe="/")
        url = (
            f"{PCS_BASE}/rest/2.0/pcs/file?method=download"
            f"&path={encoded}&app_id={PCS_APP_ID}"
        )
        headers = {
            "Referer": f"{API_BASE}/disk/home",
            "User-Agent": self.session.headers.get("User-Agent", DEFAULT_HEADERS["User-Agent"]),
        }

        # allow_redirects=False, 手动解析出真实直链
        resp = self.session.get(url, headers=headers, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                raise NetdiskError("获取直链失败: 重定向缺少 Location")
            return location

        # 非重定向说明失败, 尝试解析错误信息
        try:
            data = resp.json()
            errno = data.get("errno", data.get("error_code", 0))
            if errno == -6:
                raise self._fail(errno, "获取直链")
            msg = data.get("error_msg") or data.get("msg") or errno_text(errno)
        except ValueError:
            msg = f"获取直链失败: HTTP {resp.status_code}"
        raise NetdiskError(f"获取直链失败: {msg}")

    def get_download_plan(self, path: str, fs_id: Optional[int] = None) -> dict:
        """获取文件的下载信息: 直链、文件大小、文件名。

        返回::

            {"url": str, "size": int, "name": str}
        """
        info = self.get_file_info(path)
        if info is None:
            raise NetdiskError(f"未找到文件: {path}")

        url = self.get_direct_link(path, info.fs_id if fs_id is None else fs_id)
        return {"url": url, "size": info.size, "name": info.name}

    # ------------------------------------------------------------------
    # 分享链接
    # ------------------------------------------------------------------
    @staticmethod
    def parse_share_url(url: str) -> tuple[str, str]:
        """从分享链接中提取 (shorturl, pwd)。"""
        import re

        m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
        if not m:
            raise NetdiskError("无效的分享链接, 无法识别 shorturl")
        surl = m.group(1)
        pwd = ""
        pm = re.search(r"[?&#]pwd=([A-Za-z0-9]+)", url)
        if pm:
            pwd = pm.group(1)
        return surl, pwd

    def _share_list_raw(self, surl: str, pwd: str, dir_path: str = "/") -> list[dict]:
        """调用 share/wxlist 返回原始条目列表 (含 dlink 字段)。"""
        if not dir_path.startswith("/"):
            dir_path = "/" + dir_path
        url = f"{API_BASE}/share/wxlist"
        params = {"channel": "weixin", "version": "2.2.2", "clienttype": 25, "web": 1}
        data = {
            "shorturl": surl,
            "dir": dir_path,
            "root": "1",
            "pwd": pwd,
            "page": "1",
            "num": "100",
            "order": "time",
            "desc": "1",
        }
        resp = self.session.post(url, params=params, data=data, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        errno = payload.get("errno", 0)
        if errno != 0:
            msg = payload.get("errmsg") or payload.get("error_msg") or f"errno={errno}"
            raise NetdiskError(f"分享链接解析失败: {msg}")
        return payload.get("data", {}).get("list", []) or []

    def list_share_files(self, share_url: str, dir_path: str = "/") -> list[NetdiskFile]:
        """解析分享链接并列出其中的文件/目录。"""
        surl, pwd = self.parse_share_url(share_url)
        return [NetdiskFile.from_api(item) for item in self._share_list_raw(surl, pwd, dir_path)]

    @staticmethod
    def _strip_surl(surl: str) -> str:
        """分享短链的 verify/init 使用去掉前缀 '1' 的 surl。"""
        return surl[1:] if surl.startswith("1") else surl

    def _resolve_share_meta(self, surl: str, pwd: str) -> tuple[str, str]:
        """解析分享的 shareid 与 share_uk (供构造下载 Referer)。

        实测流程: verify 需用去前缀的 surl + Referer 头 + 表单提交提取码,
        成功后拿到 BDCLND cookie; 再从分享页 HTML 的 window.yunData 解析 shareid/share_uk。
        """
        import re

        stripped = self._strip_surl(surl)
        try:
            # 1. 验证提取码 (获取 BDCLND cookie)
            vurl = f"{API_BASE}/share/verify"
            vparams = {
                "surl": stripped,
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "web": 1,
                "clienttype": 0,
                "app_id": PCS_APP_ID,
            }
            self.session.post(
                vurl,
                params=vparams,
                data={"pwd": pwd, "vcode": "", "vcode_str": ""},
                headers={"Referer": f"{API_BASE}/share/init?surl={stripped}"},
                timeout=15,
            )
            # 2. 访问分享页, 解析 window.yunData 中的 shareid / share_uk
            resp = self.session.get(f"{API_BASE}/s/{surl}", allow_redirects=True, timeout=20)
            html = resp.text
        except requests.RequestException:
            html = ""

        m = re.search(r'shareid\s*:\s*"(\d+)"', html)
        shareid = m.group(1) if m else ""
        m = re.search(r'share_uk\s*:\s*"(\d+)"', html)
        uk = m.group(1) if m else ""
        if not shareid:
            raise NetdiskError("无法解析分享的 shareid, 链接可能已失效或提取码错误")
        return shareid, uk

    def get_share_dlink(self, share_url: str, path: str) -> tuple[str, str, int]:
        """获取分享中某文件的下载直链, 返回 (direct_url, filename, size)。

        原理: 通过 wxlist 拿到条目 dlink, 再携带 Referer 请求 dlink 得到 CDN 直链。
        注意: 百度对未登录会话不返回 dlink, 下载分享通常需要有效 BDUSS。
        """
        surl, pwd = self.parse_share_url(share_url)
        shareid, uk = self._resolve_share_meta(surl, pwd)
        target = path.strip("/")

        found = None
        for item in self._share_list_raw(surl, pwd, "/"):
            p = (item.get("path") or "").lstrip("/")
            if p == target:
                found = item
                break
        if found is None:
            raise NetdiskError(f"分享中未找到文件: {path}")

        name = found.get("server_filename") or target.rsplit("/", 1)[-1]
        size = int(found.get("size", 0) or 0)
        dlink = found.get("dlink") or ""
        if not dlink:
            raise NetdiskError("百度分享接口未返回下载链接，当前分享无法直接下载。请先在百度官方页面转存到自己的网盘，再从本工具的「文件」页下载；重新登录通常不能解决此问题。")

        referer = f"{API_BASE}/share/link?shareid={shareid}&uk={uk}"
        headers = {
            "Referer": referer,
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }
        resp = self.session.get(dlink, headers=headers, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc:
                return loc, name, size
        return dlink, name, size
