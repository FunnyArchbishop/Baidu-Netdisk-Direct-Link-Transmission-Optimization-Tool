"""Interactive official login in an isolated browser; never collect passwords."""
from __future__ import annotations

import threading
import time
import sys


class LoginValidationError(Exception):
    """A safe diagnostic containing no credentials or response bodies."""


def login_dependency_error(exc):
    """Report the interpreter actually hosting the UI, not the shell default."""
    missing = getattr(exc, "name", None) or "playwright / greenlet"
    if getattr(sys, "frozen", False):
        return (f"当前 EXE 未包含可用的登录组件（{missing}）。给系统 Python 安装依赖不会更新 EXE。"
                "请使用项目中的 start-web.cmd 启动源码版，或重新打包 EXE。")
    return (f"当前程序使用的 Python：{sys.executable}；无法加载组件：{missing}。"
            "请关闭旧服务，使用项目中的 start-web.cmd 启动专用环境。"
            f'手动修复时，请对同一个解释器运行：& "{sys.executable}" -m pip install -r requirements.txt。'
            "MSYS2 / MinGW 环境请改用启动脚本，安装后需重启服务。")


def open_login_panel(page):
    """Use public controls only; leave account input and challenges to the user."""
    try:
        page.get_by_text("去登录", exact=True).click(timeout=12000)
    except Exception:
        # The official site may already show login, or change its landing page.
        return False
    try:
        page.get_by_text("账号登录", exact=True).click(timeout=8000)
    except Exception:
        pass  # QR/SMS login remains usable if the password tab is unavailable.
    return True


class BrowserLogin:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.thread = None
        self.state = {"status": "idle", "message": "尚未开始登录"}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, status, message):
        with self.lock:
            if self.cancelled.is_set() and status != "cancelled":
                return
            self.state = {"status": status, "message": message}

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.state["status"] in ("starting", "waiting", "verifying"):
                self._set("cancelled", "已取消登录")

    def start(self, validate, commit, proxy=""):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.snapshot()
            self.cancelled = threading.Event()
            self._set("starting", "正在打开百度官方登录窗口…")
            self.thread = threading.Thread(
                target=self._run, args=(validate, commit, proxy), daemon=True
            )
            self.thread.start()
            return self.snapshot()

    def _run(self, validate, commit, proxy):
        try:
            from playwright.sync_api import sync_playwright
        except (ImportError, OSError) as exc:
            self._set("error", login_dependency_error(exc))
            return
        try:
            with sync_playwright() as pw:
                browser = None
                options = {"headless": False}
                if proxy:
                    options["proxy"] = {"server": proxy}
                for channel in ("msedge", "chrome", None):
                    if self.cancelled.is_set():
                        return
                    try:
                        browser = pw.chromium.launch(**options, **({"channel": channel} if channel else {}))
                        break
                    except Exception:
                        continue
                if browser is None:
                    self._set("error", "无法启动浏览器。请安装 Edge / Chrome，或运行 python -m playwright install chromium")
                    return
                try:
                    context = browser.new_context(locale="zh-CN")
                    page = context.new_page()
                    page.goto("https://pan.baidu.com/", wait_until="domcontentloaded", timeout=60000)
                    if self.cancelled.is_set():
                        return
                    opened = open_login_panel(page)
                    page.bring_to_front()
                    message = "请在官方窗口完成登录，可选择密码、短信或扫码；如需验证码，请按官方提示操作。"
                    if not opened:
                        message = "官方窗口已打开。如显示产品首页，请先点击「去登录」，再选择「账号登录」、短信或扫码。"
                    self._set("waiting", message)
                    deadline = time.monotonic() + 600
                    next_check = 0
                    while not self.cancelled.is_set() and time.monotonic() < deadline:
                        if not context.pages:
                            self._set("cancelled", "登录窗口已关闭，可重新打开")
                            return
                        cookies = context.cookies("https://pan.baidu.com/")
                        bduss = next((c["value"] for c in cookies if c["name"] == "BDUSS"), "")
                        if bduss and time.monotonic() >= next_check:
                            self._set("verifying", "正在验证网盘登录状态…")
                            failure = "网盘尚未接受导入的登录态"
                            try:
                                result = validate({"bduss": bduss, "cookies": cookies, "user_agent": page.evaluate("navigator.userAgent")})
                            except Exception as exc:
                                result = None
                                failure = str(exc) if isinstance(exc, LoginValidationError) else "网盘校验请求失败，请检查网络或代理"
                            if result is not None:
                                with self.lock:
                                    if self.cancelled.is_set():
                                        return
                                    try:
                                        commit(bduss, result)
                                    except OSError:
                                        self._set("error", "登录验证成功，但配置保存失败，请检查配置目录的写入权限后重试")
                                        return
                                    self._set("success", "登录成功，已连接百度网盘")
                                return
                            next_check = time.monotonic() + 5
                            self._set("waiting", "登录态导入未通过：" + failure)
                        self.cancelled.wait(1)
                    if not self.cancelled.is_set():
                        self._set("expired", "登录已超时，请重新打开登录窗口")
                finally:
                    browser.close()
        except Exception:
            if not self.cancelled.is_set():
                self._set("error", "登录窗口已关闭或官方页面加载失败，请检查网络后重试")
