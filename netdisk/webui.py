"""Web UI: 基于 Flask 的百度网盘管理界面。

提供登录、文件浏览/搜索、文件管理、多线程下载(带进度)、分享链接浏览/下载、
下载队列并发控制、下载历史与暗色/亮色主题切换等能力。
启动: python main.py --web [--port 5000]
"""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from .browser_login import BrowserLogin, LoginValidationError
from .api import AuthError, BaiduNetdiskAPI, NetdiskError
from .config import AppConfig, load_config, save_config
from .credentials import CredentialError, build_baidu_credentials
from .downloader import DirectLinkDownloader
from .history import HistoryRecord, append_record, read_records
from .providers import create_provider, get_provider_meta, get_provider_names

app = Flask(__name__)

_cfg: AppConfig = load_config()
_api: "BaiduNetdiskAPI | None" = None
_state_lock = threading.Lock()
_browser_login = BrowserLogin()
# 最近一次登录/鉴权失败原因, 让界面能把百度返回的 errno 原样展示出来
_LAST_AUTH_ERROR = ""

# 下载任务注册表: {task_id: {...}}
_TASKS: dict = {}
_TASKS_LOCK = threading.Lock()
_TASK_SEQ = 0

# 并发下载槽位控制 (动态读取 _cfg.max_concurrent)
_ACTIVE = 0
_ACTIVE_COND = threading.Condition()


def _acquire_slot() -> None:
    global _ACTIVE
    with _ACTIVE_COND:
        while _ACTIVE >= max(1, _cfg.max_concurrent):
            _ACTIVE_COND.wait()
        _ACTIVE += 1


def _release_slot() -> None:
    global _ACTIVE
    with _ACTIVE_COND:
        _ACTIVE -= 1
        _ACTIVE_COND.notify_all()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _get_api() -> "BaiduNetdiskAPI | None":
    global _api, _LAST_AUTH_ERROR
    with _state_lock:
        if _api is None:
            try:
                _api = create_provider(_cfg.provider, _cfg)
            except (AuthError, NetdiskError) as exc:
                _api = None
                _LAST_AUTH_ERROR = str(exc)
        return _api


def _require_api():
    api = _get_api()
    if api is None:
        raise AuthError("尚未登录, 请先配置网盘凭证")
    return api


def _ok(data=None, **extra):
    d = {"ok": True}
    if data is not None:
        d["data"] = data
    d.update(extra)
    return jsonify(d)


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _auth_err(exc) -> tuple:
    """认证类失败: 记下原因并原样回传, 供界面顶部提示。"""
    global _LAST_AUTH_ERROR
    message = str(exc)
    _LAST_AUTH_ERROR = message
    return _err(message)


def _public(task: dict) -> dict:
    """序列化任务时排除内部字段 (以 _ 开头)。"""
    return {k: v for k, v in task.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return INDEX_HTML


# ---------------------------------------------------------------------------
# 登录 / 状态 / 配置
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    last_error = _LAST_AUTH_ERROR
    if _cfg.provider == "baidu" and not _cfg.bduss:
        last_error = ""  # 还从没填过凭证, 不当作"上次登录失败"
    return _ok(
        {
            "logged": _get_api() is not None,
            "provider": _cfg.provider,
            "providers": get_provider_names(),
            "threads": _cfg.threads,
            "speed_limit": _cfg.speed_limit,
            "download_dir": _cfg.download_dir,
            "proxy": _cfg.proxy,
            "max_concurrent": _cfg.max_concurrent,
            # 只暴露 Cookie 名字, 不回传任何值
            "cookie_names": [c.get("name") for c in _cfg.baidu_cookies if isinstance(c, dict)],
            "last_error": last_error,
        }
    )


@app.get("/api/providers")
def api_providers():
    """返回网盘提供者列表及其登录字段 (供前端动态渲染)。"""
    return _ok(get_provider_meta())


def _validate_browser_login(credentials):
    candidate = replace(_cfg, provider="baidu", bduss=credentials["bduss"],
                        baidu_cookies=credentials["cookies"],
                        baidu_user_agent=credentials["user_agent"]).sanitize()
    api = create_provider("baidu", candidate)
    try:
        api._ensure_auth_info()
    except (AuthError, NetdiskError) as exc:
        raise LoginValidationError(str(exc)) from exc
    if not api._bdstoken or not api._uk:
        raise LoginValidationError("百度未返回有效的账户信息")
    api._browser_credentials = candidate
    return api


def _commit_browser_login(bduss, api):
    global _cfg, _api
    with _state_lock:
        credentials = api._browser_credentials
        candidate = replace(_cfg, provider="baidu", bduss=bduss,
                            baidu_cookies=credentials.baidu_cookies,
                            baidu_user_agent=credentials.baidu_user_agent)
        save_config(candidate)
        _cfg, _api = candidate, api


@app.before_request
def protect_local_login():
    if request.path.startswith("/api/login/baidu"):
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return _err("官方登录需要在运行程序的本机操作", 403)
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return _err("不允许跨站登录请求", 403)
        if urlsplit(request.host_url).hostname not in ("127.0.0.1", "localhost", "::1"):
            return _err("请使用 localhost 或 127.0.0.1 打开界面", 403)
        if request.method == "POST" and not request.is_json:
            return _err("登录请求需要 JSON", 415)


@app.post("/api/login/baidu/start")
def start_baidu_login():
    return _ok(_browser_login.start(_validate_browser_login, _commit_browser_login, _cfg.proxy))


@app.get("/api/login/baidu/status")
def baidu_login_status():
    return _ok(_browser_login.snapshot())


@app.post("/api/login/baidu/cancel")
def cancel_baidu_login():
    _browser_login.cancel()
    return _ok(_browser_login.snapshot())


@app.post("/api/login/baidu/cookie")
def login_baidu_cookie():
    """用浏览器复制的整段 Cookie 登录。

    百度对部分账号不再只认 BDUSS 一个值, 直接把整段 Cookie 灌进会话最稳。
    验证通过才写 config.json, 失败时把百度返回的 errno 原样告诉用户。
    """
    global _cfg, _api, _LAST_AUTH_ERROR
    payload = request.get_json(silent=True) or {}
    try:
        creds = build_baidu_credentials(str(payload.get("cookie") or ""))
    except CredentialError as exc:
        _LAST_AUTH_ERROR = str(exc)
        return _err(str(exc))

    base = replace(
        _cfg,
        provider="baidu",
        baidu_user_agent=str(payload.get("user_agent") or "").strip(),
    )
    # 先用 BDUSS; 若同时存在 BDUSS_BFESS 且首轮失败, 再试一次
    attempts = [creds.bduss]
    if creds.alt_bduss and creds.alt_bduss != creds.bduss:
        attempts.append(creds.alt_bduss)

    last_api = None
    for bduss in attempts:
        candidate = replace(
            base,
            bduss=bduss,
            baidu_cookies=[dict(c) for c in creds.cookies],
        ).sanitize()
        api = create_provider("baidu", candidate)
        last_api = api
        if not api.check_login():
            continue
        try:
            save_config(candidate)
        except OSError as exc:
            return _err(f"校验通过但配置保存失败: {exc}", 500)
        with _state_lock:
            _cfg, _api = candidate, api
        _LAST_AUTH_ERROR = ""
        return _ok(
            {
                "cookies": creds.cookie_names,
                "notes": creds.notes,
                "uk": getattr(api, "_uk", 0),
            }
        )

    detail = str(getattr(last_api, "last_error", "") or "百度没有接受这份 Cookie")
    message = (
        f"登录校验失败: {detail}"
        f"(本次解析到 {len(creds.cookie_names)} 个 Cookie: {', '.join(creds.cookie_names)})"
    )
    _LAST_AUTH_ERROR = message
    return _err(message)


@app.post("/api/login")
def api_login():
    global _api, _cfg, _LAST_AUTH_ERROR
    _browser_login.cancel()
    try:
        payload = request.get_json(silent=True) or {}
        provider = (payload.get("provider") or _cfg.provider).strip()
        if provider not in get_provider_names():
            return _err(f"未知网盘类型: {provider}")

        candidate = replace(_cfg, provider=provider)
        if provider == "baidu":
            candidate.baidu_cookies = []
            candidate.baidu_user_agent = ""
        # 按提供者元信息通用写入凭证字段
        meta = next((m for m in get_provider_meta() if m["name"] == provider), None)
        if meta:
            for f in meta["fields"]:
                key = f["key"]
                if f["type"] == "password":
                    setattr(candidate, key, payload.get(key) or "")
                else:
                    setattr(candidate, key, (payload.get(key) or "").strip())

        api = create_provider(provider, candidate)
        if not api.check_login():
            detail = getattr(api, "last_error", "")
            detail = detail if isinstance(detail, str) else ""
            if detail:
                message = f"登录校验失败: {detail}"
            elif provider == "baidu":
                message = "登录校验失败: 百度没接受这串 BDUSS。"
            else:
                message = "登录校验失败, 请检查凭证"
            # 百度总是给出下一步: 单个 BDUSS 不够时改用整段 Cookie
            if provider == "baidu" and "粘贴完整 Cookie" not in message:
                message += (
                    "请改用「粘贴完整 Cookie」登录(设置 → 网盘设置 → 粘贴完整 Cookie),"
                    "单个 BDUSS 在部分账号上不够用。"
                )
            _LAST_AUTH_ERROR = message
            return _err(message)
        with _state_lock:
            save_config(candidate)
            _cfg, _api = candidate, api
            _LAST_AUTH_ERROR = ""
        return _ok()
    except AuthError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"登录出错: {exc}", 500)


@app.post("/api/logout")
def api_logout():
    global _api, _cfg
    _browser_login.cancel()
    if _cfg.provider == "baidu":
        _cfg.baidu_cookies = []
        _cfg.baidu_user_agent = ""
    meta = next((m for m in get_provider_meta() if m["name"] == _cfg.provider), None)
    if meta:
        for f in meta["fields"]:
            if f["type"] == "password":
                setattr(_cfg, f["key"], "")
    save_config(_cfg)
    _api = None
    return _ok()


@app.post("/api/config")
def api_config():
    global _api
    _browser_login.cancel()
    try:
        payload = request.get_json(silent=True) or {}
        if "threads" in payload:
            _cfg.threads = max(1, min(int(payload["threads"]), 32))
        if "speed_limit" in payload:
            _cfg.speed_limit = max(0, int(payload["speed_limit"]))
        if "max_concurrent" in payload:
            _cfg.max_concurrent = max(1, min(int(payload["max_concurrent"]), 8))
        if "provider" in payload and payload["provider"] in get_provider_names():
            _cfg.provider = payload["provider"]
        # 字符串字段通用处理 (下载目录 / 代理 / 各网盘凭证)
        for key in (
            "download_dir", "proxy", "webdav_url", "username", "password",
            "aliyun_refresh_token", "aliyun_drive_id", "quark_cookie",
            "iflytek_url", "iflytek_token",
        ):
            if key in payload:
                setattr(_cfg, key, str(payload[key]).strip())
        save_config(_cfg)
        _api = None  # 代理/网盘变化需重建 session
        return _ok()
    except (TypeError, ValueError) as exc:
        return _err(f"参数错误: {exc}")


# ---------------------------------------------------------------------------
# 文件浏览 / 搜索 / 管理
# ---------------------------------------------------------------------------
@app.get("/api/list")
def api_list():
    try:
        api = _require_api()
        path = request.args.get("path", "/") or "/"
        files = api.list_files(path)
        return _ok([f.to_dict() for f in files])
    except (AuthError, NetdiskError) as exc:
        return _auth_err(exc)
    except Exception as exc:  # noqa: BLE001
        return _err(f"列出目录出错: {exc}", 500)


@app.get("/api/search")
def api_search():
    try:
        api = _require_api()
        q = (request.args.get("q") or "").strip()
        if not q:
            return _err("请输入关键词")
        return _ok([f.to_dict() for f in api.search(q)])
    except (AuthError, NetdiskError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"搜索出错: {exc}", 500)


@app.post("/api/fileop")
def api_fileop():
    try:
        api = _require_api()
        payload = request.get_json(silent=True) or {}
        opera = payload.get("opera")
        if opera == "mkdir":
            api.mkdir(payload.get("path", ""))
        elif opera == "delete":
            paths = payload.get("paths") or [payload.get("path", "")]
            api.delete(*[p for p in paths if p])
        elif opera == "rename":
            api.rename(payload.get("path", ""), payload.get("newname", ""))
        elif opera == "move":
            api.move(payload.get("path", ""), payload.get("dest", ""))
        elif opera == "copy":
            api.copy(payload.get("path", ""), payload.get("dest", ""))
        else:
            return _err(f"未知操作: {opera}")
        return _ok()
    except (AuthError, NetdiskError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"文件操作出错: {exc}", 500)


# ---------------------------------------------------------------------------
# 下载任务
# ---------------------------------------------------------------------------
@app.post("/api/download")
def api_download():
    global _TASK_SEQ
    try:
        api = _require_api()
        payload = request.get_json(silent=True) or {}
        path = (payload.get("path") or "").strip()
        if not path:
            return _err("缺少文件路径")
        info = api.get_file_info(path)
        if info is None:
            return _err(f"未找到: {path}")
        if info.is_dir:
            return _err("网页版暂不支持下载整个目录, 请进入目录后下载单个文件")

        dest = os.path.join(_cfg.ensure_download_dir(), info.name)
        if os.path.exists(dest) and not os.path.exists(dest + ".download.json"):
            return _ok({"skipped": True, "dest": dest, "name": info.name})

        with _TASKS_LOCK:
            _TASK_SEQ += 1
            task_id = _TASK_SEQ
            _TASKS[task_id] = {
                "id": task_id,
                "kind": "file",
                "path": path,
                "name": info.name,
                "size": info.size,
                "dest": dest,
                "downloaded": 0,
                "total": info.size,
                "speed": 0.0,
                "status": "排队中",
                "error": "",
                "finished": False,
                "done": False,
            }
        threading.Thread(target=_run_download, args=(task_id,), daemon=True).start()
        return _ok({"task_id": task_id})
    except (AuthError, NetdiskError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"发起下载出错: {exc}", 500)


@app.get("/api/share/list")
def api_share_list():
    try:
        api = _require_api()
        url = (request.args.get("url") or "").strip()
        if not url:
            return _err("缺少分享链接")
        files = api.list_share_files(url)
        return _ok([f.to_dict() for f in files])
    except (AuthError, NetdiskError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"解析分享失败: {exc}", 500)


@app.post("/api/share/download")
def api_share_download():
    global _TASK_SEQ
    try:
        api = _require_api()
        payload = request.get_json(silent=True) or {}
        share_url = (payload.get("url") or "").strip()
        path = (payload.get("path") or "").strip()
        if not share_url or not path:
            return _err("缺少分享链接或文件路径")
        dest = os.path.join(_cfg.ensure_download_dir(), path.rsplit("/", 1)[-1])
        with _TASKS_LOCK:
            _TASK_SEQ += 1
            task_id = _TASK_SEQ
            _TASKS[task_id] = {
                "id": task_id,
                "kind": "share",
                "share_url": share_url,
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "size": 0,
                "dest": dest,
                "downloaded": 0,
                "total": 0,
                "speed": 0.0,
                "status": "排队中",
                "error": "",
                "finished": False,
                "done": False,
            }
        threading.Thread(target=_run_download, args=(task_id,), daemon=True).start()
        return _ok({"task_id": task_id})
    except (AuthError, NetdiskError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"发起分享下载出错: {exc}", 500)


def _run_download(task_id: int) -> None:
    task = _TASKS.get(task_id)
    api = _get_api()
    if not task or not api:
        return
    task["status"] = "排队中"
    _acquire_slot()
    try:
        if task.get("cancel_requested"):
            task["status"] = "已取消"
            task["finished"] = True
            return

        expected_md5 = None
        if task.get("kind") == "share":
            url, name, size = api.get_share_dlink(task["share_url"], task["path"])
            task["name"] = name
            if size:
                task["size"] = size
            dl_headers = api.download_headers(task["path"])
        else:
            info = api.get_file_info(task["path"])
            url = api.get_direct_link(task["path"], info.fs_id)
            expected_md5 = info.md5
            dl_headers = api.download_headers(task["path"])

        dl = DirectLinkDownloader(
            url=url,
            dest_path=task["dest"],
            threads=_cfg.threads,
            speed_limit=_cfg.speed_limit,
            proxy=_cfg.proxy,
            headers=dl_headers,
        )
        task["_dl"] = dl
        if task.get("cancel_requested"):
            dl.cancel()

        def on_progress(d: int, t: int, s: float, st: str) -> None:
            task["downloaded"] = d
            task["total"] = t
            task["speed"] = s
            task["status"] = st

        stats = dl.download(on_progress=on_progress, expected_md5=expected_md5)
        task["finished"] = True
        task["done"] = stats.done
        task["downloaded"] = stats.downloaded
        task["status"] = "完成" if stats.done else "失败"
        task["error"] = stats.error or ""
        append_record(
            HistoryRecord(
                path=task["path"],
                name=task["name"],
                size=task["size"],
                dest=task["dest"],
                status="done" if stats.done else "failed",
                speed_bps=stats.speed_bps,
                error=stats.error or "",
            )
        )
    except Exception as exc:  # noqa: BLE001
        task["finished"] = True
        task["done"] = False
        task["status"] = "失败"
        task["error"] = str(exc)
    finally:
        _release_slot()


@app.get("/api/downloads")
def api_downloads():
    with _TASKS_LOCK:
        return _ok([_public(t) for t in _TASKS.values()])


@app.get("/api/download/<int:task_id>")
def api_download_one(task_id: int):
    task = _TASKS.get(task_id)
    if not task:
        return _err("任务不存在", 404)
    return _ok(_public(task))


@app.post("/api/cancel/<int:task_id>")
def api_cancel(task_id: int):
    task = _TASKS.get(task_id)
    if not task:
        return _err("任务不存在", 404)
    task["cancel_requested"] = True
    dl = task.get("_dl")
    if dl is not None:
        dl.cancel()
    task["status"] = "已取消"
    task["finished"] = True
    return _ok()


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------
@app.get("/api/history")
def api_history():
    return _ok(read_records(limit=100))


def run(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug, threaded=True)


# ---------------------------------------------------------------------------
# 前端页面 (单文件, 无外部依赖)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>百度网盘直链传输工具</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --panel2:#273449; --fg:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --ok:#34d399; --err:#f87171; }
  [data-theme="light"] { --bg:#eef2f7; --panel:#ffffff; --panel2:#e2e8f0; --fg:#0f172a; --muted:#64748b; --accent:#0284c7; --ok:#059669; --err:#dc2626; }
  [data-theme="light"] header, [data-theme="light"] .panel, [data-theme="light"] th, [data-theme="light"] td { border-color:#dbe3ee; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; gap:16px; padding:14px 20px; background:var(--panel); border-bottom:1px solid #334155; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; color:var(--accent); }
  .login-box { margin-left:auto; display:flex; gap:8px; align-items:center; }
  input, button { font-family:inherit; font-size:14px; }
  input[type=text], input[type=password] { background:var(--panel2); border:1px solid #334155; color:var(--fg); padding:8px 10px; border-radius:6px; }
  input[type=text]:focus, input[type=password]:focus { outline:1px solid var(--accent); }
  button { background:var(--accent); color:#082f49; border:0; padding:8px 14px; border-radius:6px; cursor:pointer; font-weight:600; }
  button:hover { filter:brightness(1.1); }
  button.ghost { background:transparent; color:var(--fg); border:1px solid #475569; }
  button.danger { background:var(--err); color:#fff; }
  .tabs { display:flex; gap:4px; padding:10px 20px 0; }
  .tabs button { background:var(--panel2); color:var(--muted); border-radius:8px 8px 0 0; }
  .tabs button.active { background:var(--panel); color:var(--fg); }
  main { padding:20px; }
  .panel { background:var(--panel); border-radius:10px; padding:16px; }
  .toolbar { display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
  .toolbar .path { flex:1; min-width:200px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid #334155; }
  th { color:var(--muted); font-weight:600; font-size:13px; }
  tr:hover { background:var(--panel2); }
  .dir { color:var(--accent); cursor:pointer; }
  .dir:hover { text-decoration:underline; }
  .row-actions { display:flex; gap:6px; }
  .row-actions button { padding:4px 9px; font-size:12px; }
  .bar-wrap { background:#0b1220; height:14px; border-radius:7px; overflow:hidden; min-width:140px; }
  .bar { height:100%; background:linear-gradient(90deg, var(--accent), var(--ok)); transition:width .3s; }
  .muted { color:var(--muted); }
  .ok { color:var(--ok); } .err { color:var(--err); }
  .hidden { display:none; }
  .toast { position:fixed; bottom:20px; right:20px; background:var(--panel2); border:1px solid #475569; padding:12px 16px; border-radius:8px; max-width:420px; }
  .status-dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; background:var(--err); }
  .status-dot.on { background:var(--ok); }
  .settings-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px; }
  .settings-card { background:var(--panel2); border-radius:10px; padding:16px; }
  .settings-card h3 { margin:0 0 12px; font-size:15px; color:var(--accent); }
  .settings-card label { display:block; margin-bottom:10px; font-size:13px; color:var(--muted); }
  .settings-card input, .settings-card select { display:block; width:100%; margin-top:4px; }
  .cred-fields { display:flex; flex-direction:column; gap:8px; margin-bottom:12px; }
  :root { --border:#334155; }
  [data-theme="light"] { --bg:#f4f6fb; --panel2:#f0f4fa; --accent:#2563eb; --border:#e2e8f0; }
  body { line-height:1.6; }
  header { padding:18px 32px; border-color:var(--border); }
  header h1 { color:var(--fg); letter-spacing:-.5px; }
  main { max-width:1440px; margin:auto; padding:28px 32px; }
  .tabs { padding:16px 32px 0; gap:10px; border-bottom:1px solid var(--border); }
  .tabs button { border-radius:8px 8px 0 0; padding:12px 24px; background:transparent; }
  .tabs button.active { color:var(--accent); box-shadow:inset 0 -3px var(--accent); }
  button { border-radius:8px; min-height:38px; transition:filter .15s; }
  button:not(.ghost):not(.danger) { color:white; background:var(--accent); }
  .tabs button:not(.ghost) { color:var(--muted); background:transparent; }
  .tabs button.active { color:var(--accent); }
  button:disabled { opacity:.5; cursor:wait; }
  :focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
  input, select { min-height:38px; border-color:var(--border)!important; }
  .panel { border:1px solid var(--border); border-radius:16px; padding:22px; overflow-x:auto; box-shadow:0 4px 24px #00000004; }
  th,td { border-color:var(--border); padding:14px 12px; }
  th { white-space:nowrap; }
  .workspace-heading { display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; gap:16px; }
  .workspace-heading h2 { margin:4px 0; font-size:28px; letter-spacing:-1px; }
  .workspace-heading p { margin:0; }
  .eyebrow { font-size:11px; letter-spacing:2px; color:var(--accent); font-weight:700; }
  .workspace-badge { color:var(--muted); font-size:13px; }
  .connect-card { display:flex; justify-content:space-between; align-items:center; gap:24px; padding:24px; margin-bottom:24px; border:1px solid var(--border); border-radius:16px; background:var(--panel); border-left:4px solid var(--accent); }
  .connect-card h3 { margin:0 0 8px; } .connect-card p { margin:4px 0; font-size:14px; }
  .connect-card.hidden { display:none; }
  .empty-state { text-align:center; padding:64px 16px; color:var(--muted); }
  #loginFields { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  details { font-size:12px; color:var(--muted); } summary { cursor:pointer; } details input { margin-top:8px; }
  dialog { width:min(520px, calc(100% - 32px)); border:1px solid var(--border); border-radius:20px; padding:32px; background:var(--panel); color:var(--fg); box-shadow:0 24px 100px #0004; }
  dialog::backdrop { background:#0f172a88; backdrop-filter:blur(4px); }
  dialog h2 { font-size:22px; } dialog p { font-size:14px; color:var(--muted); }
  #officialLoginStatus { padding:16px; background:var(--panel2); border-radius:10px; margin:24px 0; overflow-wrap:anywhere; }
  .dialog-actions { display:flex; gap:12px; justify-content:flex-end; }
  .settings-grid { grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr)); }
  .login-banner { margin:0 0 18px; padding:14px 18px; border-radius:12px; border:1px solid var(--border); border-left:4px solid var(--err); background:var(--panel2); font-size:13px; line-height:1.7; overflow-wrap:anywhere; }
  .login-banner.hidden { display:none; }
  .cookie-import { display:flex; flex-direction:column; gap:8px; margin-top:12px; padding-top:12px; border-top:1px dashed var(--border); }
  .cookie-import label { margin-bottom:0; }
  textarea { width:100%; min-height:76px; margin-top:6px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:var(--panel); color:var(--fg); font-family:"Cascadia Mono","Consolas",monospace; font-size:12px; line-height:1.5; resize:vertical; }
  textarea:focus { outline:1px solid var(--accent); }
  .cookie-hint { font-size:12px; line-height:1.6; color:var(--muted); }
  .connect-actions { display:flex; gap:12px; flex-wrap:wrap; }
  @media(max-width:760px) {
    header,main { padding:16px; } .login-box { margin-left:0; flex-wrap:wrap; width:100%; }
    .tabs { padding:8px 12px 0; gap:0; overflow-x:auto; } .tabs button { padding:10px 16px; white-space:nowrap; }
    .workspace-heading h2 { font-size:24px; } .workspace-badge { display:none; }
    .connect-card { align-items:flex-start; flex-direction:column; padding:18px; }
    .panel { padding:14px; } .row-actions { flex-wrap:wrap; } .toolbar .path { min-width:140px; }
  }
</style>
</head>
<body>
<header>
  <h1>📦 网盘直链传输工具</h1>
  <span class="muted"><span id="loginDot" class="status-dot"></span><span id="loginText">未登录</span></span>
  <div class="login-box" id="loginForm">
    <select id="providerSel" onchange="onProviderChange()"></select>
    <span id="loginFields"></span>
    <button id="headerLoginBtn" onclick="doLogin()">连接网盘</button>
    <button class="ghost" onclick="doLogout()" id="logoutBtn" style="display:none">退出</button>
    <button class="ghost" onclick="toggleTheme()" id="themeBtn" title="切换主题">🌙</button>
  </div>
</header>

<div class="tabs">
  <button id="tabFilesBtn" class="active" onclick="switchTab('files')">文件</button>
  <button id="tabShareBtn" onclick="switchTab('share')">分享</button>
  <button id="tabDlBtn" onclick="switchTab('dl')">下载</button>
  <button id="tabHistBtn" onclick="switchTab('hist')">历史</button>
  <button id="tabSetBtn" onclick="switchTab('set')">设置</button>
</div>

<main>
  <div class="workspace-heading"><div><span class="eyebrow">NETDISK WORKSPACE</span><h2>你的文件，一处管理</h2><p class="muted">连接网盘、浏览文件，随时掌握传输进度。</p></div><span class="workspace-badge">多线程传输 · 断点续传</span></div>
  <div id="loginBanner" class="login-banner hidden" role="status" aria-live="polite"></div>
  <section id="connectCard" class="connect-card">
    <div><h3>连接百度网盘</h3><p>打开官方登录窗口，选择扫码、短信或账号密码登录。</p><p class="muted">密码和验证码直接在百度页面输入，完成后自动返回文件列表。若官方窗口登录不上，可改用「粘贴完整 Cookie」。</p></div>
    <div class="connect-actions">
      <button onclick="startOfficialLogin()">打开百度官方登录</button>
      <button class="ghost" onclick="focusCookieImport()">粘贴 Cookie 登录</button>
    </div>
  </section>
  <!-- 文件浏览 -->
  <section id="tab-files" class="panel">
    <div class="toolbar">
      <button onclick="goUp()" title="上级目录">⬆</button>
      <button class="ghost" onclick="refresh()">刷新</button>
      <input type="text" class="path" id="pathInput" placeholder="网盘路径, 如 /我的资料">
      <button onclick="gotoPath()">跳转</button>
      <button class="ghost" onclick="doMkdir()">新建目录</button>
      <input type="text" id="searchInput" placeholder="搜索关键词" style="width:160px">
      <button class="ghost" onclick="doSearch()">搜索</button>
      <button class="ghost" onclick="resetSearch()">清除搜索</button>
    </div>
    <table>
      <thead><tr><th>名称</th><th style="width:110px">大小</th><th style="width:150px">修改时间</th><th style="width:220px">操作</th></tr></thead>
      <tbody id="fileRows"><tr><td colspan="4" class="muted">加载中…</td></tr></tbody>
    </table>
    <div id="fileCount" class="muted" style="margin-top:8px"></div>
  </section>

  <!-- 分享链接 -->
  <section id="tab-share" class="panel hidden">
    <div class="toolbar">
      <input type="text" id="shareUrlInput" placeholder="粘贴分享链接, 如 https://pan.baidu.com/s/1xxx?pwd=abcd" style="flex:1;min-width:260px">
      <button onclick="loadShare()">解析</button>
    </div>
    <table>
      <thead><tr><th>名称</th><th style="width:110px">大小</th><th style="width:220px">操作</th></tr></thead>
      <tbody id="shareRows"><tr><td colspan="3" class="muted">请输入分享链接并点击「解析」</td></tr></tbody>
    </table>
  </section>

  <!-- 下载任务 -->
  <section id="tab-dl" class="panel hidden">
    <table>
      <thead><tr><th>文件</th><th>进度</th><th style="width:120px">速度</th><th style="width:90px">状态</th><th style="width:80px">操作</th></tr></thead>
      <tbody id="dlRows"><tr><td colspan="5" class="muted">暂无下载任务</td></tr></tbody>
    </table>
  </section>

  <!-- 历史 -->
  <section id="tab-hist" class="panel hidden">
    <table>
      <thead><tr><th>时间</th><th>文件</th><th style="width:100px">大小</th><th style="width:80px">状态</th><th>保存位置</th></tr></thead>
      <tbody id="histRows"><tr><td colspan="5" class="muted">暂无记录</td></tr></tbody>
    </table>
  </section>

  <!-- 设置 -->
  <section id="tab-set" class="panel hidden">
    <div class="settings-grid">
      <div class="settings-card">
        <h3>下载设置</h3>
        <label>并发线程数 (1-32)<input type="number" id="setThreads" min="1" max="32"></label>
        <label>队列并发数 (1-8)<input type="number" id="setConcurrent" min="1" max="8"></label>
        <label>下载限速 (如 512k / 1m / 0 不限)<input type="text" id="setSpeed" placeholder="0"></label>
        <label>下载目录<input type="text" id="setDir"></label>
        <label>代理 (可选)<input type="text" id="setProxy" placeholder="http://127.0.0.1:7890"></label>
        <button onclick="saveSettings()">保存设置</button>
      </div>
      <div class="settings-card">
        <h3>网盘设置</h3>
        <label>网盘类型
          <select id="settingsProviderSel" onchange="renderSettingsCredFields()"></select>
        </label>
        <div id="settingsCredFields" class="cred-fields"></div>
        <button onclick="saveCredential()">保存并登录</button>
      </div>
    </div>
  </section>
</main>

<dialog id="officialLoginDialog" aria-labelledby="officialLoginTitle">
  <div class="eyebrow">BAIDU · 官方登录</div><h2 id="officialLoginTitle">在官方窗口完成验证</h2>
  <p>1. 在新窗口选择登录方式</p><p>2. 输入账号密码，或完成扫码 / 短信验证</p><p>3. 登录成功后，文件列表会自动刷新</p>
  <div id="officialLoginStatus" role="status" aria-live="polite"></div>
  <div class="dialog-actions"><button id="officialRetry" onclick="startOfficialLogin()" hidden>重新打开</button><button class="ghost" onclick="closeOfficialLogin()">关闭 / 取消</button></div>
</dialog>
<div id="toast" class="toast hidden" role="status"></div>

<script>
const $ = s => document.querySelector(s);
let currentPath = "/";
let searching = false;

function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.borderColor = isErr ? "#f87171" : "#475569";
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"Content-Type":"application/json"}}, opts || {}));
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "请求失败");
  return j;
}

function fmtSize(n) {
  n = Number(n || 0);
  const u = ["B","KB","MB","GB","TB"];
  let i = 0;
  while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(2)) + " " + u[i];
}
function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
}

function switchTab(name) {
  ["files","share","dl","hist","set"].forEach(n => $("#tab-"+n).classList.toggle("hidden", n !== name));
  ["tabFilesBtn","tabShareBtn","tabDlBtn","tabHistBtn","tabSetBtn"].forEach((id,i) => $("#"+id).classList.toggle("active", ["files","share","dl","hist","set"][i] === name));
  if (name === "hist") loadHistory();
  if (name === "set") refreshStatus();
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  $("#themeBtn").textContent = next === "light" ? "☀️" : "🌙";
}
(function initTheme() {
  const t = localStorage.getItem("theme") || "light";
  document.documentElement.setAttribute("data-theme", t);
  $("#themeBtn").textContent = t === "light" ? "☀️" : "🌙";
})();

let loginTimer;
let loginGeneration = 0;
async function startOfficialLogin() {
  const generation = ++loginGeneration;
  clearTimeout(loginTimer);
  const dialog = $("#officialLoginDialog");
  if (!dialog.open) dialog.showModal();
  $("#officialRetry").hidden = true;
  $("#officialLoginStatus").textContent = "正在打开官方登录窗口…";
  try {
    await api("/api/login/baidu/start", {method:"POST", body:"{}"});
    if (generation === loginGeneration) pollOfficialLogin(generation);
  } catch(e) {
    $("#officialLoginStatus").textContent = e.message;
    $("#officialRetry").hidden = false;
  }
}
async function pollOfficialLogin(generation) {
  try {
    const j = await api("/api/login/baidu/status");
    if (generation !== loginGeneration) return;
    $("#officialLoginStatus").textContent = j.data.message;
    if (j.data.status === "success") {
      $("#officialLoginDialog").close();
      toast("百度网盘登录成功");
      await refreshStatus();
      return;
    }
    if (["error", "cancelled", "expired", "idle"].includes(j.data.status)) {
      $("#officialRetry").hidden = false;
      return;
    }
    loginTimer = setTimeout(() => pollOfficialLogin(generation), 1200);
  } catch(e) {
    if (generation !== loginGeneration) return;
    $("#officialLoginStatus").textContent = e.message;
    $("#officialRetry").hidden = false;
  }
}
async function closeOfficialLogin() {
  ++loginGeneration;
  clearTimeout(loginTimer);
  try {
    await api("/api/login/baidu/cancel", {method:"POST", body:"{}"});
    $("#officialLoginDialog").close();
  } catch(e) { $("#officialLoginStatus").textContent = e.message; }
}
$("#officialLoginDialog").addEventListener("cancel", e => {e.preventDefault(); closeOfficialLogin();});

async function loadShare() {
  const url = $("#shareUrlInput").value.trim();
  if (!url) return toast("请输入分享链接", true);
  try {
    const j = await api("/api/share/list?url=" + encodeURIComponent(url));
    renderShare(j.data, url);
  } catch(e) { toast(e.message, true); }
}

function renderShare(files, url) {
  const rows = $("#shareRows");
  rows.innerHTML = "";
  if (!files.length) { rows.innerHTML = '<tr><td colspan="3" class="muted">(空分享)</td></tr>'; return; }
  files.forEach(f => {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = (f.is_dir ? "📁 " : "📄 ") + f.name;
    tr.appendChild(nameTd);
    const sizeTd = document.createElement("td");
    sizeTd.textContent = f.is_dir ? "-" : fmtSize(f.size);
    tr.appendChild(sizeTd);
    const act = document.createElement("td");
    act.className = "row-actions";
    if (!f.is_dir) {
      const b = document.createElement("button");
      b.textContent = "下载";
      b.onclick = () => downloadShare(url, f.path);
      act.appendChild(b);
    }
    tr.appendChild(act);
    rows.appendChild(tr);
  });
}

async function downloadShare(url, path) {
  try {
    const j = await api("/api/share/download", {method:"POST", body: JSON.stringify({url, path})});
    toast("已加入下载队列");
    switchTab("dl");
    pollDownloads();
  } catch(e) { toast(e.message, true); }
}

let LOGGED = false;
let PROVIDERS = [];

function metaOf(name) {
  return PROVIDERS.find(m => m.name === name);
}

async function loadProviders() {
  try {
    const j = await api("/api/providers");
    PROVIDERS = j.data || [];
    ["#providerSel", "#settingsProviderSel"].forEach(selId => {
      const sel = $(selId);
      if (!sel) return;
      sel.innerHTML = "";
      PROVIDERS.forEach(m => {
        const o = document.createElement("option");
        o.value = m.name; o.textContent = m.label;
        sel.appendChild(o);
      });
    });
  } catch(e) {}
}

function buildCookieImport() {
  const wrap = document.createElement("div");
  wrap.className = "cookie-import";
  const label = document.createElement("label");
  label.textContent = "粘贴完整 Cookie（推荐，只填 BDUSS 在部分账号上不够用）";
  const ta = document.createElement("textarea");
  ta.className = "cookie-paste-input";
  ta.placeholder = "BDUSS=xxxx; STOKEN=yyyy; BAIDUID=zzzz; …";
  ta.spellcheck = false;
  label.appendChild(ta);
  wrap.appendChild(label);
  const btn = document.createElement("button");
  btn.textContent = "用 Cookie 登录";
  const hint = document.createElement("div");
  hint.className = "cookie-hint";
  hint.textContent = "获取方法：浏览器登录 pan.baidu.com → F12 → 网络(Network) → 任意请求 → 请求标头里的 Cookie 整行复制。";
  btn.onclick = () => loginWithCookie(ta, btn, hint);
  wrap.appendChild(btn);
  wrap.appendChild(hint);
  return wrap;
}

async function loginWithCookie(ta, btn, hint) {
  const raw = ta.value.trim();
  if (!raw) { toast("请先粘贴 Cookie", true); ta.focus(); return; }
  btn.disabled = true;
  hint.textContent = "正在用百度校验这份 Cookie…";
  try {
    const j = await api("/api/login/baidu/cookie", {method:"POST", body: JSON.stringify({cookie: raw})});
    const d = j.data || {};
    const names = d.cookies || [];
    ta.value = "";
    hint.textContent = `已导入 ${names.length} 个 Cookie：${names.join(", ")}` + ((d.notes || []).length ? "。" + d.notes.join(" ") : "");
    toast("百度网盘登录成功");
    await refreshStatus();
  } catch(e) {
    hint.textContent = e.message;
    toast("登录失败", true);
  } finally {
    btn.disabled = false;
  }
}

async function focusCookieImport() {
  switchTab("set");
  await refreshStatus();  // 等状态刷新把设置区重新渲染完, 否则焦点会被覆盖
  const ta = document.querySelector("#settingsCredFields .cookie-paste-input");
  if (!ta) return toast("请先把网盘类型切换为百度网盘", true);
  ta.focus();
  ta.scrollIntoView({block:"center"});
}

function renderCredFields(container, meta) {
  container.innerHTML = "";
  if (!meta) return;
  let fieldHost = container;
  if (meta.name === "baidu") {
    if (container.id !== "loginFields") {
      const btn = document.createElement("button");
      btn.textContent = "官方网页登录";
      btn.onclick = startOfficialLogin;
      container.appendChild(btn);
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "高级：手动填写 BDUSS";
    details.appendChild(summary);
    container.appendChild(details);
    fieldHost = details;
    if (container.id !== "loginFields") container.appendChild(buildCookieImport());
  }
  meta.fields.forEach(f => {
    const inp = document.createElement("input");
    inp.type = f.type;
    inp.dataset.key = f.key;
    inp.placeholder = f.label;
    inp.style.width = (f.type === "text" ? 200 : 150) + "px";
    fieldHost.appendChild(inp);
  });
}

function renderLoginFields() {
  const box = $("#loginFields");
  box.innerHTML = "";
  if (LOGGED) return;
  renderCredFields(box, metaOf($("#providerSel").value));
}

function renderSettingsCredFields() {
  renderCredFields($("#settingsCredFields"), metaOf($("#settingsProviderSel").value));
}

function onProviderChange() { renderCredFields($("#loginFields"), metaOf($("#providerSel").value)); $("#headerLoginBtn").hidden = false; }

async function refreshStatus() {
  try {
    const j = await api("/api/status");
    LOGGED = j.data.logged;
    $("#connectCard").classList.toggle("hidden", LOGGED || j.data.provider !== "baidu");
    $("#headerLoginBtn").hidden = LOGGED;
    if (!LOGGED) $("#fileRows").innerHTML = '<tr><td colspan="4" class="empty-state">连接网盘后，即可浏览和下载文件。请点击「连接网盘」开始。</td></tr>';
    const cookieCount = (j.data.cookie_names || []).length;
    $("#loginText").textContent = LOGGED ? (cookieCount ? `已登录 · ${cookieCount} Cookie` : "已登录") : "未登录";
    $("#loginDot").classList.toggle("on", LOGGED);
    $("#logoutBtn").style.display = LOGGED ? "inline-block" : "none";
    const banner = $("#loginBanner");
    if (!LOGGED && j.data.last_error) {
      banner.textContent = "登录提示：" + j.data.last_error;
      banner.classList.remove("hidden");
    } else {
      banner.textContent = "";
      banner.classList.add("hidden");
    }
    if (PROVIDERS.length === 0) await loadProviders();
    $("#providerSel").value = j.data.provider;
    $("#settingsProviderSel").value = j.data.provider;
    renderLoginFields();
    renderSettingsCredFields();
    loadSettings(j.data);
    if (LOGGED && !searching) listDir(currentPath);
    else if (LOGGED && searching) doSearch();
  } catch(e) {}
}

async function doLogin() {
  const p = $("#providerSel").value;
  const meta = metaOf(p);
  const manual = $(p === "baidu" ? "#loginFields [data-key=bduss]" : "#providerSel");
  if (p === "baidu" && (!manual || !manual.value.trim())) return startOfficialLogin();
  const payload = { provider: p };
  let ok = true;
  if (meta) {
    for (const f of meta.fields) {
      const el = $("#loginFields").querySelector(`[data-key="${f.key}"]`);
      if (!el) continue;
      if (!el.value.trim()) { toast("请输入 " + f.label, true); ok = false; break; }
      payload[f.key] = el.value;
    }
  }
  if (!ok) return;
  try {
    await api("/api/login", {method:"POST", body: JSON.stringify(payload)});
    toast("登录成功"); refreshStatus();
  } catch(e) { toast(e.message, true); }
}

async function saveCredential() {
  const p = $("#settingsProviderSel").value;
  if (p === "baidu" && !$("#settingsCredFields [data-key=bduss]")?.value.trim()) return startOfficialLogin();
  const meta = metaOf(p);
  const payload = { provider: p };
  let ok = true;
  if (meta) {
    for (const f of meta.fields) {
      const el = $("#settingsCredFields").querySelector(`[data-key="${f.key}"]`);
      if (!el) continue;
      if (!el.value.trim()) { toast("请输入 " + f.label, true); ok = false; break; }
      payload[f.key] = el.value;
    }
  }
  if (!ok) return;
  try {
    await api("/api/login", {method:"POST", body: JSON.stringify(payload)});
    toast("网盘凭证已保存并登录"); refreshStatus();
  } catch(e) { toast(e.message, true); }
}

function loadSettings(data) {
  if (!data) return;
  $("#setThreads").value = data.threads;
  $("#setConcurrent").value = data.max_concurrent;
  $("#setSpeed").value = data.speed_limit || 0;
  $("#setDir").value = data.download_dir || "";
  $("#setProxy").value = data.proxy || "";
}

function parseSpeed(v) {
  v = String(v || "").trim().toLowerCase();
  if (!v || v === "0" || v === "unlimited" || v === "none") return 0;
  let mult = 1;
  if (v.endsWith("k")) { mult = 1024; v = v.slice(0, -1); }
  else if (v.endsWith("m")) { mult = 1024 * 1024; v = v.slice(0, -1); }
  else if (v.endsWith("g")) { mult = 1024 ** 3; v = v.slice(0, -1); }
  const n = parseFloat(v);
  return isNaN(n) || n < 0 ? 0 : Math.round(n * mult);
}

async function saveSettings() {
  const payload = {
    threads: parseInt($("#setThreads").value, 10) || 4,
    max_concurrent: parseInt($("#setConcurrent").value, 10) || 2,
    speed_limit: parseSpeed($("#setSpeed").value),
    download_dir: $("#setDir").value.trim(),
    proxy: $("#setProxy").value.trim(),
  };
  try {
    await api("/api/config", {method:"POST", body: JSON.stringify(payload)});
    toast("设置已保存"); refreshStatus();
  } catch(e) { toast(e.message, true); }
}

async function doLogout() {
  try { await api("/api/logout", {method:"POST"}); toast("已退出"); refreshStatus(); listDir("/"); }
  catch(e) { toast(e.message, true); }
}

async function listDir(path) {
  currentPath = path;
  $("#pathInput").value = path;
  try {
    const j = await api("/api/list?path=" + encodeURIComponent(path));
    renderFiles(j.data);
  } catch(e) { toast(e.message, true); }
}

function renderFiles(files) {
  const rows = $("#fileRows");
  rows.innerHTML = "";
  if (!files.length) { rows.innerHTML = '<tr><td colspan="4" class="muted">(空目录)</td></tr>'; }
  files.forEach(f => {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    if (f.is_dir) {
      nameTd.innerHTML = `<span class="dir">📁 ${escapeHtml(f.name)}/</span>`;
      nameTd.onclick = () => listDir(f.path);
    } else {
      nameTd.textContent = "📄 " + f.name;
    }
    tr.appendChild(nameTd);
    const sizeTd = document.createElement("td");
    sizeTd.textContent = f.is_dir ? "-" : fmtSize(f.size);
    tr.appendChild(sizeTd);
    const timeTd = document.createElement("td");
    timeTd.textContent = fmtTime(f.server_mtime);
    tr.appendChild(timeTd);
    const act = document.createElement("td");
    act.className = "row-actions";
    if (!f.is_dir) {
      const b = document.createElement("button");
      b.textContent = "下载";
      b.onclick = () => downloadFile(f.path);
      act.appendChild(b);
    }
    const ren = document.createElement("button");
    ren.textContent = "重命名"; ren.className = "ghost";
    ren.onclick = () => doRename(f.path, f.name);
    act.appendChild(ren);
    const del = document.createElement("button");
    del.textContent = "删除"; del.className = "ghost danger";
    del.onclick = () => doDelete(f.path, f.name);
    act.appendChild(del);
    tr.appendChild(act);
    rows.appendChild(tr);
  });
  $("#fileCount").textContent = `共 ${files.length} 项`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function refresh() { listDir(currentPath); }
function gotoPath() { let p = $("#pathInput").value.trim() || "/"; if (!p.startsWith("/")) p = "/" + p; listDir(p); }
function goUp() { if (currentPath === "/") return; currentPath = currentPath.replace(/\/$/, ""); currentPath = currentPath.slice(0, currentPath.lastIndexOf("/")) || "/"; listDir(currentPath); }

async function doMkdir() {
  const name = prompt("目录名:");
  if (!name) return;
  const full = (currentPath === "/" ? "" : currentPath) + "/" + name;
  try { await api("/api/fileop", {method:"POST", body: JSON.stringify({opera:"mkdir", path:full})}); toast("已创建"); listDir(currentPath); }
  catch(e) { toast(e.message, true); }
}
async function doRename(path, old) {
  const name = prompt("新名称:", old);
  if (!name || name === old) return;
  try { await api("/api/fileop", {method:"POST", body: JSON.stringify({opera:"rename", path, newname:name})}); listDir(currentPath); }
  catch(e) { toast(e.message, true); }
}
async function doDelete(path, name) {
  if (!confirm(`确认删除 ${name} ?`)) return;
  try { await api("/api/fileop", {method:"POST", body: JSON.stringify({opera:"delete", path})}); toast("已删除"); listDir(currentPath); }
  catch(e) { toast(e.message, true); }
}

async function doSearch() {
  const q = $("#searchInput").value.trim();
  if (!q) return resetSearch();
  searching = true;
  try {
    const j = await api("/api/search?q=" + encodeURIComponent(q));
    renderFiles(j.data);
    $("#fileCount").textContent = `搜索 "${q}" 共 ${j.data.length} 项`;
  } catch(e) { toast(e.message, true); }
}
function resetSearch() { searching = false; $("#searchInput").value = ""; listDir(currentPath); }

async function downloadFile(path) {
  try {
    const j = await api("/api/download", {method:"POST", body: JSON.stringify({path})});
    if (j.data && j.data.skipped) return toast(`文件已存在: ${j.data.name}`, true);
    toast("已加入下载任务");
    switchTab("dl");
    pollDownloads();
  } catch(e) { toast(e.message, true); }
}

async function pollDownloads() {
  try {
    const j = await api("/api/downloads");
    const rows = $("#dlRows");
    const tasks = j.data || [];
    if (!tasks.length) { rows.innerHTML = '<tr><td colspan="5" class="muted">暂无下载任务</td></tr>'; return; }
    rows.innerHTML = "";
    tasks.forEach(t => {
      const tr = document.createElement("tr");
      const pct = t.total > 0 ? (t.downloaded / t.total * 100) : (t.done ? 100 : 0);
      tr.innerHTML = `<td>${escapeHtml(t.name)}</td>
        <td><div class="bar-wrap"><div class="bar" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="muted">${fmtSize(t.downloaded)}/${fmtSize(t.total)}</span></td>
        <td>${t.speed > 0 ? fmtSize(t.speed)+"/s" : "-"}</td>
        <td class="${t.done ? "ok" : (t.error ? "err" : "")}">${escapeHtml(t.status)}</td>`;
      const act = document.createElement("td");
      if (!t.finished) {
        const b = document.createElement("button");
        b.textContent = "取消"; b.className = "ghost danger";
        b.onclick = () => cancelTask(t.id);
        act.appendChild(b);
      }
      tr.appendChild(act);
      rows.appendChild(tr);
    });
  } catch(e) {}
}
async function cancelTask(id) {
  try { await api("/api/cancel/" + id, {method:"POST"}); pollDownloads(); }
  catch(e) { toast(e.message, true); }
}

async function loadHistory() {
  try {
    const j = await api("/api/history");
    const rows = $("#histRows");
    const recs = j.data || [];
    if (!recs.length) { rows.innerHTML = '<tr><td colspan="5" class="muted">暂无记录</td></tr>'; return; }
    rows.innerHTML = "";
    recs.forEach(r => {
      const tr = document.createElement("tr");
      const mark = r.status === "done" ? "✅" : (r.status === "cancelled" ? "⏸" : "❌");
      tr.innerHTML = `<td class="muted">${escapeHtml(r.time || "")}</td><td>${escapeHtml(r.name || "")}</td>
        <td>${fmtSize(r.size)}</td><td>${mark}</td><td class="muted" style="font-size:12px">${escapeHtml(r.dest || "")}</td>`;
      rows.appendChild(tr);
    });
  } catch(e) {}
}

refreshStatus();
setInterval(pollDownloads, 800);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    run()
