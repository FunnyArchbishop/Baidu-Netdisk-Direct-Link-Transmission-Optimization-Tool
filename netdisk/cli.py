"""交互式命令行界面 (REPL)。

命令:
    help                       显示帮助
    login <BDUSS>              设置/更新登录凭证 (也可粘贴整段 Cookie)
    status                     查看登录状态与配置
    ls [路径]                  列出目录内容
    cd <路径>                  切换当前目录
    pwd                        显示当前目录
    download <文件> [本地目录]  用直链+多线程下载文件
    mkdir <目录>               创建目录
    rm <路径> [...]            删除文件/目录
    mv <源> <目标目录>         移动文件/目录
    cp <源> <目标目录>         复制文件/目录
    rename <路径> <新名称>     重命名
    search <关键词>            按名称搜索文件
    history                    查看下载历史
    share <链接>               解析分享链接并列出文件
    set threads <N>            设置并发线程数
    set speed <速率>           设置下载限速 (如 512k / 1m, 0 不限速)
    set dir <本地目录>         设置下载保存目录
    exit / quit                退出
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Optional

# ── 强制终端输出为 UTF-8, 避免 Windows 下中文乱码 ──────────────────────
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .api import BaiduNetdiskAPI, NetdiskError, AuthError, human_size
from .credentials import CredentialError, build_baidu_credentials
from .config import AppConfig, load_config, save_config
from .downloader import DirectLinkDownloader
from .history import HistoryRecord, append_record, read_records
from .providers import create_provider, get_provider_names

BANNER = r"""
   ____        _ _     _   ____             _    _
  |  _ \  __ _| (_) __| | |  _ \  ___ _ __ | |_ (_)_ __
  | | | |/ _` | | |/ _` | | | | |/ _ \ '_ \| __|| | '_ \
  | |_| | (_| | | | (_| | | |_| |  __/ | | | |_ | | | | |
  |____/ \__,_|_|_|\__,_| |____/ \___|_| |_|\__||_|_| |_|
   网盘通用 · 直链传输优化工具
"""


class NetdiskCLI:
    """交互式命令解释器。"""

    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.api = None
        self.cwd = "/"
        self._build_api_if_possible()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _build_api_if_possible(self) -> None:
        # 仅当已配置凭证时才尝试构建 (避免启动时因缺凭证报错)
        provider = self.cfg.provider
        has_cred = {
            "baidu": bool(self.cfg.bduss),
            "webdav": bool(self.cfg.webdav_url),
        }.get(provider, False)
        if not has_cred:
            return
        try:
            self.api = create_provider(provider, self.cfg)
        except (AuthError, NetdiskError) as exc:
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
        eta_txt = self._fmt_eta(downloaded, total, speed)
        line = (
            f"\r[{bar}] {percent:5.1f}%  {human_size(downloaded)}/{human_size(total)}"
            f"  {speed_txt}  ETA {eta_txt}  {status}"
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        if status != "下载中...":
            sys.stdout.write("\n")
            sys.stdout.flush()

    @staticmethod
    def _fmt_eta(downloaded: int, total: int, speed: float) -> str:
        """根据剩余字节与速度计算剩余时间, 返回 `mm:ss` 或 `hh:mm:ss`。"""
        if speed <= 0 or downloaded >= total:
            return "--:--"
        remain = max(0, int((total - downloaded) / speed))
        h, rem = divmod(remain, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

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
        # 使用 shlex 解析, 支持带空格/引号的路径 (如 download "我的 文件.zip")
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"[!] 命令解析失败: {exc}")
            return True
        if not parts:
            return True
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
        elif cmd == "mkdir":
            self._cmd_mkdir(args)
        elif cmd in ("rm", "delete"):
            self._cmd_rm(args)
        elif cmd in ("mv", "move"):
            self._cmd_mv(args)
        elif cmd in ("cp", "copy"):
            self._cmd_cp(args)
        elif cmd in ("rename", "ren"):
            self._cmd_rename(args)
        elif cmd in ("search", "find"):
            self._cmd_search(args)
        elif cmd in ("history", "log"):
            self._cmd_history()
        elif cmd == "share":
            self._cmd_share(args)
        else:
            print(f"[!] 未知命令: {cmd} (输入 help 查看帮助)")
        return True

    # ------------------------------------------------------------------
    # 各命令实现
    # ------------------------------------------------------------------
    def _cmd_help(self) -> None:
        print(__doc__)

    def _cmd_login(self, args: list[str]) -> None:
        if self.cfg.provider == "webdav":
            # WebDAV 通过 set 命令配置账号, 这里仅校验
            try:
                api = create_provider("webdav", self.cfg)
                if api.check_login():
                    self.api = api
                    print("[OK] WebDAV 连接成功")
                else:
                    print("[!] WebDAV 连接失败, 请检查地址/用户名/密码 (set webdav_url/username/password)")
            except (AuthError, NetdiskError) as exc:
                print(f"[!] 登录失败: {exc}")
            return
        if not args:
            print("用法: login <BDUSS>")
            print("       login BDUSS=xxx; STOKEN=yyy; BAIDUID=zzz   (推荐, 整段 Cookie)")
            print("提示: 浏览器登录 https://pan.baidu.com 后, 按 F12 → 网络 → 任意请求 → 请求标头 Cookie 整行复制。")
            return
        raw = " ".join(args).strip()
        try:
            creds = build_baidu_credentials(raw)
        except CredentialError as exc:
            print(f"[!] {exc}")
            return
        self.cfg.bduss = creds.bduss
        self.cfg.baidu_cookies = creds.cookies
        try:
            api = create_provider("baidu", self.cfg)
            if api.check_login():
                save_config(self.cfg)
                self.api = api
                print(f"[OK] 登录成功, 已保存 {len(creds.cookies)} 个 Cookie 到 config.json")
                for note in creds.notes:
                    print(f"[i] {note}")
            else:
                detail = getattr(api, "last_error", "") or "BDUSS 可能无效或已过期"
                print(f"[!] 登录校验失败: {detail}")
        except AuthError as exc:
            print(f"[!] 登录失败: {exc}")

    def _cmd_status(self) -> None:
        cfg = self.cfg
        print(f"网盘类型 : {cfg.provider}")
        print(f"登录状态 : {'已连接' if self.api else '未登录 (使用 login 或 set 配置凭证)'}")
        print(f"下载目录 : {cfg.download_dir}")
        print(f"并发线程 : {cfg.threads}")
        print(f"自动重试 : {'开启' if cfg.auto_retry else '关闭'}")
        print(f"下载限速 : {human_size(cfg.speed_limit) + '/s' if cfg.speed_limit else '不限速'}")
        if self.api:
            print(f"当前目录 : {self.cwd}")

    def _require_api(self):
        if self.api is None:
            raise AuthError("尚未登录, 请先配置凭证并 login (百度用 login <BDUSS>, WebDAV 用 set webdav_url/username/password)")
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
            print("用法: download <文件或目录路径> [本地保存目录]")
            return
        path = self._resolve(args[0])
        local_dir = self.cfg.ensure_download_dir()
        if len(args) > 1:
            local_dir = args[1]
            os.makedirs(local_dir, exist_ok=True)

        info = api.get_file_info(path)
        if info is None:
            print(f"[!] 未找到路径: {path}")
            return

        if info.is_dir:
            files = api.walk(path)
            if not files:
                print("(目录为空)")
                return
            print(f"目录下载: 共 {len(files)} 个文件")
            ok = 0
            try:
                for f in files:
                    rel = f.path
                    if rel.startswith(path):
                        rel = rel[len(path):].lstrip("/")
                    sub_dir = os.path.join(local_dir, os.path.dirname(rel))
                    os.makedirs(sub_dir, exist_ok=True)
                    if self._download_file(f, sub_dir):
                        ok += 1
            except KeyboardInterrupt:
                print("\n[!] 已取消目录下载")
            else:
                print(f"[OK] 目录下载完成: {ok}/{len(files)} 成功")
            return

        try:
            self._download_file(info, local_dir)
        except KeyboardInterrupt:
            print("\n[!] 已取消, 断点状态已保存, 可续传")

    def _download_file(self, info, local_dir: str) -> bool:
        """下载单个文件, 返回是否成功。"""
        dest = os.path.join(local_dir, info.name)
        if os.path.exists(dest) and not os.path.exists(dest + ".download.json"):
            print(f"[!] 文件已存在, 跳过: {dest}")
            return False
        try:
            url = self.api.get_direct_link(info.path, info.fs_id)
        except NetdiskError as exc:
            print(f"[!] 获取直链失败 ({info.name}): {exc}")
            self._record(info, dest, "failed", error=str(exc))
            return False

        dl = DirectLinkDownloader(
            url=url,
            dest_path=dest,
            threads=self.cfg.threads,
            speed_limit=self.cfg.speed_limit,
            proxy=self.cfg.proxy,
            headers=self.api.download_headers(info.path),
        )
        try:
            stats = dl.download(on_progress=self._on_progress, expected_md5=info.md5)
        except KeyboardInterrupt:
            dl.cancel()
            self._record(info, dest, "cancelled")
            raise
        if stats.done:
            print(f"[OK] 下载完成: {dest}")
            self._record(info, dest, "done", speed=stats.speed_bps)
            return True
        print(f"[!] 下载失败 ({info.name}): {stats.error or '未完成'}")
        self._record(info, dest, "failed", speed=stats.speed_bps, error=stats.error or "")
        return False

    def _record(self, info, dest: str, status: str, speed: float = 0.0, error: str = "") -> None:
        """追加一条下载历史。"""
        append_record(
            HistoryRecord(
                path=info.path,
                name=info.name,
                size=info.size,
                dest=dest,
                status=status,
                speed_bps=speed,
                error=error,
            )
        )

    def _cmd_history(self) -> None:
        records = read_records()
        if not records:
            print("(暂无下载历史)")
            return
        for r in records:
            mark = {"done": "[OK]", "failed": "[!]", "cancelled": "[~]"}.get(r.get("status"), "[?]")
            print(f"{mark} {r.get('time', '')}  {r.get('name', '')}  ({human_size(r.get('size', 0))}) -> {r.get('dest', '')}")
            if r.get("error"):
                print(f"       错误: {r['error']}")
        print(f"\n最近 {len(records)} 条记录")

    def _cmd_share(self, args: list[str]) -> None:
        api = self._require_api()
        if not args:
            print("用法: share <分享链接>")
            return
        try:
            files = api.list_share_files(args[0])
        except NetdiskError as exc:
            print(f"[!] {exc}")
            return
        if not files:
            print("(空分享)")
            return
        for f in files:
            mark = "[DIR]" if f.is_dir else "     "
            print(f"{mark}  {f.name}  {f.size_human}")
        print(f"\n共 {len(files)} 项")

    def _cmd_set(self, args: list[str]) -> None:
        if len(args) < 2:
            print("用法: set threads <N> | speed <速率> | dir <目录> | proxy <URL> | provider <baidu|webdav> | webdav_url <URL> | username <用户名> | password <密码>")
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
        elif key == "speed":
            try:
                self.cfg.speed_limit = self._parse_speed(value)
            except ValueError:
                print("[!] 限速值无效 (支持字节数或 512k / 1m 后缀, 0 为不限速)")
                return
        elif key == "dir":
            self.cfg.download_dir = value
        elif key == "proxy":
            self.cfg.proxy = value if value.lower() not in ("none", "off") else ""
        elif key == "provider":
            if value.lower() not in get_provider_names():
                print(f"[!] 未知网盘: {value} (可用: {', '.join(get_provider_names())})")
                return
            self.cfg.provider = value.lower()
            self.api = None  # 切换后重建
        elif key == "webdav_url":
            self.cfg.webdav_url = value
        elif key == "username":
            self.cfg.username = value
        elif key == "password":
            self.cfg.password = value
        else:
            # 通用兜底: 允许直接设置任意字符串配置字段 (如 aliyun_refresh_token / quark_cookie / iflytek_url 等)
            if key not in self.cfg.__dataclass_fields__:
                print(f"[!] 未知配置项: {key}")
                return
            setattr(self.cfg, key, value)
        save_config(self.cfg)
        print(f"[OK] 已更新 {key} = {value}")

    @staticmethod
    def _parse_speed(value: str) -> int:
        """解析限速值, 支持纯字节数或 K/M/G 后缀, 返回字节/秒。"""
        v = value.strip().lower()
        if v in ("", "0", "unlimited", "none", "off"):
            return 0
        mult = 1
        if v.endswith("k"):
            mult, v = 1024, v[:-1]
        elif v.endswith("m"):
            mult, v = 1024 * 1024, v[:-1]
        elif v.endswith("g"):
            mult, v = 1024 ** 3, v[:-1]
        n = float(v)
        if n < 0:
            raise ValueError
        return int(n * mult)

    # ------------------------------------------------------------------
    # 文件管理命令
    # ------------------------------------------------------------------
    def _cmd_mkdir(self, args: list[str]) -> None:
        api = self._require_api()
        if not args:
            print("用法: mkdir <目录名>")
            return
        api.mkdir(self._resolve(args[0]))
        print(f"[OK] 目录已创建: {self._resolve(args[0])}")

    def _cmd_rm(self, args: list[str]) -> None:
        api = self._require_api()
        if not args:
            print("用法: rm <路径> [更多路径...]")
            return
        targets = [self._resolve(a) for a in args]
        api.delete(*targets)
        print(f"[OK] 已删除 {len(targets)} 项")

    def _cmd_mv(self, args: list[str]) -> None:
        api = self._require_api()
        if len(args) < 2:
            print("用法: mv <源路径> <目标目录>")
            return
        api.move(self._resolve(args[0]), self._resolve(args[1]))
        print("[OK] 移动完成")

    def _cmd_cp(self, args: list[str]) -> None:
        api = self._require_api()
        if len(args) < 2:
            print("用法: cp <源路径> <目标目录>")
            return
        api.copy(self._resolve(args[0]), self._resolve(args[1]))
        print("[OK] 复制完成")

    def _cmd_rename(self, args: list[str]) -> None:
        api = self._require_api()
        if len(args) < 2:
            print("用法: rename <路径> <新名称>")
            return
        api.rename(self._resolve(args[0]), args[1])
        print("[OK] 重命名完成")

    def _cmd_search(self, args: list[str]) -> None:
        api = self._require_api()
        if not args:
            print("用法: search <关键词>")
            return
        files = api.search(args[0], self.cwd)
        if not files:
            print("(无结果)")
            return
        for f in files:
            mark = "[DIR]" if f.is_dir else "     "
            print(f"{mark}  {f.path}")
        print(f"\n共 {len(files)} 项")

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
