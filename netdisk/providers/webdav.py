"""WebDAV 网盘提供者。

适用于所有支持 WebDAV 协议的服务: 坚果云、Nextcloud、ownCloud、
以及部分天翼云/和彩云等。使用 HTTP Basic 认证 (用户名 + 应用密码)。

接口映射:
- 列目录   -> PROPFIND (Depth: 1)
- 下载直链 -> 文件 URL (带 Authorization 头, 由下载器携带)
- 新建目录 -> MKCOL
- 删除     -> DELETE
- 移动/重命名 -> MOVE
- 复制     -> COPY
"""

from __future__ import annotations

import base64
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from ..api import AuthError, NetdiskError, NetdiskFile
from .base import NetdiskProvider


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


class WebDAVProvider(NetdiskProvider):
    name = "webdav"

    def __init__(self, base_url: str, username: str = "", password: str = "", proxy: str = "") -> None:
        base_url = (base_url or "").rstrip("/")
        if not base_url:
            raise AuthError("WebDAV 地址不能为空")
        self.base_url = base_url
        self.username = username or ""
        self.password = password or ""

        self.session = requests.Session()
        if self.username:
            self.session.auth = (self.username, self.password)
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({"User-Agent": "pan-opt/1.0"})

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _auth_header(self) -> dict:
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        return {}

    def _href(self, path: str) -> str:
        path = path if path.startswith("/") else "/" + path
        return self.base_url + urllib.parse.quote(path, safe="/")

    def _propfind(self, path: str, depth: str = "1") -> requests.Response:
        resp = self.session.request("PROPFIND", self._href(path), headers={"Depth": depth}, timeout=30)
        if resp.status_code in (401, 403):
            raise AuthError("WebDAV 认证失败, 请检查用户名/密码 (坚果云需使用应用密码)")
        resp.raise_for_status()
        return resp

    def _parse_multistatus(self, xml_text: str, base_path: str) -> list[NetdiskFile]:
        ns = {"d": "DAV:"}
        root = ET.fromstring(xml_text)
        files: list[NetdiskFile] = []
        base_norm = base_path.rstrip("/")
        for response in root.findall("d:response", ns):
            href = _text(response.find("d:href", ns))
            path = urllib.parse.unquote(href)
            if path.startswith(self.base_url):
                path = path[len(self.base_url):] or "/"
            path = "/" + path.lstrip("/")
            if path.rstrip("/") == base_norm:
                continue  # 跳过当前目录自身

            propstat = response.find("d:propstat", ns)
            prop = propstat.find("d:prop", ns) if propstat is not None else None
            props = {}
            if prop is not None:
                for child in prop:
                    props[child.tag.split("}")[-1]] = child

            is_dir = props.get("resourcetype") is not None and props["resourcetype"].find("{DAV:}collection") is not None
            size = int(_text(props.get("getcontentlength")) or 0)
            name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
            files.append(
                NetdiskFile(
                    fs_id=0,
                    path=path,
                    name=name,
                    size=size,
                    is_dir=is_dir,
                    server_mtime=0,
                    md5="",
                    raw={"href": href},
                )
            )
        return files

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------
    def check_login(self) -> bool:
        try:
            self._propfind("/", depth="0")
            return True
        except Exception:
            return False

    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        resp = self._propfind(dir_path, depth="1")
        return self._parse_multistatus(resp.text, dir_path)

    def get_direct_link(self, path: str, fs_id=None) -> str:
        return self._href(path)

    def download_headers(self, path: str) -> dict:
        return self._auth_header()

    # ------------------------------------------------------------------
    # 文件管理
    # ------------------------------------------------------------------
    def mkdir(self, path: str) -> None:
        resp = self.session.request("MKCOL", self._href(path), timeout=30)
        if resp.status_code >= 400:
            raise NetdiskError(f"创建目录失败: HTTP {resp.status_code}")

    def delete(self, *paths: str) -> None:
        for p in paths:
            resp = self.session.request("DELETE", self._href(p), timeout=30)
            if resp.status_code >= 400:
                raise NetdiskError(f"删除失败: HTTP {resp.status_code}")

    def move(self, src: str, dest_dir: str) -> None:
        dest = (dest_dir.rstrip("/") + "/" + src.rstrip("/").rsplit("/", 1)[-1]).rstrip("/")
        headers = {"Destination": self._href(dest)}
        resp = self.session.request("MOVE", self._href(src), headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise NetdiskError(f"移动失败: HTTP {resp.status_code}")

    def rename(self, path: str, newname: str) -> None:
        parent = path.rsplit("/", 1)[0] or "/"
        self.move(path, parent.rstrip("/") + "/" + newname)

    def copy(self, src: str, dest_dir: str) -> None:
        dest = (dest_dir.rstrip("/") + "/" + src.rstrip("/").rsplit("/", 1)[-1]).rstrip("/")
        headers = {"Destination": self._href(dest)}
        resp = self.session.request("COPY", self._href(src), headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise NetdiskError(f"复制失败: HTTP {resp.status_code}")
