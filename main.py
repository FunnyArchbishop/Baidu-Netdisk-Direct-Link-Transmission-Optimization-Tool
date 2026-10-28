#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度网盘直链传输优化工具 - 入口。

用法:
    python main.py                    进入交互式命令行
    python main.py --list /我的资料    列出目录内容
    python main.py --download /xx.zip  下载指定文件 (直链+多线程)
    python main.py --threads 8         设置并发线程数
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

# Keep the usual web command working when PATH resolves to MSYS2 Python.
# Only use the explicitly provisioned project environment; never install on startup.
if __name__ == "__main__" and "--web" in sys.argv and not getattr(sys, "frozen", False):
    web_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv-web", "Scripts", "python.exe")
    if os.path.isfile(web_python) and not os.path.samefile(sys.executable, web_python):
        try:
            from playwright.sync_api import sync_playwright as _login_runtime_check
        except (ImportError, OSError):
            import subprocess
            print(f"Using project web environment: {web_python}", flush=True)
            try:
                raise SystemExit(subprocess.call([web_python, os.path.abspath(__file__), *sys.argv[1:]]))
            except KeyboardInterrupt:
                raise SystemExit(130)

# ── 强制终端输出为 UTF-8, 避免 Windows 下中文乱码 ──────────────────────
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from netdisk import __version__
from netdisk.api import NetdiskError, AuthError, human_size
from netdisk.cli import NetdiskCLI
from netdisk.config import load_config, save_config
from netdisk.credentials import CredentialError, build_baidu_credentials
from netdisk.downloader import DirectLinkDownloader
from netdisk.history import HistoryRecord, append_record
from netdisk.providers import create_provider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pan-opt",
        description="基于直链传输技术的百度网盘传输优化工具",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--login", metavar="BDUSS", help="设置登录凭证 (BDUSS)")
    parser.add_argument("--list", metavar="目录", help="列出指定目录内容")
    parser.add_argument("--download", metavar="文件路径", help="下载指定文件")
    parser.add_argument("--output", metavar="本地目录", help="下载文件保存目录 (配合 --download)")
    parser.add_argument("--threads", type=int, metavar="N", help="设置并发线程数 1-32")
    parser.add_argument("--speed", metavar="速率", help="设置下载限速 (字节数或 512k/1m, 0 为不限速)")
    parser.add_argument("--proxy", metavar="URL", help="设置 HTTP 代理 (如 http://127.0.0.1:7890)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果 (便于脚本调用)")
    parser.add_argument("--web", action="store_true", help="启动 Web 管理界面")
    parser.add_argument("--host", default="127.0.0.1", help="Web 界面监听地址 (配合 --web)")
    parser.add_argument("--port", type=int, default=5000, help="Web 界面端口 (配合 --web)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config()

    # 更新配置
    if args.login:
        # 既接受单个 BDUSS, 也接受浏览器里整段 Cookie (BDUSS 单值在部分账号上不够用)
        try:
            creds = build_baidu_credentials(args.login)
        except CredentialError as exc:
            print(f"[!] {exc}")
            return 1
        candidate = replace(
            cfg, provider="baidu", bduss=creds.bduss, baidu_cookies=creds.cookies
        ).sanitize()
        try:
            provider = create_provider("baidu", candidate)
            ok = provider.check_login()
        except (AuthError, NetdiskError) as exc:
            ok = False
            print(f"[!] {exc}")
            provider = None
        if not ok:
            detail = str(getattr(provider, "last_error", "") or "百度没接受这份凭证")
            if args.json:
                print(json.dumps({"error": detail}, ensure_ascii=False))
            else:
                print(f"[!] 登录校验失败: {detail}")
            return 1
        save_config(candidate)
        if args.json:
            print(json.dumps({"ok": True, "cookies": creds.cookie_names, "notes": creds.notes}, ensure_ascii=False))
        else:
            print(f"[OK] 已保存 {len(creds.cookies)} 个 Cookie, 登录校验通过")
            for note in creds.notes:
                print(f"[i] {note}")
        return 0
    if args.threads:
        if not 1 <= args.threads <= 32:
            print("[!] 线程数需为 1-32")
            return 1
        cfg.threads = args.threads
        save_config(cfg)
        print(f"[OK] 并发线程数已设为 {args.threads}")
        return 0
    if args.speed:
        try:
            cfg.speed_limit = NetdiskCLI._parse_speed(args.speed)
        except ValueError:
            print("[!] 限速值无效 (支持字节数或 512k/1m 后缀, 0 为不限速)")
            return 1
        save_config(cfg)
        print(f"[OK] 下载限速已设为 {args.speed}")
        return 0
    if args.proxy:
        cfg.proxy = args.proxy if args.proxy.lower() not in ("none", "off") else ""
        save_config(cfg)
        print(f"[OK] 代理已设为 {args.proxy}")
        return 0

    # 无参数时进入交互模式
    if not args.list and not args.download and not args.web:
        NetdiskCLI(cfg).run()
        return 0

    # Web 界面
    if args.web:
        from netdisk.webui import run as run_web

        print(f"Web 界面已启动: http://{args.host}:{args.port}")
        print("按 Ctrl+C 停止")
        run_web(host=args.host, port=args.port)
        return 0

    # 一次性模式
    if not cfg.bduss and cfg.provider == "baidu":
        print("[!] 尚未登录, 请先运行: python main.py --login <BDUSS>")
        return 1
    if cfg.provider == "webdav" and not cfg.webdav_url:
        print("[!] 尚未配置 WebDAV 地址, 请编辑 config.json 或使用 CLI: set webdav_url <URL>")
        return 1

    api = create_provider(cfg.provider, cfg)
    if not api.check_login():
        print("[!] 登录校验失败, 请检查凭证 (百度用 --login <BDUSS> 更新)")
        return 1

    if args.list:
        try:
            files = api.list_files(args.list)
            if args.json:
                print(json.dumps([f.to_dict() for f in files], ensure_ascii=False))
            else:
                for f in files:
                    mark = "[DIR]" if f.is_dir else "     "
                    print(f"{mark}  {f.name:<40} {f.size_human:>10}")
                print(f"\n共 {len(files)} 项")
        except (AuthError, NetdiskError) as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            else:
                print(f"[!] {exc}")
            return 1
        return 0

    if args.download:
        path = args.download
        try:
            info = api.get_file_info(path)
            if info is None:
                raise NetdiskError(f"未找到文件: {path}")
            if info.is_dir:
                raise NetdiskError("目标为目录, 请指定具体文件")
            url = api.get_direct_link(path, info.fs_id)
        except (AuthError, NetdiskError) as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            else:
                print(f"[!] 获取直链失败: {exc}")
            return 1

        local_dir = args.output or cfg.ensure_download_dir()
        if args.output:
            os.makedirs(local_dir, exist_ok=True)
        dest = os.path.join(local_dir, info.name)
        if os.path.exists(dest) and not os.path.exists(dest + ".download.json"):
            if args.json:
                print(json.dumps({"name": info.name, "dest": dest, "done": True, "skipped": True}, ensure_ascii=False))
            else:
                print(f"[!] 文件已存在, 跳过: {dest}")
            return 0

        if not args.json:
            print(f"开始下载 {info.name} ({info.size_human}) -> {dest}")
            print(f"线程数: {cfg.threads}  限速: {human_size(cfg.speed_limit) + '/s' if cfg.speed_limit else '不限'}")

        cli = NetdiskCLI(cfg)
        dl = DirectLinkDownloader(url=url, dest_path=dest, threads=cfg.threads, speed_limit=cfg.speed_limit, proxy=cfg.proxy, headers=api.download_headers(path))
        progress_cb = None if args.json else cli._on_progress
        try:
            stats = dl.download(on_progress=progress_cb, expected_md5=info.md5)
        except KeyboardInterrupt:
            dl.cancel()
            print("\n[!] 已取消, 断点已保存")
            return 130
        except OSError as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            else:
                print(f"[!] 下载出错: {exc}")
            return 1

        if stats.done:
            if args.json:
                print(json.dumps({"name": info.name, "path": path, "dest": dest, "size": info.size, "done": True}, ensure_ascii=False))
            else:
                print(f"[OK] 下载完成: {dest}")
            append_record(HistoryRecord(path=path, name=info.name, size=info.size, dest=dest, status="done", speed_bps=stats.speed_bps))
            return 0
        if args.json:
            print(json.dumps({"name": info.name, "path": path, "dest": dest, "size": info.size, "done": False, "error": stats.error or "未完成"}, ensure_ascii=False))
        else:
            print(f"[!] 下载失败: {stats.error or '未完成'}")
        append_record(HistoryRecord(path=path, name=info.name, size=info.size, dest=dest, status="failed", speed_bps=stats.speed_bps, error=stats.error or ""))
        return 1


if __name__ == "__main__":
    sys.exit(main())
