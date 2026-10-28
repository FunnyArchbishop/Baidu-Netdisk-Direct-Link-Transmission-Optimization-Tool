"""把浏览器里复制出来的登录文本解析成百度网盘凭证。

BDUSS 单独一个值在部分账号上已经不够用（百度会返回 errno=-6），
因此支持直接粘贴 DevTools 里整段 Cookie：

- ``BDUSS=xxx; STOKEN=yyy; BAIDUID=zzz``（请求头整行）
- 带 ``Cookie:`` 前缀、或每行一条 ``name=value``
- Chrome「应用 → Cookie」表格整块复制（制表符分列）
- 只粘贴一个 BDUSS 值（兼容旧用法）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 百家站点 Cookie 统一挂在这个域, 对 pan.baidu.com / passport.baidu.com 都生效
COOKIE_DOMAIN = ".baidu.com"

# Chrome Cookie 表格复制进来时会带上的列名, 它们不是 Cookie
_ATTRIBUTE_NAMES = frozenset(
    {
        "path", "domain", "expires", "max-age", "secure", "httponly", "samesite",
        "priority", "partitioned", "hostonly", "session", "true", "false", "name", "value",
    }
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_BARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-+/=%.:]{20,}$")

REQUIRED_HINT = (
    "请先在浏览器登录 https://pan.baidu.com，按 F12 → 网络(Network) → 任意 pan 请求 → "
    "请求标头里的 Cookie 整行复制后粘贴。"
)


class CredentialError(ValueError):
    """粘贴内容无法解析成凭证 (消息直接展示给用户)。"""


@dataclass
class BaiduCredentials:
    """解析后的百度凭证。"""

    bduss: str
    cookies: list = field(default_factory=list)  # [{"name","value","domain","path"}]
    notes: list = field(default_factory=list)
    alt_bduss: str = ""  # BDUSS_BFESS 等备用登录态

    @property
    def cookie_names(self) -> list:
        return [c["name"] for c in self.cookies]


def _clean_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def parse_cookie_text(raw: str) -> dict:
    """把粘贴文本解析为 {name: value}（保持出现顺序，重名后者覆盖）。"""
    if not isinstance(raw, str) or not raw.strip():
        return {}

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    pairs: dict = {}

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 整行 Cookie 头: "Cookie: a=1; b=2"
        line = re.sub(r"^cookie\s*:\s*", "", line, flags=re.IGNORECASE).strip()
        # 表格粘贴: 列之间是制表符, 第二列才是值
        if "\t" in line and "=" not in line.split("\t")[0]:
            cols = [c.strip() for c in line.split("\t") if c.strip()]
            if len(cols) >= 2 and _NAME_RE.match(cols[0]) and cols[0].lower() not in _ATTRIBUTE_NAMES:
                pairs[cols[0]] = _clean_value(cols[1])
                continue
        for segment in line.split(";"):
            segment = segment.strip()
            if not segment or "=" not in segment:
                continue
            name, value = segment.split("=", 1)
            name = name.strip()
            if not _NAME_RE.match(name) or name.lower() in _ATTRIBUTE_NAMES:
                continue
            pairs[name] = _clean_value(value)
    return pairs


def build_baidu_credentials(raw: str) -> BaiduCredentials:
    """解析粘贴内容并校验其中是否真的含有登录态。

    失败时抛 :class:`CredentialError`，消息里带上可操作的建议。
    """
    pairs = parse_cookie_text(raw)

    if not pairs:
        token = (raw or "").strip()
        if _BARE_TOKEN_RE.match(token):
            # 兼容旧用法: 直接输入一个 BDUSS 值
            return BaiduCredentials(
                bduss=token,
                cookies=[{"name": "BDUSS", "value": token, "domain": COOKIE_DOMAIN, "path": "/"}],
                notes=["识别到单个令牌，已作为 BDUSS 使用；若校验失败请改粘贴完整 Cookie。"],
            )
        raise CredentialError("没有识别到任何 Cookie 键值对。" + REQUIRED_HINT)

    bduss = pairs.get("BDUSS", "")
    alt = pairs.get("BDUSS_BFESS", "")
    if not bduss and not alt:
        raise CredentialError(
            "粘贴内容里没有 BDUSS 或 BDUSS_BFESS。"
            "只复制到 BAIDUID 之类匿名 Cookie 说明当时还没登录成功。" + REQUIRED_HINT
        )

    notes: list = []
    if not bduss:
        notes.append("未发现 BDUSS，已改用 BDUSS_BFESS 作为登录凭证（新版账号常见）。")
    if not pairs.get("STOKEN"):
        notes.append("缺少 STOKEN，分享转存等部分操作可能受限。")

    cookies = [
        {"name": name, "value": value, "domain": COOKIE_DOMAIN, "path": "/"}
        for name, value in pairs.items()
        if value
    ]
    return BaiduCredentials(
        bduss=bduss or alt,
        cookies=cookies,
        notes=notes,
        alt_bduss=alt if bduss and alt else "",
    )
