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
import os
import sys

# ── 强制终端输出为 UTF-8, 避免 Windows 下中文乱码 ──────────────────────
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from netdisk import __version__
from netdisk.api import BaiduNetdiskAPI, NetdiskError, AuthError, human_size
from netdisk.cli import NetdiskCLI
from netdisk.config import load_config, save_config
from netdisk.downloader import DirectLinkDownloader


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config()

    # 更新配置
    if args.login:
        cfg.bduss = args.login
        save_config(cfg)
        print("[OK] BDUSS 已保存")
        return 0
    if args.threads:
        if not 1 <= args.threads <= 32:
            print("[!] 线程数需为 1-32")
            return 1
        cfg.threads = args.threads
        save_config(cfg)
        print(f"[OK] 并发线程数已设为 {args.threads}")
        return 0

    # 无参数时进入交互模式
    if not args.list and not args.download:
        NetdiskCLI(cfg).run()
        return 0

    # 一次性模式
    if not cfg.bduss:
        print("[!] 尚未登录, 请先运行: python main.py --login <BDUSS>")
        return 1

    api = BaiduNetdiskAPI(cfg.bduss)
    if not api.check_login():
        print("[!] BDUSS 无效或已过期, 请重新登录")
        return 1

    if args.list:
        try:
            files = api.list_files(args.list)
            for f in files:
                mark = "[DIR]" if f.is_dir else "     "
                print(f"{mark}  {f.name:<40} {f.size_human:>10}")
            print(f"\n共 {len(files)} 项")
        except (AuthError, NetdiskError) as exc:
            print(f"[!] {exc}")
            return 1
        return 0

    if args.download:
        path = args.download
        try:
            info = api.get_file_info(path)
            if info is None:
                print(f"[!] 未找到文件: {path}")
                return 1
            if info.is_dir:
                print("[!] 目标为目录, 请指定具体文件")
                return 1
            url = api.get_direct_link(path, info.fs_id)
        except (AuthError, NetdiskError) as exc:
            print(f"[!] 获取直链失败: {exc}")
            return 1

        local_dir = args.output or cfg.ensure_download_dir()
        if args.output:
            os.makedirs(local_dir, exist_ok=True)
        dest = os.path.join(local_dir, info.name)
        print(f"开始下载 {info.name} ({info.size_human}) -> {dest}")
        print(f"线程数: {cfg.threads}")

        cli = NetdiskCLI(cfg)
        dl = DirectLinkDownloader(url=url, dest_path=dest, threads=cfg.threads)
        try:
            stats = dl.download(on_progress=cli._on_progress)
            if stats.done:
                print(f"[OK] 下载完成: {dest}")
                return 0
            print(f"[!] 未完成, 断点保留在 {dest}.part, 可再次执行续传")
            return 1
        except KeyboardInterrupt:
            dl.cancel()
            print("\n[!] 已取消, 断点已保存")
            return 130


if __name__ == "__main__":
    sys.exit(main())
