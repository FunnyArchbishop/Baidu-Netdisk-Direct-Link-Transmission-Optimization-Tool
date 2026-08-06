"""配置管理: 持久化 BDUSS 凭证与下载偏好。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


@dataclass
class AppConfig:
    """程序配置。"""

    bduss: str = ""
    download_dir: str = field(default_factory=lambda: os.path.join(os.path.expanduser("~"), "Downloads", "网盘下载"))
    threads: int = 4
    auto_retry: bool = True

    def ensure_download_dir(self) -> str:
        os.makedirs(self.download_dir, exist_ok=True)
        return self.download_dir


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """从磁盘加载配置, 不存在则返回默认值。"""
    if not os.path.exists(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        cfg = AppConfig()
        for key in asdict(cfg):
            if key in data:
                setattr(cfg, key, data[key])
        return cfg
    except (OSError, ValueError, TypeError):
        return AppConfig()


def save_config(cfg: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    """保存配置到磁盘。"""
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(asdict(cfg), fp, ensure_ascii=False, indent=2)
