"""讯飞网盘提供者 (通用 REST 模板)。

说明: 科大讯飞目前没有面向个人开放的标准化网盘 API (不同于阿里云盘 Open API),
不同讯飞服务 (讯飞听见 / 讯飞文档等) 的接口各异。本实现提供一个**可配置的通用 REST
模板**, 通过 `iflytek_url` 指定服务地址, 便于按具体服务微调。

凭证: `iflytek_token` (Token) + `iflytek_url` (服务地址, 必填)。

默认约定的接口 (可按实际服务修改本文件):
- 列目录   -> GET  {iflytek_url}/file/list?path=...      返回 {"code":0,"data":{"list":[...]}}
- 下载直链 -> GET  {iflytek_url}/file/download?path=...   返回 {"code":0,"data":{"url":...}} 或 302
"""

from __future__ import annotations

import requests

from ..api import AuthError, NetdiskError, NetdiskFile
from .base import NetdiskProvider


class IflytekProvider(NetdiskProvider):
    name = "iflytek"

    def __init__(self, token: str, url: str = "", proxy: str = "") -> None:
        if not url:
            raise AuthError("讯飞网盘服务地址 (iflytek_url) 不能为空")
        self.base_url = (url or "").rstrip("/")
        self.token = token or ""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "pan-opt/1.0", "Authorization": f"Bearer {self.token}"})
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _get(self, endpoint: str, **params) -> dict:
        resp = self.session.get(f"{self.base_url}{endpoint}", params=params, timeout=20)
        if resp.status_code in (401, 403):
            raise AuthError("讯飞网盘认证失败, 请检查 Token")
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") not in (0, 200, None, ""):
            raise NetdiskError(f"讯飞网盘请求失败: {data.get('message') or data.get('code')}")
        return data

    def check_login(self) -> bool:
        try:
            self._get("/file/list", path="/")
            return True
        except Exception:
            return False

    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        data = self._get("/file/list", path=dir_path)
        items = (data.get("data") or {}).get("list", []) or []
        base = "" if dir_path == "/" else dir_path.rstrip("/")
        out = []
        for it in items:
            name = it.get("name") or it.get("file_name") or ""
            is_dir = bool(it.get("is_dir") or it.get("type") == "dir" or it.get("type") == "folder")
            out.append(
                NetdiskFile(
                    fs_id=int(abs(hash(it.get("id") or name)) % (10 ** 15)),
                    path=f"{base}/{name}",
                    name=name,
                    size=int(it.get("size", 0) or 0),
                    is_dir=is_dir,
                    md5=it.get("md5", "") or "",
                    raw=it,
                )
            )
        return out

    def get_direct_link(self, path: str, fs_id=None) -> str:
        resp = self.session.get(f"{self.base_url}/file/download", params={"path": path}, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc:
                return loc
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        url = (data.get("data") or {}).get("url") or data.get("url")
        if not url:
            raise NetdiskError("获取讯飞网盘下载链接失败 (请按实际服务调整 /file/download 接口)")
        return url

    def download_headers(self, path: str) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}
