"""网盘提供者注册与工厂。

扩展新网盘的步骤:
1. 在 providers/ 下新建模块, 实现 NetdiskProvider 接口 (继承 base.NetdiskProvider)。
2. 在此注册 builder (register(name, builder)), 并补充 PROVIDER_META 中的元信息。
3. 在 AppConfig 中补充该网盘所需的凭证字段。
"""

from __future__ import annotations

from .base import NetdiskProvider

_BUILDERS: dict = {}

# 提供者元信息: 名称 -> {label: 显示名, fields: [(config字段, 标签, 输入类型)]}
PROVIDER_META: dict = {
    "baidu": {"label": "百度网盘", "fields": [("bduss", "BDUSS", "password")]},
    "webdav": {
        "label": "WebDAV",
        "fields": [
            ("webdav_url", "地址", "text"),
            ("username", "用户名", "text"),
            ("password", "密码/应用密码", "password"),
        ],
    },
    "aliyun": {"label": "阿里云盘", "fields": [("aliyun_refresh_token", "Refresh Token", "password")]},
    "quark": {"label": "夸克网盘", "fields": [("quark_cookie", "Cookie", "password")]},
    "iflytek": {
        "label": "讯飞",
        "fields": [
            ("iflytek_url", "服务地址", "text"),
            ("iflytek_token", "Token", "password"),
        ],
    },
}


def register(name: str, builder) -> None:
    """注册提供者构造器。builder 接收 AppConfig 返回 provider 实例。"""
    _BUILDERS[name] = builder


def get_provider_names() -> list[str]:
    return list(_BUILDERS.keys())


def get_provider_meta() -> list[dict]:
    """返回已注册提供者的元信息 (供 Web UI 动态渲染登录表单)。"""
    return [
        {
            "name": name,
            "label": meta["label"],
            "fields": [{"key": k, "label": l, "type": t} for k, l, t in meta["fields"]],
        }
        for name, meta in PROVIDER_META.items()
        if name in _BUILDERS
    ]


def create_provider(name: str, cfg) -> NetdiskProvider:
    """根据配置创建网盘提供者实例。"""
    if name not in _BUILDERS:
        from ..api import NetdiskError

        raise NetdiskError(f"未知网盘类型: {name} (可用: {', '.join(get_provider_names())})")
    return _BUILDERS[name](cfg)


# ---------------------------------------------------------------------------
# 各提供者构造器
# ---------------------------------------------------------------------------
def _build_baidu(cfg):
    from ..api import BaiduNetdiskAPI

    api = BaiduNetdiskAPI(cfg.bduss, proxy=cfg.proxy)
    if cfg.baidu_cookies:
        api.session.cookies.clear()
        for cookie in cfg.baidu_cookies:
            api.session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path="/")
        # 完整 Cookie 里若没带 BDUSS, 仍要保证构造时给的 BDUSS 在会话里
        if cfg.bduss and not any(c["name"] in ("BDUSS", "BDUSS_BFESS") for c in cfg.baidu_cookies):
            api.session.cookies.set("BDUSS", cfg.bduss, domain="pan.baidu.com")
    if cfg.baidu_user_agent:
        api.session.headers["User-Agent"] = cfg.baidu_user_agent
    api.session.headers["Referer"] = "https://pan.baidu.com/disk/main"
    return api


def _build_webdav(cfg):
    from .webdav import WebDAVProvider

    return WebDAVProvider(cfg.webdav_url, username=cfg.username, password=cfg.password, proxy=cfg.proxy)


def _build_aliyun(cfg):
    from .aliyun import AliyunProvider

    return AliyunProvider(cfg.aliyun_refresh_token, drive_id=cfg.aliyun_drive_id, proxy=cfg.proxy)


def _build_quark(cfg):
    from .quark import QuarkProvider

    return QuarkProvider(cfg.quark_cookie, proxy=cfg.proxy)


def _build_iflytek(cfg):
    from .iflytek import IflytekProvider

    return IflytekProvider(cfg.iflytek_token, url=cfg.iflytek_url, proxy=cfg.proxy)


register("baidu", _build_baidu)
register("webdav", _build_webdav)
register("aliyun", _build_aliyun)
register("quark", _build_quark)
register("iflytek", _build_iflytek)
