"""交互式命令行界面 (REPL)。

命令:
    help                       显示帮助
    login <BDUSS>              设置/更新登录凭证
    status                     查看登录状态与配置
    ls [路径]                  列出目录内容
    cd <路径>                  切换当前目录
    pwd                        显示当前目录
    download <文件> [本地目录]  用直链+多线程下载文件
    set threads <N>            设置并发线程数
    set dir <本地目录>          设置下载保存目录
    exit / quit                退出
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

# ── 强制终端输出为 UTF-8, 避免 Windows 下中文乱码 ──────────────────────
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .api import BaiduNetdiskAPI, NetdiskError, AuthError, human_size
from .config import AppConfig, load_config, save_config
from .downloader import DirectLinkDownloader

BANNER = r"""
   ____        _ _     _   ____             _    _
  |  _ \  __ _| (_) __| | |  _ \  ___ _ __ | |_ (_)_ __
  | | | |/ _` | | |/ _` | | | | |/ _ \ '_ \| __|| | '_ \
  | |_| | (_| | | | (_| | | |_| |  __/ | | | |_ | | | | |
  |____/ \__,_|_|_|\__,_| |____/ \___|_| |_|\__||_|_| |_|
   百度网盘 · 直链传输优化工具
"""


class NetdiskCLI:
    """交互式命令解释器。"""

    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.api: Optional[BaiduNetdiskAPI] = None
        self.cwd = "/"
        self._build_api_if_possible()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _build_api_if_possible(self) -> None:
        if self.cfg.bduss:
            try:
                self.api = BaiduNetdiskAPI(self.cfg.bduss)
            except AuthError as exc:
                print(f"[!] 初始化失败: {exc}")

    # ------------------------------------------------------------------
    # 进度显示
    # ------------------------------------------------------------------
    def _on_progress(self, downloaded: int, total: int, speed: float, status: str) -> None:
        if total <= 0:
            return
        percent = downloaded / total * 100
        width = 30
        filled = int(percent / 100 * width)
        bar = "#" * filled + "-" * (width - filled)
        speed_txt = human_size(speed) + "/s" if speed > 0 else "--"
        line = (
            f"\r[{bar}] {percent:5.1f}%  {human_size(downloaded)}/{human_size(total)}"
            f"  {speed_txt}  {status}"
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        if status in ("完成",) or downloaded >= total:
            sys.stdout.write("\n")
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------
    def run(self) -> None:
        """进入 REPL 主循环。"""
        print(BANNER)
        print("输入 help 查看可用命令, exit 退出。\n")
        while True:
            try:
                line = input(f"pan://{self.cwd} > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见!")
                break
            if not line:
                continue
            try:
                if not self._dispatch(line):
                    break
            except (AuthError, NetdiskError) as exc:
                print(f"[!] {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"[!] 发生错误: {exc}")

    def _dispatch(self, line: str) -> bool:
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("exit", "quit", "q"):
            return False
        if cmd in ("help", "h", "?"):
            self._cmd_help()
        elif cmd == "login":
            self._cmd_login(args)
        elif cmd in ("status", "info"):
            self._cmd_status()
        elif cmd in ("ls", "list"):
            self._cmd_ls(args)
        elif cmd in ("cd", "open"):
            self._cmd_cd(args)
        elif cmd in ("pwd",):
            print(self.cwd)
        elif cmd == "download":
            self._cmd_download(args)
        elif cmd == "set":
            self._cmd_set(args)
        else:
            print(f"[!] 未知命令: {cmd} (输入 help 查看帮助)")
        return True

    # ------------------------------------------------------------------
    # 各命令实现
    # ------------------------------------------------------------------
    def _cmd_help(self) -> None:
        print(__doc__)

    def _cmd_login(self, args: list[str]) -> None:
        if not args:
            print("用法: login <BDUSS>")
            print("提示: 登录网页版百度网盘后, 从浏览器 Cookie 中复制 BDUSS 值。")
            return
        bduss = args[0]
        try:
            api = BaiduNetdiskAPI(bduss)
            if api.check_login():
                self.cfg.bduss = bduss
                save_config(self.cfg)
                self.api = api
                print("[OK] 登录成功, 凭证已保存到 config.json")
            else:
                print("[!] 登录校验失败, BDUSS 可能无效或已过期")
        except AuthError as exc:
            print(f"[!] 登录失败: {exc}")

    def _cmd_status(self) -> None:
        cfg = self.cfg
        print(f"登录状态 : {'已登录' if self.api else '未登录 (使用 login <BDUSS> 登录)'}")
        print(f"下载目录 : {cfg.download_dir}")
        print(f"并发线程 : {cfg.threads}")
        print(f"自动重试 : {'开启' if cfg.auto_retry else '关闭'}")
        if self.api:
            print(f"当前目录 : {self.cwd}")

    def _require_api(self) -> BaiduNetdiskAPI:
        if self.api is None:
            raise AuthError("尚未登录, 请先执行: login <BDUSS>")
        return self.api

    def _cmd_ls(self, args: list[str]) -> None:
        api = self._require_api()
        target = self._resolve(args[0] if args else self.cwd)
        files = api.list_files(target)
        if not files:
            print("(空目录)")
            return
        dirs = [f for f in files if f.is_dir]
        plain = [f for f in files if not f.is_dir]
        for d in dirs:
            print(f"[DIR]  {d.name}/")
        for f in plain:
            print(f"       {f.name:<40} {f.size_human:>10}")
        print(f"\n共 {len(files)} 项 ({len(dirs)} 个目录, {len(plain)} 个文件)")

    def _cmd_cd(self, args: list[str]) -> None:
        api = self._require_api()
        if not args or args[0] in (".", ".."):
            if args and args[0] == "..":
                if self.cwd != "/":
                    self.cwd = self.cwd.rsplit("/", 1)[0] or "/"
            return
        target = self._resolve(args[0])
        if not target.endswith("/"):
            target += "/"
        # 校验目录存在
        parent = target.rstrip("/").rsplit("/", 1)[0] or "/"
        name = target.rstrip("/").rsplit("/", 1)[-1]
        found = False
        for f in api.list_files(parent):
            if f.is_dir and f.name == name:
                found = True
                break
        if not found:
            print(f"[!] 目录不存在: {args[0]}")
            return
        self.cwd = target

    def _cmd_download(self, args: list[str]) -> None:
        api = self._require_api()
        if not args:
            print("用法: download <文件路径> [本地保存目录]")
            return
        path = self._resolve(args[0])
        if path.endswith("/"):
            print("[!] 请指定文件, 目录下载暂不支持")
            return
        info = api.get_file_info(path)
        if info is None:
            print(f"[!] 未找到文件: {path}")
            return
        if info.is_dir:
            print("[!] 目标为目录, 请指定具体文件")
            return

        local_dir = self.cfg.ensure_download_dir()
        if len(args) > 1:
            local_dir = args[1]
            os.makedirs(local_dir, exist_ok=True)

        dest = os.path.join(local_dir, info.name)
        if os.path.exists(dest) and not os.path.exists(dest + ".download.json"):
            print(f"[!] 文件已存在, 跳过: {dest}")
            return

        print(f"获取直链: {info.name} ({info.size_human}) ...")
        try:
            url = api.get_direct_link(path, info.fs_id)
        except NetdiskError as exc:
            print(f"[!] 获取直链失败: {exc}")
            return
        print(f"直链已获取, 开始多线程下载 ({self.cfg.threads} 线程)...")

        dl = DirectLinkDownloader(
            url=url,
            dest_path=dest,
            threads=self.cfg.threads,
        )
        try:
            stats = dl.download(on_progress=self._on_progress)
            if stats.done:
                print(f"[OK] 下载完成: {dest}")
            else:
                print(f"[!] 下载未完成, 已保留断点 {dest + '.part'}, 可重新执行 download 续传")
        except KeyboardInterrupt:
            dl.cancel()
            print("\n[!] 已取消, 断点状态已保存, 可续传")

    def _cmd_set(self, args: list[str]) -> None:
        if len(args) < 2:
            print("用法: set threads <N> | set dir <本地目录>")
            return
        key, value = args[0].lower(), args[1]
        if key == "threads":
            try:
                n = int(value)
                if not 1 <= n <= 32:
                    raise ValueError
                self.cfg.threads = n
            except ValueError:
                print("[!] 线程数需为 1-32 的整数")
                return
        elif key == "dir":
            self.cfg.download_dir = value
        else:
            print(f"[!] 未知配置项: {key}")
            return
        save_config(self.cfg)
        print(f"[OK] 已更新 {key} = {value}")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _resolve(self, path: str) -> str:
        """把相对路径解析为绝对网盘路径。"""
        if path.startswith("/"):
            return path
        if path == "..":
            return self.cwd.rsplit("/", 1)[0] if self.cwd != "/" else "/"
        if self.cwd == "/":
            return "/" + path
        return self.cwd.rstrip("/") + "/" + path


def run_cli() -> None:
    NetdiskCLI().run()
