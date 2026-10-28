"""配置管理: 持久化 BDUSS 凭证与下载偏好。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field


def app_dir() -> str:
    """可写的应用数据目录。

    打包成单文件 exe 后 (sys.frozen) 指向 exe 所在目录, 否则指向项目根目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DEFAULT_CONFIG_PATH = os.path.join(app_dir(), "config.json")

_DEFAULT_DOWNLOAD_DIR = os.path.join(
    os.path.expanduser("~"), "Downloads", "网盘下载"
)


@dataclass
class AppConfig:
    """程序配置。"""

    baidu_cookies: list = field(default_factory=list)
    baidu_user_agent: str = ""
    bduss: str = ""
    download_dir: str = field(default_factory=lambda: _DEFAULT_DOWNLOAD_DIR)
    threads: int = 4
    auto_retry: bool = True
    speed_limit: int = 0  # 下载限速 (字节/秒), 0 表示不限速
    proxy: str = ""  # 可选 HTTP 代理, 例如 http://127.0.0.1:7890
    max_concurrent: int = 2  # Web 界面同时下载的最大任务数
    provider: str = "baidu"  # 当前网盘提供者: baidu / webdav / aliyun / quark / iflytek
    webdav_url: str = ""  # WebDAV 服务地址
    username: str = ""  # WebDAV 用户名
    password: str = ""  # WebDAV 密码 / 应用密码
    aliyun_refresh_token: str = ""  # 阿里云盘 Open API refresh_token
    aliyun_drive_id: str = ""  # 阿里云盘 drive_id (留空自动获取)
    quark_cookie: str = ""  # 夸克网盘 Cookie
    iflytek_url: str = ""  # 讯飞网盘服务地址
    iflytek_token: str = ""  # 讯飞网盘 Token

    def ensure_download_dir(self) -> str:
        os.makedirs(self.download_dir, exist_ok=True)
        return self.download_dir

    def sanitize(self) -> "AppConfig":
        """规范化字段, 避免外部配置注入非法值。"""
        if not isinstance(self.baidu_cookies, list):
            self.baidu_cookies = []
        self.baidu_cookies = [
            {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": "/"}
            for c in self.baidu_cookies
            if isinstance(c, dict) and c.get("domain") in (".baidu.com", "baidu.com", "pan.baidu.com", ".pan.baidu.com")
            and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)
        ]
        if not isinstance(self.baidu_user_agent, str):
            self.baidu_user_agent = ""
        if not isinstance(self.bduss, str):
            self.bduss = ""
        if not isinstance(self.download_dir, str) or not self.download_dir.strip():
            self.download_dir = _DEFAULT_DOWNLOAD_DIR
        try:
            self.threads = int(self.threads)
        except (TypeError, ValueError):
            self.threads = 4
        self.threads = max(1, min(self.threads, 32))
        if not isinstance(self.auto_retry, bool):
            self.auto_retry = self.auto_retry in (1, "1", "true", "True", "yes", "on")
        try:
            self.speed_limit = int(self.speed_limit)
        except (TypeError, ValueError):
            self.speed_limit = 0
        self.speed_limit = max(0, self.speed_limit)
        if not isinstance(self.proxy, str):
            self.proxy = ""
        try:
            self.max_concurrent = int(self.max_concurrent)
        except (TypeError, ValueError):
            self.max_concurrent = 2
        self.max_concurrent = max(1, min(self.max_concurrent, 8))
        if not isinstance(self.provider, str) or not self.provider.strip():
            self.provider = "baidu"
        for key in ("webdav_url", "username", "password", "aliyun_refresh_token", "aliyun_drive_id", "quark_cookie", "iflytek_url", "iflytek_token"):
            if not isinstance(getattr(self, key), str):
                setattr(self, key, "")
        return self


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """从磁盘加载配置, 不存在或损坏则返回默认值。"""
    if not os.path.exists(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return AppConfig()
        cfg = AppConfig()
        for key in asdict(cfg):
            if key in data:
                setattr(cfg, key, data[key])
        return cfg.sanitize()
    except (OSError, ValueError, TypeError):
        return AppConfig()


def save_config(cfg: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    """保存配置到磁盘 (原子写入, 避免中断导致配置损坏)。"""
    cfg.sanitize()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(asdict(cfg), fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
