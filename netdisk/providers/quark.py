"""夸克网盘提供者 (基于社区逆向接口, 端点可能变动)。

认证: 浏览器 Cookie (需从夸克网盘网页版抓取)。
凭证: `quark_cookie` (Cookie 字符串)。

接口 (社区整理, 稳定性一般, 需真实账号验证):
- 列目录   -> GET https://drive-pc.quark.cn/1/clouddrive/file/sort?pdir_fid=...
- 下载直链 -> GET https://drive-pc.quark.cn/1/clouddrive/file/download?fid=...
- 新建目录 -> POST .../file (创建)
- 删除     -> POST .../file/delete

注意: 夸克接口未公开文档, 字段/域名可能随版本变化, 使用前需抓取有效 Cookie。
"""

from __future__ import annotations

import time

import requests

from ..api import AuthError, NetdiskError, NetdiskFile
from .base import NetdiskProvider

API_BASE = "https://drive-pc.quark.cn/1/clouddrive"


class QuarkProvider(NetdiskProvider):
    name = "quark"

    def __init__(self, cookie: str, proxy: str = "") -> None:
        if not cookie:
            raise AuthError("夸克网盘 Cookie 不能为空")
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": cookie,
            }
        )
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _sort(self, pdir_fid: str = "0") -> list[dict]:
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "pdir_fid": pdir_fid,
            "_size": "100",
            "_fetch_total": "1",
            "_fetch_sub_dirs": "1",
            "__dt": int(time.time() * 1000),
        }
        resp = self.session.get(f"{API_BASE}/file/sort", params=params, timeout=20)
        data = resp.json()
        if data.get("status") != 200 and data.get("code") not in (0, 200):
            msg = data.get("message") or data.get("status") or resp.status_code
            raise NetdiskError(f"夸克网盘请求失败: {msg}")
        return (data.get("data") or {}).get("list", []) or []

    def check_login(self) -> bool:
        try:
            self._sort("0")
            return True
        except Exception:
            return False

    def _resolve(self, path: str) -> tuple[str, dict]:
        path = path.strip("/")
        parent = "0"
        item: dict = {}
        if path:
            for name in path.split("/"):
                found = None
                for it in self._sort(parent):
                    if it.get("file_name") == name or it.get("name") == name:
                        found = it
                        break
                if found is None:
                    raise NetdiskError(f"未找到: /{path}")
                parent = found.get("fid") or found.get("file_id") or ""
                item = found
        else:
            item = {"fid": "0", "dir": True}
        return parent, item

    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        fid, _ = self._resolve(dir_path)
        base = "" if dir_path == "/" else dir_path.rstrip("/")
        out = []
        for it in self._sort(fid):
            name = it.get("file_name") or it.get("name") or ""
            is_dir = bool(it.get("dir")) or it.get("file_type") == "folder"
            out.append(
                NetdiskFile(
                    fs_id=int(abs(hash(it.get("fid") or it.get("file_id") or name)) % (10 ** 15)),
                    path=f"{base}/{name}",
                    name=name,
                    size=int(it.get("size", 0) or 0),
                    is_dir=is_dir,
                    md5="",
                    raw=it,
                )
            )
        return out

    def get_direct_link(self, path: str, fs_id=None) -> str:
        fid, item = self._resolve(path)
        if item.get("dir"):
            raise NetdiskError("目录不能下载, 请指定文件")
        params = {"fid": fid, "pr": "ucpro", "fr": "pc"}
        resp = self.session.get(f"{API_BASE}/file/download", params=params, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc:
                return loc
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        url = (data.get("data") or {}).get("download_url") or data.get("download_url")
        if not url:
            raise NetdiskError("获取夸克网盘下载链接失败")
        return url

    def download_headers(self, path: str) -> dict:
        return {"Referer": "https://pan.quark.cn/"}
