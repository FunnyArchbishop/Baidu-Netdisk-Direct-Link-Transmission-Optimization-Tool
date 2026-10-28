"""阿里云盘提供者 (基于官方 Open API: https://openapi.alipan.com)。

认证: OAuth 2.0, 使用 refresh_token 换取 access_token (自动续期)。
凭证: 需要 `refresh_token` (可通过第三方「阿里云盘 Open API 令牌」工具获取)。

接口映射:
- 列目录   -> POST /adrive/v1.0/openFile/list
- 下载直链 -> POST /adrive/v1.0/openFile/getDownloadUrl (签名 URL, 无需鉴权头)
- 搜索     -> POST /adrive/v1.0/openFile/search
- 新建目录 -> POST /adrive/v1.0/openFile/create
- 删除     -> POST /adrive/v1.0/openFile/delete
- 重命名   -> POST /adrive/v1.0/openFile/update
- 移动     -> POST /adrive/v1.0/openFile/move

注意: 阿里云盘是「文件 ID」体系而非路径体系, 本实现内部通过从根目录逐层
遍历把路径解析为 file_id, 以适配统一的路径接口。
"""

from __future__ import annotations

import time

import requests

from ..api import AuthError, NetdiskError, NetdiskFile
from .base import NetdiskProvider

API_BASE = "https://openapi.alipan.com"


class AliyunProvider(NetdiskProvider):
    name = "aliyun"

    def __init__(self, refresh_token: str, drive_id: str = "", proxy: str = "") -> None:
        if not refresh_token:
            raise AuthError("阿里云盘 refresh_token 不能为空")
        self.refresh_token = refresh_token
        self.drive_id = drive_id or ""
        self._access_token = ""
        self._expires_at = 0.0

        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({"User-Agent": "pan-opt/1.0"})

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        resp = self.session.post(
            f"{API_BASE}/oauth/access_token",
            json={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            timeout=20,
        )
        data = resp.json() if resp.status_code < 500 else {}
        if resp.status_code >= 400 or "access_token" not in data:
            msg = (data or {}).get("message") or (data or {}).get("error_description") or f"HTTP {resp.status_code}"
            raise AuthError(f"阿里云盘登录失败: {msg}")
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 7200)) - 60
        return self._access_token

    def check_login(self) -> bool:
        try:
            self._ensure_token()
            return True
        except Exception:
            return False

    def _api(self, path: str, payload: dict, retry: bool = True) -> dict:
        self._ensure_token()
        resp = self.session.post(
            f"{API_BASE}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=20,
        )
        if resp.status_code == 401 and retry:
            self._access_token = ""  # 强制刷新
            return self._api(path, payload, retry=False)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("code")):
            msg = (data or {}).get("message") or (data or {}).get("code") or f"HTTP {resp.status_code}"
            raise NetdiskError(f"阿里云盘请求失败 ({path}): {msg}")
        return data or {}

    def _ensure_drive_id(self) -> str:
        if not self.drive_id:
            info = self._api("/adrive/v1.0/user/getDriveInfo", {})
            self.drive_id = info.get("default_drive_id") or info.get("resource_drive_id") or ""
            if not self.drive_id:
                raise NetdiskError("无法获取阿里云盘 drive_id")
        return self.drive_id

    # ------------------------------------------------------------------
    # 路径 <-> file_id
    # ------------------------------------------------------------------
    def _list_items(self, parent_file_id: str) -> list[dict]:
        data = self._api(
            "/adrive/v1.0/openFile/list",
            {"drive_id": self._ensure_drive_id(), "parent_file_id": parent_file_id, "limit": 100, "order_by": "name", "order_direction": "ASC"},
        )
        return data.get("items", []) or []

    def _resolve(self, path: str) -> tuple[str, dict]:
        """把路径解析为 (file_id, 文件条目)。"""
        path = path.strip("/")
        parent = "root"
        item: dict = {}
        if path:
            for name in path.split("/"):
                found = None
                for it in self._list_items(parent):
                    if it.get("name") == name:
                        found = it
                        break
                if found is None:
                    raise NetdiskError(f"未找到: /{path}")
                parent = found["file_id"]
                item = found
        else:
            item = {"file_id": "root", "type": "folder", "name": "/"}
        return parent, item

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------
    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        fid, _ = self._resolve(dir_path)
        base = "" if dir_path == "/" else dir_path.rstrip("/")
        out = []
        for it in self._list_items(fid):
            is_dir = it.get("type") == "folder"
            out.append(
                NetdiskFile(
                    fs_id=int(abs(hash(it["file_id"])) % (10 ** 15)),
                    path=f"{base}/{it['name']}",
                    name=it["name"],
                    size=int(it.get("size", 0) or 0),
                    is_dir=is_dir,
                    server_mtime=0,
                    md5=it.get("content_hash", "") or "",
                    raw=it,
                )
            )
        return out

    def get_direct_link(self, path: str, fs_id=None) -> str:
        fid, item = self._resolve(path)
        if item.get("type") == "folder":
            raise NetdiskError("目录不能下载, 请指定文件")
        data = self._api("/adrive/v1.0/openFile/getDownloadUrl", {"drive_id": self._ensure_drive_id(), "file_id": fid})
        url = data.get("url")
        if not url:
            raise NetdiskError("获取阿里云盘下载链接失败")
        return url

    def download_headers(self, path: str) -> dict:
        return {"Referer": "https://www.alipan.com/"}

    # ------------------------------------------------------------------
    # 文件管理 / 搜索
    # ------------------------------------------------------------------
    def search(self, keyword: str, dir_path: str = "/", recursion: bool = True) -> list[NetdiskFile]:
        data = self._api(
            "/adrive/v1.0/openFile/search",
            {"drive_id": self._ensure_drive_id(), "query": f"name like '%{keyword}%'", "limit": 100},
        )
        out = []
        for it in data.get("items", []) or []:
            is_dir = it.get("type") == "folder"
            out.append(
                NetdiskFile(
                    fs_id=int(abs(hash(it["file_id"])) % (10 ** 15)),
                    path=it.get("name", ""),
                    name=it["name"],
                    size=int(it.get("size", 0) or 0),
                    is_dir=is_dir,
                    md5=it.get("content_hash", "") or "",
                    raw=it,
                )
            )
        return out

    def mkdir(self, path: str) -> None:
        parent_path = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        fid, _ = self._resolve(parent_path)
        self._api(
            "/adrive/v1.0/openFile/create",
            {"drive_id": self._ensure_drive_id(), "parent_file_id": fid, "name": name, "type": "folder", "check_name_mode": "refuse"},
        )

    def delete(self, *paths: str) -> None:
        for p in paths:
            fid, _ = self._resolve(p)
            self._api("/adrive/v1.0/openFile/delete", {"drive_id": self._ensure_drive_id(), "file_id": fid})

    def rename(self, path: str, newname: str) -> None:
        fid, _ = self._resolve(path)
        self._api("/adrive/v1.0/openFile/update", {"drive_id": self._ensure_drive_id(), "file_id": fid, "name": newname})

    def move(self, src: str, dest_dir: str) -> None:
        fid, _ = self._resolve(src)
        to_fid, _ = self._resolve(dest_dir)
        self._api("/adrive/v1.0/openFile/move", {"drive_id": self._ensure_drive_id(), "file_id": fid, "to_parent_file_id": to_fid})
