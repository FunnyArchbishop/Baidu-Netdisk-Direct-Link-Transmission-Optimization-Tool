"""下载器核心逻辑自包含测试 (不依赖真实 requests 与网络)。

用一个极简的假 requests 模块在内存中模拟支持 Range 的源文件, 验证:
1. 多线程分段下载正确性
2. 失败重试 + 进度回滚 (修复后进度不虚增)
3. 断点续传
4. 不支持 Range 时的整体下载
5. 空文件
6. 可选 MD5 校验

运行: python tests/test_downloader_stub.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# 构造假 requests 模块并注入 sys.modules, 使 netdisk.downloader 使用它
# ---------------------------------------------------------------------------


class RequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: bytes, fail_mid: bool = False, throttle: float = 0.0):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self._fail_mid = fail_mid
        self._throttle = throttle

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RequestException(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        half = max(1, len(self._body) // 2)
        emitted = 0
        for i in range(0, len(self._body), chunk_size):
            chunk = self._body[i : i + chunk_size]
            if self._throttle:
                time.sleep(self._throttle)
            yield chunk
            emitted += len(chunk)
            if self._fail_mid and emitted >= half:
                raise RequestException("连接中断 (模拟)")

    def close(self) -> None:
        pass


class FakeSession:
    """在内存中模拟一个支持 Range 的源文件。"""

    def __init__(self, data: bytes, supports_range: bool = True):
        self._data = data
        self.supports_range = supports_range
        self.headers: dict = {}
        # 接下来 N 次 get() 在下载中途抛错 (模拟断流)
        self.fail_mid_get = 0
        # 每个分块读取前的延迟 (秒), 用于让取消/进度线程有机会介入
        self.throttle = 0.0

    def head(self, url, timeout=None, allow_redirects=True) -> FakeResponse:
        headers = {"Content-Length": str(len(self._data))}
        if self.supports_range:
            headers["Accept-Ranges"] = "bytes"
        return FakeResponse(200, headers, b"")

    def get(self, url, headers=None, timeout=None, stream=True, allow_redirects=True) -> FakeResponse:
        fail_mid = self.fail_mid_get > 0
        if fail_mid:
            self.fail_mid_get -= 1
        rng = (headers or {}).get("Range")
        if rng:
            spec = rng.replace("bytes=", "")
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else len(self._data) - 1
            end = min(end, len(self._data) - 1)
            body = self._data[start : end + 1]
            return FakeResponse(206, {"Content-Range": f"bytes {start}-{end}/{len(self._data)}"}, body, fail_mid, self.throttle)
        return FakeResponse(200, {"Content-Length": str(len(self._data))}, self._data, fail_mid, self.throttle)


SOURCE = b""  # 由测试填充, Session() 工厂读取


def _session_factory():
    return FakeSession(SOURCE)


_fake = types.ModuleType("requests")
_fake.RequestException = RequestException
_fake.Session = _session_factory
sys.modules["requests"] = _fake

from netdisk.downloader import DirectLinkDownloader  # noqa: E402


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def make_downloader(dest: str, chunk_size: int, threads: int = 4) -> DirectLinkDownloader:
    return DirectLinkDownloader(url="mem://source.bin", dest_path=dest, threads=threads, chunk_size=chunk_size)


def _set_source(data: bytes) -> FakeSession:
    global SOURCE
    SOURCE = data
    session = _session_factory()
    sys.modules["requests"].Session = lambda: session
    return session


def main() -> int:
    # 在项目目录内创建临时目录, 避免受系统临时目录的沙箱限制
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workdir = tempfile.mkdtemp(prefix="dsh_dl_test_", dir=project_root)
    try:
        data = os.urandom(3000)

        # ---- 测试 1: 多线程分段下载正确性 ----
        out1 = os.path.join(workdir, "out1.bin")
        _set_source(data)
        dl = DirectLinkDownloader("mem://s.bin", out1, threads=4, chunk_size=1000)
        progress = []
        stats = dl.download(on_progress=lambda d, t, s, st: progress.append((d, t, st)))
        assert stats.done, f"未完成: {stats.error}"
        with open(out1, "rb") as fh:
            assert fh.read() == data, "测试1失败: 内容不一致"
        assert progress[-1][2] == "完成", f"测试1失败: 终态应为完成, 实际 {progress[-1]}"
        print("[PASS] 测试1: 多线程分段下载内容一致, 终态回调正常")

        # ---- 测试 2: 失败重试 + 进度回滚 ----
        out2 = os.path.join(workdir, "out2.bin")
        session = _set_source(data)
        session.fail_mid_get = 1  # 注入一次断流
        dl2 = DirectLinkDownloader("mem://s.bin", out2, threads=1, chunk_size=1000)
        stats2 = dl2.download()
        assert stats2.done, f"重试后未完成: {stats2.error}"
        with open(out2, "rb") as fh:
            assert fh.read() == data, "测试2失败: 内容不一致"
        assert stats2.downloaded == len(data), (
            f"测试2失败: 进度虚增/不足 (downloaded={stats2.downloaded}, total={len(data)})"
        )
        print("[PASS] 测试2: 断流重试成功, 进度已正确回滚 (无虚增)")

        # ---- 测试 3: 断点续传 ----
        data3 = os.urandom(200_000)
        out3 = os.path.join(workdir, "out3.bin")
        session3 = _set_source(data3)
        session3.throttle = 0.003  # 放慢下载, 让取消有机会介入
        dl3 = DirectLinkDownloader("mem://s.bin", out3, threads=2, chunk_size=1000)

        def cancel_early(d, t, s, st):
            if d > 2000:
                dl3.cancel()

        dl3.download(on_progress=cancel_early)
        assert os.path.exists(out3 + ".part"), "测试3失败: 未生成断点文件"
        assert os.path.exists(out3 + ".download.json"), "测试3失败: 未生成状态文件"

        _set_source(data3)
        dl3b = DirectLinkDownloader("mem://s.bin", out3, threads=2, chunk_size=1000)
        stats3b = dl3b.download()
        assert stats3b.done, f"续传未完成: {stats3b.error}"
        with open(out3, "rb") as fh:
            assert fh.read() == data3, "测试3失败: 续传后内容不一致"
        assert not os.path.exists(out3 + ".part"), "测试3失败: 临时文件未清理"
        assert not os.path.exists(out3 + ".download.json"), "测试3失败: 状态文件未清理"
        print("[PASS] 测试3: 断点续传正常, 续传后文件完整且清理干净")

        # ---- 测试 4: 不支持 Range 的整体下载 ----
        out4 = os.path.join(workdir, "out4.bin")
        session4 = _set_source(data)
        session4.supports_range = False
        dl4 = DirectLinkDownloader("mem://s.bin", out4, threads=4, chunk_size=1000)
        stats4 = dl4.download()
        assert stats4.done, f"整体下载未完成: {stats4.error}"
        with open(out4, "rb") as fh:
            assert fh.read() == data, "测试4失败: 内容不一致"
        print("[PASS] 测试4: 无 Range 退化整体下载正确")

        # ---- 测试 5: 空文件 ----
        out5 = os.path.join(workdir, "out5.bin")
        _set_source(b"")
        dl5 = DirectLinkDownloader("mem://s.bin", out5, threads=4, chunk_size=1000)
        stats5 = dl5.download()
        assert stats5.done and os.path.getsize(out5) == 0, "测试5失败: 空文件下载异常"
        print("[PASS] 测试5: 空文件下载正常")

        # ---- 测试 6: MD5 校验 ----
        out6 = os.path.join(workdir, "out6.bin")
        _set_source(data)
        dl6 = DirectLinkDownloader("mem://s.bin", out6, threads=2, chunk_size=1000)
        stats6 = dl6.download(expected_md5=md5(data))
        assert stats6.done, f"MD5 正确时未完成: {stats6.error}"

        out7 = os.path.join(workdir, "out7.bin")
        _set_source(data)
        dl7 = DirectLinkDownloader("mem://s.bin", out7, threads=2, chunk_size=1000)
        stats7 = dl7.download(expected_md5="0" * 32)
        assert not stats7.done and "MD5" in (stats7.error or ""), "测试6失败: 错误 MD5 未触发校验失败"

        out8 = os.path.join(workdir, "out8.bin")
        _set_source(data)
        dl8 = DirectLinkDownloader("mem://s.bin", out8, threads=2, chunk_size=1000)
        stats8 = dl8.download(expected_md5="not-a-valid-md5")
        assert stats8.done, "测试6失败: 非法 MD5 应跳过校验"
        print("[PASS] 测试6: MD5 校验 (正确通过 / 错误报错 / 非法跳过) 正常")

        # ---- 测试 7: 限速参数下下载正确性 ----
        out9 = os.path.join(workdir, "out9.bin")
        _set_source(data)
        dl9 = DirectLinkDownloader("mem://s.bin", out9, threads=2, chunk_size=1000, speed_limit=10_000_000)
        stats9 = dl9.download()
        assert stats9.done, f"限速下载未完成: {stats9.error}"
        with open(out9, "rb") as fh:
            assert fh.read() == data, "测试7失败: 限速下载内容不一致"
        print("[PASS] 测试7: 限速参数下下载内容正确")

        # ---- 测试 8: 限速真实生效 (宽松时间断言) ----
        data_slow = os.urandom(60_000)  # 60 KB
        out10 = os.path.join(workdir, "out10.bin")
        _set_source(data_slow)
        dl10 = DirectLinkDownloader("mem://s.bin", out10, threads=1, chunk_size=1000, speed_limit=20_000)  # 20 KB/s
        t0 = time.monotonic()
        stats10 = dl10.download()
        elapsed = time.monotonic() - t0
        assert stats10.done, f"限速下载未完成: {stats10.error}"
        with open(out10, "rb") as fh:
            assert fh.read() == data_slow, "测试8失败: 限速下载内容不一致"
        # 60KB @ 20KB/s ≈ 2s (首桶满额度免等待), 取宽松下限
        assert elapsed >= 1.2, f"测试8失败: 限速未生效 (仅 {elapsed:.2f}s)"
        print(f"[PASS] 测试8: 限速生效 (60KB 用时 {elapsed:.1f}s)")

    finally:
        for name in os.listdir(workdir):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

    print("\n全部下载器核心测试通过!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
