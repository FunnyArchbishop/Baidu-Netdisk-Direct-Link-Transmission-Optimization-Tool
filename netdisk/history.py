"""下载历史记录: 以 JSON Lines 形式持久化每次下载。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

from .config import app_dir

DEFAULT_HISTORY_PATH = os.path.join(app_dir(), "download_history.jsonl")


@dataclass
class HistoryRecord:
    """一条下载记录。"""

    path: str  # 网盘路径
    name: str
    size: int
    dest: str  # 本地保存路径
    status: str  # done / failed / cancelled
    speed_bps: float = 0.0
    error: str = ""
    time: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.time))
        return d


def append_record(rec: HistoryRecord, path: str = DEFAULT_HISTORY_PATH) -> None:
    """追加一条记录 (追加式写入, 失败静默忽略)。"""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_records(path: str = DEFAULT_HISTORY_PATH, limit: int = 50) -> list[dict]:
    """读取最近 limit 条记录 (新→旧)。"""
    if not os.path.exists(path):
        return []
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    records.reverse()
    return records[:limit]
