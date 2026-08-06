"""直链多线程下载器。

传输优化核心:
- 分段并发: 文件按固定块大小切分为多个区间, 用线程池并发请求 Range 下载
- 细粒度断点续传: 每下载完一个块立即写入正式文件并保存状态, 中断后可从已完成的块继续
- 失败重试: 单块下载失败自动重试, 附带指数退避
- 实时统计: 汇总下载进度与速度, 回调给 UI 层

线程安全约定: 各块下载区间互不重叠, 写入正式文件时按块偏移 seek 后写入。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

# 单块大小: 断点续传的粒度
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB
# 单块下载重试次数
MAX_RETRIES = 5
# 连接/读取超时 (秒)
TIMEOUT = 30
# 最大并发线程数
MAX_THREADS = 32

# 进度回调签名: on_progress(downloaded_bytes, total_bytes, speed_bps, status_msg)
ProgressCallback = Callable[[int, int, float, str], None]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://pan.baidu.com/disk/home",
}


@dataclass
class DownloadStats:
    """下载统计信息。"""

    total: int = 0
    downloaded: int = 0
    speed_bps: float = 0.0
    start_time: float = field(default_factory=time.time)
    done: bool = False
    error: Optional[str] = None


class DirectLinkDownloader:
    """基于直链的多线程下载器。

    用法::

        dl = DirectLinkDownloader(url, dest_path, threads=8)
        dl.download(on_progress=my_cb)
    """

    def __init__(
        self,
        url: str,
        dest_path: str,
        threads: int = 4,
        headers: Optional[dict] = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self.url = url
        self.dest_path = os.path.abspath(dest_path)
        self.threads = max(1, min(threads, MAX_THREADS))
        self.chunk_size = chunk_size
        self.headers = dict(DEFAULT_HEADERS)
        if headers:
            self.headers.update(headers)

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.stats = DownloadStats()
        # 块列表: {"start": int, "end": int, "done": bool}
        self._blocks: list[dict] = []
        self._block_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._part_path = self.dest_path + ".part"
        self._meta_path = self.dest_path + ".download.json"

    # ------------------------------------------------------------------
    # 文件信息探测
    # ------------------------------------------------------------------
    def probe(self) -> tuple[int, bool]:
        """探测文件总大小与是否支持 Range 分段。

        返回 (total_size, supports_range)。
        """
        total = 0
        accepts_ranges = False
        try:
            resp = self.session.head(self.url, timeout=TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            accepts_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
        except requests.RequestException:
            # 某些 CDN 不支持 HEAD, 回退为 Range 探测
            resp = self.session.get(
                self.url,
                headers={**self.headers, "Range": "bytes=0-0"},
                timeout=TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
            resp.close()
            if resp.status_code == 206:
                content_range = resp.headers.get("Content-Range", "")
                total = int(content_range.split("/")[-1]) if "/" in content_range else 0
                accepts_ranges = True
            elif resp.status_code == 200:
                total = int(resp.headers.get("Content-Length") or 0)
            else:
                resp.raise_for_status()
        return total, accepts_ranges

    # ------------------------------------------------------------------
    # 块管理
    # ------------------------------------------------------------------
    def _build_blocks(self, total: int) -> None:
        """按固定块大小把文件划分为若干区间。"""
        self._blocks = []
        for start in range(0, total, self.chunk_size):
            end = min(total - 1, start + self.chunk_size - 1)
            self._blocks.append({"start": start, "end": end, "done": False})

    def _load_state(self, total: int) -> int:
        """加载断点状态, 返回已下载字节数。"""
        if not os.path.exists(self._meta_path):
            return 0
        try:
            with open(self._meta_path, "r", encoding="utf-8") as fp:
                state = json.load(fp)
            if state.get("url") != self.url or int(state.get("total", 0)) != total:
                return 0
            saved = {b["start"]: b["done"] for b in state.get("blocks", [])}
            restored = 0
            for block in self._blocks:
                if saved.get(block["start"], False):
                    block["done"] = True
                    restored += block["end"] - block["start"] + 1
            return restored
        except (OSError, ValueError, KeyError):
            return 0

    def _save_state(self) -> None:
        """把当前块进度写入状态文件 (供断点续传)。"""
        try:
            with open(self._meta_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {"url": self.url, "total": self.stats.total, "blocks": self._blocks},
                    fp,
                    ensure_ascii=False,
                )
        except OSError:
            pass

    def _remove_state(self) -> None:
        try:
            if os.path.exists(self._meta_path):
                os.remove(self._meta_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 下载核心
    # ------------------------------------------------------------------
    def _worker(self) -> None:
        """工作线程: 从队列取块并下载。"""
        while not self._stop.is_set():
            try:
                block = self._block_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._download_block(block)
            finally:
                self._block_queue.task_done()

    def _download_block(self, block: dict) -> None:
        """下载单个块, 写入正式文件对应偏移, 完成后保存状态。"""
        if self._stop.is_set():
            return
        start, end = block["start"], block["end"]
        expected = end - start + 1
        retries = 0
        err: Optional[Exception] = None

        while not self._stop.is_set():
            try:
                resp = self.session.get(
                    self.url,
                    headers={**self.headers, "Range": f"bytes={start}-{end}"},
                    timeout=TIMEOUT,
                    stream=True,
                )
                if resp.status_code not in (200, 206):
                    raise requests.RequestException(f"HTTP {resp.status_code}")

                buf = io.BytesIO()
                for data in resp.iter_content(64 * 1024):
                    if not data:
                        continue
                    buf.write(data)
                    with self._lock:
                        self.stats.downloaded += len(data)
                if buf.tell() != expected:
                    raise requests.RequestException(
                        f"响应长度不匹配 (期望 {expected}, 实际 {buf.tell()})"
                    )

                with self._write_lock:
                    with open(self._part_path, "r+b") as fh:
                        fh.seek(start)
                        fh.write(buf.getvalue())
                block["done"] = True
                with self._lock:
                    self._save_state()
                return
            except requests.RequestException as exc:
                err = exc
                retries += 1
                if retries > MAX_RETRIES:
                    with self._lock:
                        if self.stats.error is None:
                            self.stats.error = f"块 [{start}-{end}] 下载失败: {exc}"
                    self._stop.set()
                    return
                time.sleep(min(2 ** (retries - 1), 8))

        # 被取消: 记录未完成原因
        if err is not None and not self.stats.error:
            with self._lock:
                self.stats.error = f"块 [{start}-{end}] 未完成: {err}"

    def download(self, on_progress: Optional[ProgressCallback] = None) -> DownloadStats:
        """执行多线程下载。on_progress 回调用于进度展示。"""
        total, supports_range = self.probe()
        self.stats.total = total

        if total == 0:
            # 空文件直接创建
            with open(self.dest_path, "wb"):
                pass
            self.stats.done = True
            return self.stats

        if not supports_range:
            # 服务端不支持 Range: 退化为单线程整体下载
            self._download_plain(on_progress)
            return self.stats

        self._build_blocks(total)

        # 初始化 .part 文件 (定长稀疏文件 — 仅在首次下载时创建, 续传时复用)
        if not os.path.exists(self._part_path):
            with open(self._part_path, "wb") as fh:
                fh.truncate(total)
        else:
            existing = os.path.getsize(self._part_path)
            if existing != total:
                raise RuntimeError(
                    f"断点文件大小 ({existing}) 与服务器声明 ({total}) 不一致, "
                    f"请删除 {self._part_path} 后重试"
                )

        # 恢复断点
        restored = self._load_state(total)
        with self._lock:
            self.stats.downloaded += restored

        # 未完成块入队
        for block in self._blocks:
            if not block["done"]:
                self._block_queue.put(block)

        stop_event = self._stop
        threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.threads)]
        for t in threads:
            t.start()

        # 统计线程: 计算实时速度并触发进度回调
        speed_win: list[float] = []
        last_downloaded = self.stats.downloaded

        def _speed_loop() -> None:
            nonlocal last_downloaded
            while not stop_event.is_set():
                time.sleep(0.5)
                with self._lock:
                    now = self.stats.downloaded
                speed_win.append((now - last_downloaded) * 2)
                last_downloaded = now
                if len(speed_win) > 4:
                    speed_win.pop(0)
                with self._lock:
                    self.stats.speed_bps = sum(speed_win) / len(speed_win)
                if on_progress:
                    with self._lock:
                        d, t = self.stats.downloaded, self.stats.total
                        sp = self.stats.speed_bps
                    on_progress(d, t, sp, "下载中...")

        speed_thread = threading.Thread(target=_speed_loop, daemon=True)
        speed_thread.start()

        # 等待所有工作线程结束 (取消时线程会停止取块并退出)
        for t in threads:
            t.join()

        stop_event.set()
        speed_thread.join(timeout=1)

        with self._lock:
            completed = all(b["done"] for b in self._blocks)

        if completed and not self.stats.error:
            os.replace(self._part_path, self.dest_path)
            self._remove_state()
            self.stats.done = True
            self.stats.speed_bps = 0.0
            if on_progress:
                on_progress(self.stats.total, self.stats.total, 0.0, "完成")
        else:
            self.stats.speed_bps = 0.0
            if on_progress:
                with self._lock:
                    d, t, err = self.stats.downloaded, self.stats.total, self.stats.error
                on_progress(d, t, 0.0, f"已暂停 ({err or '未完成'})")

        return self.stats

    def _download_plain(self, on_progress: Optional[ProgressCallback] = None) -> None:
        """不支持 Range 时的整体下载 (单连接 + 失败重试)。"""
        retries = 0
        while retries <= MAX_RETRIES:
            try:
                resp = self.session.get(self.url, headers=self.headers, timeout=TIMEOUT, stream=True)
                resp.raise_for_status()
                with open(self._part_path, "wb") as fh:
                    for data in resp.iter_content(64 * 1024):
                        if self._stop.is_set():
                            return
                        if not data:
                            continue
                        fh.write(data)
                        with self._lock:
                            self.stats.downloaded += len(data)
                        if on_progress:
                            with self._lock:
                                d, t = self.stats.downloaded, self.stats.total
                            on_progress(d, t, 0.0, "下载中...")
                break
            except requests.RequestException as exc:
                retries += 1
                if retries > MAX_RETRIES:
                    with self._lock:
                        self.stats.error = f"下载失败: {exc}"
                    if on_progress:
                        on_progress(self.stats.downloaded, self.stats.total, 0.0, str(exc))
                    return
                time.sleep(2 ** retries)

        os.replace(self._part_path, self.dest_path)
        self.stats.done = True
        if on_progress:
            on_progress(self.stats.total, self.stats.total, 0.0, "完成")

    def cancel(self) -> None:
        """取消下载 (保留断点状态, 可续传)。"""
        self._stop.set()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def md5_of_file(path: str, block: int = 8 * 1024 * 1024) -> str:
        """计算文件 MD5 (用于校验下载完整性)。"""
        md5 = hashlib.md5()
        with open(path, "rb") as fh:
            while True:
                data = fh.read(block)
                if not data:
                    break
                md5.update(data)
        return md5.hexdigest()
