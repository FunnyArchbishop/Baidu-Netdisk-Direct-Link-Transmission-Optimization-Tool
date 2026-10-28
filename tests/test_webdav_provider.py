"""WebDAV provider 本地集成测试。

启动一个最小 WebDAV 服务器 (PROPFIND/GET/HEAD/MKCOL/DELETE/MOVE),
验证 WebDAVProvider 的列目录、直链解析、鉴权头、下载、目录/文件管理。
运行: python tests/test_webdav_provider.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netdisk.api import NetdiskError  # noqa: E402
from netdisk.downloader import DirectLinkDownloader  # noqa: E402
from netdisk.providers.webdav import WebDAVProvider  # noqa: E402

DAV = "DAV:"


def _build_multistatus(entries: list[tuple[str, bool, int]]) -> str:
    parts = ['<?xml version="1.0" encoding="utf-8"?>', f'<D:multistatus xmlns:D="{DAV}">']
    for path, is_dir, size in entries:
        rt = "<D:resourcetype><D:collection/></D:resourcetype>" if is_dir else "<D:resourcetype/>"
        cl = "" if is_dir else f"<D:getcontentlength>{size}</D:getcontentlength>"
        display = os.path.basename(path.rstrip("/")) or "/"
        parts.append(
            f"<D:response><D:href>{path}</D:href><D:propstat><D:prop>"
            f"{rt}{cl}<D:displayname>{display}</D:displayname></D:prop>"
            f"<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )
    parts.append("</D:multistatus>")
    return "".join(parts)


class WebDAVHandler(http.server.BaseHTTPRequestHandler):
    root = ""

    def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _disk(self) -> str:
        p = urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/")
        return os.path.join(self.root, p) if p else self.root

    def _entries(self, depth: str) -> list[tuple[str, bool, int]]:
        target = self._disk()
        results: list[tuple[str, bool, int]] = []
        if os.path.isdir(target):
            results.append((target, True, 0))
            if depth == "1":
                for name in os.listdir(target):
                    fp = os.path.join(target, name)
                    results.append((fp, os.path.isdir(fp), 0 if os.path.isdir(fp) else os.path.getsize(fp)))
        elif os.path.isfile(target):
            results.append((target, False, os.path.getsize(target)))
        rel = []
        for fp, isdir, size in results:
            relp = os.path.relpath(fp, self.root).replace("\\", "/")
            rel.append(("/" + relp if relp != "." else "/", isdir, size))
        return rel

    def do_PROPFIND(self):
        xml = _build_multistatus(self._entries(self.headers.get("Depth", "1")))
        self._send(207, xml.encode("utf-8"), {"Content-Type": "application/xml; charset=utf-8"})

    def do_HEAD(self):
        fp = self._disk()
        if os.path.isfile(fp):
            self._send(200, b"", {"Content-Length": str(os.path.getsize(fp)), "Accept-Ranges": "bytes"})
        else:
            self._send(404)

    def do_GET(self):
        fp = self._disk()
        if not os.path.isfile(fp):
            self._send(404)
            return
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        with open(fp, "rb") as fh:
            if rng:
                spec = rng.replace("bytes=", "")
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                fh.seek(start)
                body = fh.read(end - start + 1)
                self._send(206, body, {"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(len(body))})
            else:
                self._send(200, fh.read(), {"Content-Length": str(size), "Accept-Ranges": "bytes"})

    def do_MKCOL(self):
        try:
            os.makedirs(self._disk(), exist_ok=True)
            self._send(201)
        except OSError:
            self._send(405)

    def do_DELETE(self):
        fp = self._disk()
        try:
            (os.rmdir if os.path.isdir(fp) else os.remove)(fp)
            self._send(204)
        except OSError:
            self._send(404)

    def do_MOVE(self):
        src = self._disk()
        dest = self.headers.get("Destination", "")
        dest_path = urllib.parse.unquote(urllib.parse.urlparse(dest).path).lstrip("/")
        dest_disk = os.path.join(self.root, dest_path)
        try:
            os.rename(src, dest_disk)
            self._send(201)
        except OSError:
            self._send(409)

    def log_message(self, *args):
        pass


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        WebDAVHandler.root = tmp
        # 构造目录结构
        with open(os.path.join(tmp, "file.txt"), "wb") as fh:
            fh.write(b"hello webdav")
        os.makedirs(os.path.join(tmp, "subdir"))
        with open(os.path.join(tmp, "subdir", "inner.bin"), "wb") as fh:
            fh.write(os.urandom(200_000))

        httpd = socketserver.TCPServer(("127.0.0.1", 0), WebDAVHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{port}"

        p = WebDAVProvider(base_url, username="user", password="pass")

        # 1. 登录
        assert p.check_login(), "测试1失败: 登录校验失败"
        print("[PASS] 测试1: WebDAV 登录校验")

        # 2. 列目录
        files = p.list_files("/")
        names = {f.name: f for f in files}
        assert "file.txt" in names and not names["file.txt"].is_dir
        assert names["file.txt"].size == len(b"hello webdav")
        assert "subdir" in names and names["subdir"].is_dir
        print("[PASS] 测试2: 列目录正确 (文件 + 子目录)")

        # 3. 直链 + 鉴权头
        url = p.get_direct_link("/file.txt")
        assert url == base_url + "/file.txt"
        assert p.download_headers("/file.txt")["Authorization"].startswith("Basic ")
        print("[PASS] 测试3: 直链解析与鉴权头正确")

        # 4. 下载 (携带鉴权头)
        dest = os.path.join(tmp, "out.txt")
        dl = DirectLinkDownloader(url=url, dest_path=dest, threads=2, chunk_size=1000, headers=p.download_headers("/file.txt"))
        stats = dl.download()
        assert stats.done, f"下载失败: {stats.error}"
        with open(dest, "rb") as fh:
            assert fh.read() == b"hello webdav", "测试4失败: 内容不一致"
        print("[PASS] 测试4: 通过直链多线程下载正确")

        # 5. mkdir / move / delete
        p.mkdir("/newdir")
        assert os.path.isdir(os.path.join(tmp, "newdir"))
        p.move("/file.txt", "/newdir")
        assert os.path.isfile(os.path.join(tmp, "newdir", "file.txt"))
        p.delete("/newdir/file.txt")
        assert not os.path.exists(os.path.join(tmp, "newdir", "file.txt"))
        print("[PASS] 测试5: mkdir/move/delete 正确")

        # 6. 不支持的操作应抛错
        try:
            p.search("x")
            assert False, "应抛出 NetdiskError"
        except NetdiskError:
            pass
        print("[PASS] 测试6: 不支持的搜索抛 NetdiskError")

        httpd.shutdown()

    print("\n全部 WebDAV provider 测试通过!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
