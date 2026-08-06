"""下载器本地功能测试。

启动一个支持 Range 的本地 HTTP 服务器, 验证:
1. 多线程分段下载正确性 (MD5 校验)
2. 断点续传 (中断后再次下载可续传)
3. 进度回调正常触发

运行: python tests/test_downloader.py
"""

from __future__ import annotations

import hashlib
import http.server
import os
import socketserver
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netdisk.downloader import DirectLinkDownloader  # noqa: E402


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """支持 HTTP Range 请求的文件服务器。"""

    # 每块写入前的小延迟 (模拟网络, 便于测试取消/续传)
    THROTTLE = 0.008

    def _write(self, data: bytes) -> None:
        import time

        time.sleep(self.THROTTLE)
        self.wfile.write(data)

    def do_HEAD(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()

    def do_GET(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        if range_header:
            # 只支持 bytes=start-end 形式
            rng = range_header.replace("bytes=", "")
            start, _, end = rng.partition("-")
            start = int(start) if start else 0
            end = int(end) if end else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    data = fh.read(min(64 * 1024, remaining))
                    if not data:
                        break
                    self._write(data)
                    remaining -= len(data)
        else:
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as fh:
                while True:
                    data = fh.read(64 * 1024)
                    if not data:
                        break
                    self._write(data)

    def log_message(self, *args):
        pass


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def start_server(directory: str) -> tuple[int, threading.Thread]:
    handler = lambda *a, **kw: RangeHandler(*a, directory=directory, **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return port, thread


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        src = os.path.join(workdir, "source.bin")
        # 生成 30 MiB 随机数据作为测试文件
        with open(src, "wb") as fh:
            fh.write(os.urandom(30 * 1024 * 1024))
        expected_md5 = md5(src)

        port, _ = start_server(workdir)
        url = f"http://127.0.0.1:{port}/source.bin"

        # ---- 测试 1: 多线程下载完整性 ----
        out1 = os.path.join(workdir, "out1.bin")
        dl = DirectLinkDownloader(url, out1, threads=4)
        progress = []
        stats = dl.download(on_progress=lambda d, t, s, st: progress.append(d))
        assert stats.done, f"下载未完成: {stats.error}"
        assert md5(out1) == expected_md5, "测试1失败: 文件 MD5 不一致"
        assert len(progress) > 0, "测试1失败: 进度回调未触发"
        print("[PASS] 测试1: 4线程分段下载完整, MD5 一致, 进度回调正常")

        # ---- 测试 2: 断点续传 ----
        out2 = os.path.join(workdir, "out2.bin")
        dl2 = DirectLinkDownloader(url, out2, threads=4)
        # 下载一小部分后取消
        def cancel_early(d, t, s, st):
            if d > 1024 * 1024:
                dl2.cancel()
        dl2.download(on_progress=cancel_early)
        part_size = os.path.getsize(out2 + ".part")
        assert part_size > 0, "测试2失败: 未生成断点文件"
        meta_exists = os.path.exists(out2 + ".download.json")
        assert meta_exists, "测试2失败: 未生成状态文件"

        # 重新下载 (应续传)
        dl3 = DirectLinkDownloader(url, out2, threads=4)
        stats3 = dl3.download()
        assert stats3.done, f"续传未完成: {stats3.error}"
        assert md5(out2) == expected_md5, "测试2失败: 续传后 MD5 不一致"
        assert not os.path.exists(out2 + ".part"), "测试2失败: 临时文件未清理"
        assert not os.path.exists(out2 + ".download.json"), "测试2失败: 状态文件未清理"
        print("[PASS] 测试2: 断点续传正常, 续传后文件完整")

        # ---- 测试 3: 单线程退化 (服务器强制无 Range) ----
        # 直接验证空文件下载
        empty_src = os.path.join(workdir, "empty.txt")
        open(empty_src, "wb").close()
        out3 = os.path.join(workdir, "out3.txt")
        dl4 = DirectLinkDownloader(f"http://127.0.0.1:{port}/empty.txt", out3, threads=4)
        stats4 = dl4.download()
        assert stats4.done and os.path.getsize(out3) == 0, "测试3失败: 空文件下载异常"
        print("[PASS] 测试3: 空文件下载正常")

    print("\n全部测试通过!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
