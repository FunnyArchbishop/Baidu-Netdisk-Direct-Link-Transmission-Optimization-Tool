"""网盘提供者统一接口。

所有网盘实现都应遵循此接口：只需实现「认证 + 列目录 + 解析直链」三个核心方法，
其余能力 (搜索 / 文件管理 / 分享) 按需覆盖，未覆盖时抛出「不支持」错误。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..api import AuthError, NetdiskError, NetdiskFile


class NetdiskProvider(ABC):
    """网盘提供者基类。"""

    name: str = "base"  # 提供者标识

    # ------------------------------------------------------------------
    # 必须实现
    # ------------------------------------------------------------------
    @abstractmethod
    def check_login(self) -> bool:
        """校验当前凭证是否有效。"""

    @abstractmethod
    def list_files(self, dir_path: str = "/") -> list[NetdiskFile]:
        """列出目录内容。"""

    @abstractmethod
    def get_direct_link(self, path: str, fs_id=None) -> str:
        """返回文件的下载直链 (支持 Range 分段)。"""

    # ------------------------------------------------------------------
    # 通用默认实现
    # ------------------------------------------------------------------
    def get_file_info(self, path: str) -> "NetdiskFile | None":
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        for f in self.list_files(parent):
            if f.name == name:
                return f
        return None

    def download_headers(self, path: str) -> dict:
        """下载直链所需的额外请求头 (如鉴权 Authorization)。"""
        return {}

    def walk(self, dir_path: str = "/") -> list[NetdiskFile]:
        """递归遍历目录, 返回其中所有文件。"""
        result: list[NetdiskFile] = []
        stack = [dir_path if dir_path.startswith("/") else "/" + dir_path]
        while stack:
            cur = stack.pop()
            for f in self.list_files(cur):
                if f.is_dir:
                    stack.append(f.path)
                else:
                    result.append(f)
        return result

    # ------------------------------------------------------------------
    # 可选能力: 未覆盖时抛 "不支持"
    # ------------------------------------------------------------------
    def search(self, keyword: str, dir_path: str = "/", recursion: bool = True) -> list[NetdiskFile]:
        raise NetdiskError(f"{self.name} 网盘不支持搜索")

    def mkdir(self, path: str) -> None:
        raise NetdiskError(f"{self.name} 网盘不支持创建目录")

    def delete(self, *paths: str) -> None:
        raise NetdiskError(f"{self.name} 网盘不支持删除")

    def rename(self, path: str, newname: str) -> None:
        raise NetdiskError(f"{self.name} 网盘不支持重命名")

    def move(self, src: str, dest_dir: str) -> None:
        raise NetdiskError(f"{self.name} 网盘不支持移动")

    def copy(self, src: str, dest_dir: str) -> None:
        raise NetdiskError(f"{self.name} 网盘不支持复制")

    def list_share_files(self, share_url: str, dir_path: str = "/") -> list[NetdiskFile]:
        raise NetdiskError(f"{self.name} 网盘不支持分享链接")

    def get_share_dlink(self, share_url: str, path: str):
        raise NetdiskError(f"{self.name} 网盘不支持分享下载")
