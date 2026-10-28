# 网盘直链传输优化工具(Self USE)

基于**直链传输技术**的网盘传输优化程序（**多网盘通用**）。它将网盘文件解析为**可直接分段的 HTTP 直链**, 再通过**多线程分段并发下载**、**断点续传**与**失败自动重试**等策略大幅提升传输速度与稳定性。

> 内置提供者: **百度网盘** (BDUSS) 与 **WebDAV** (坚果云 / Nextcloud / ownCloud 等)。下载器本身直链通用, 通过 Provider 插件架构即可扩展更多网盘。

> **声明**: 本项目仅用于个人学习、数据备份与合法内容下载, 请遵守各网盘用户协议与相关法律法规。勿用于下载侵权内容。

## 特性

- **BDUSS 会话认证** — 无需 OAuth 复杂流程, 直接使用浏览器 Cookie 中的 BDUSS
- **完整 Cookie 导入** — BDUSS 单值被百度判为 `errno=-6` 时, 直接粘贴浏览器整段 Cookie 即可登录 (自动兼容 `BDUSS_BFESS`)
- **直链解析** — 通过 PCS `file?method=download` 接口获取支持 `Range` 的 CDN 直链
- **多线程分段下载** — 文件切分为多个区间并行请求, 充分利用带宽
- **断点续传** — 中断后保留 `.part` 与状态文件, 再次执行即可续传
- **自动重试** — 单块失败指数退避重试, 提高弱网环境成功率
- **完整性校验** — 下载完成后自动比对 MD5 (若网盘返回), 确保文件无损
- **实时进度** — 进度条、百分比、速度实时刷新
- **双模式** — 交互式 REPL 与一次性命令行均可使用

## 安装

需要 Python 3.9+。

```bash
pip install -r requirements.txt
```

## Windows 固定环境启动（解决已安装却仍提示缺少组件）

双击项目根目录的 `start-web.cmd`，打开 `http://127.0.0.1:5001`。
脚本固定使用 `.venv-web\Scripts\python.exe`，不受系统 PATH 中的 MSYS2 Python 影响。
保留启动窗口；关闭服务按 Ctrl+C。旧的 5000 页面和旧 EXE 不会自动切换到这个环境。
也可停止旧服务后重新运行 `python main.py --web`：当当前解释器无法加载 Playwright 时，入口会自动切换到已经配置好的 `.venv-web`，仍使用 5000 端口。

首次在另一台电脑配置时，安装 uv 后在项目目录运行 PowerShell：

```powershell
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.runtime\python'
$env:UV_CACHE_DIR = Join-Path $PWD '.runtime\cache'
uv venv --python 3.13 --managed-python .venv-web
uv pip install --python .venv-web\Scripts\python.exe -r requirements.txt
.\start-web.cmd
```

安装依赖必须对应实际启动程序的解释器。给系统 Python 安装库不会更新已打包的 `pan-opt.exe`。

## 官方网页登录（推荐）

使用官方 Windows 64 位 Python（MSYS2 / MinGW Python 无法安装 Playwright），执行
`python -m pip install -r requirements.txt`，然后运行 `python main.py --web`。
在运行程序的电脑上打开 `http://127.0.0.1:5000`，点击「打开百度官方登录」。

- 程序使用独立浏览器会话加载真实的 `https://pan.baidu.com/`，优先使用 Edge / Chrome。
- 账号密码、短信验证码、扫码及可能出现的二次验证均由官方页面处理；本工具不读取或保存密码。
- 完成验证后自动读取 BDUSS，校验成功才保存至本地 `config.json` 并刷新文件列表。BDUSS 属于敏感登录凭证。
- 登录窗口可取消，等待超过 5 分钟可重试；关闭窗口不会覆盖原有配置。
- 若没有可用浏览器，运行 `python -m playwright install chromium`。
- 此功能用于本机桌面，远程访问时请使用「高级：手动填写 BDUSS」。

浏览器启动方式参考 [Playwright 官方文档](https://playwright.dev/python/docs/browsers)。

## 粘贴完整 Cookie（官方登录进不去 / BDUSS 校验失败时用这个）

百度对部分账号只认「整段 Cookie」, 单独一个 BDUSS 会被判为 `errno=-6(身份验证失败)`。
此时用网页界面的 Cookie 导入通道最稳:

1. 在**你自己的浏览器**里正常登录 <https://pan.baidu.com>(确认能看到文件列表)。
2. `F12` → **网络 / Network** → 点任意一条 `pan.baidu.com` 请求 → **请求标头 / Request Headers** →
   把 `Cookie:` 后面**整行**复制(形如 `BDUSS=xxx; STOKEN=yyy; BAIDUID=zzz; …`)。
3. 打开本工具网页 → **设置 → 网盘设置 → 百度网盘 → 粘贴完整 Cookie** → 点「用 Cookie 登录」。

也可以直接粘贴 Chrome「应用 / Application → Cookie」表格里复制出来的多行内容, 或用命令行:
`python main.py --login "BDUSS=xxx; STOKEN=yyy; BAIDUID=zzz"`、交互式里 `login BDUSS=xxx; STOKEN=yyy`。

说明:

- 解析结果只保留 `名字=值`, 会自动丢掉 `Path/Domain/Expires/HttpOnly` 这类属性列。
- 只有 `BDUSS_BFESS`(新版账号常见)时自动改用它; 两者都在则先用 BDUSS, 失败再自动重试 BDUSS_BFESS。
- **校验通过才写入 `config.json`**; 失败时界面会显示百度返回的原始 errno 与本次解析到的 Cookie 名字(不显示值)。
- 仍然过不去说明登录态本身已失效: 重新登录百度后再复制, 或改用「官方网页登录」窗口扫码。

## 获取 BDUSS（手动方式）

1. 浏览器登录 [百度网盘网页版](https://pan.baidu.com)
2. 按 `F12` 打开开发者工具 → `Application` (应用) → `Cookies` → `https://pan.baidu.com`
3. 找到名为 `BDUSS` 的 Cookie, 复制其值 (较长的一串)

## 使用

### 方式一: 交互式

```bash
python main.py
```

```
pan:// > login 你的BDUSS值
pan:// > ls /我的资料
pan:// > cd /我的资料/电影
pan:// > download 盗梦空间.mkv
```

### 方式二: 一次性命令

```bash
# 登录
python main.py --login 你的BDUSS值

# 设置并发线程数
python main.py --threads 8

# 列出目录
python main.py --list /我的资料

# 下载文件到指定目录
python main.py --download /我的资料/example.zip --output D:/Downloads
```

### 方式三: Web 界面

```bash
python main.py --web                 # 默认 http://127.0.0.1:5000
python main.py --web --port 8080     # 自定义端口
```

浏览器打开后即可使用，界面包含以下标签页：

| 标签页 | 功能 |
| --- | --- |
| 文件 | 登录后浏览 / 搜索 / 管理文件，点击下载 |
| 分享 | 粘贴分享链接解析并下载（百度网盘，下载需登录 BDUSS） |
| 下载 | 多线程下载任务队列，实时进度条 + 速度，可取消 |
| 历史 | 查看下载历史记录 |
| **设置** | **网盘设置**（切换网盘类型 + 凭证 + 保存并登录）与**下载设置**（线程数 / 队列并发 / 限速 / 下载目录 / 代理） |

## 网盘提供者

内置五个网盘提供者，页头或「设置」面板可切换：

| 名称 | 凭证 | 说明 |
| --- | --- | --- |
| 百度网盘 | BDUSS | 全功能（搜索/分享/文件管理） |
| WebDAV | 地址 + 用户名 + 密码 | 坚果云 / Nextcloud / ownCloud 等 |
| 阿里云盘 | Refresh Token | 官方 Open API |
| 夸克网盘 | Cookie | 社区逆向接口（需抓 Cookie） |
| 讯飞 | 服务地址 + Token | 通用 REST 模板 |

> 架构基于 `netdisk/providers/` 插件式 Provider 接口，扩展新网盘只需新增一个 provider 类并注册，下载/续传/限速/进度等能力自动复用。

## 分享下载说明（百度网盘）

百度网盘的「分享链接」功能分两个阶段，**实测结论**如下：

| 能力 | 是否需要登录 | 说明 |
| --- | --- | --- |
| 解析链接 + 列出分享文件 | ❌ 无需登录 | `share <链接>` 即可列出文件与提取码校验 |
| 下载分享文件（获取直链） | ✅ **需要登录 BDUSS** | 百度对未登录会话（`loginstate=0`）不返回 `dlink` 下载直链 |

**使用步骤**：

```bash
# 1. 先登录 (写入 BDUSS)
python main.py --login <你的BDUSS>

# 2. 解析分享链接并列出文件
python main.py            # 进入交互式
pan:// > share https://pan.baidu.com/s/xxxx?pwd=abcd

# 3. 用列出的路径下载 (或网页端「分享」标签页直接点下载)
pan:// > download /分享路径/文件名.zip
```

> 提示：BDUSS 是账号登录凭证，等同登录态，请勿泄露给他人。仅用于个人学习与数据备份，下载内容请遵守版权与平台协议。

## 交互式命令一览

| 命令 | 说明 |
| --- | --- |
| `login <BDUSS>` | 登录（百度网盘） |
| `status` | 查看登录状态与配置 |
| `ls [路径]` | 列出目录内容 |
| `cd <路径>` | 切换目录 (`..` 返回上级) |
| `pwd` | 显示当前目录 |
| `download <文件或目录> [本地目录]` | 直链多线程下载（支持目录） |
| `mkdir` / `rm` / `mv` / `cp` / `rename` | 文件管理 |
| `search <关键词>` | 搜索文件 |
| `share <链接>` | 解析分享链接 |
| `history` | 查看下载历史 |
| `set provider <名称>` | 切换网盘类型 |
| `set threads <N>` | 设置并发线程数 (1-32) |
| `set speed <速率>` | 设置下载限速 (如 512k / 1m) |
| `set dir <本地目录>` | 设置默认下载目录 |
| `exit` / `quit` | 退出 |

## 配置说明

配置保存在 `config.json`:

| 字段 | 说明 |
| --- | --- |
| `provider` | 当前网盘类型 (`baidu`/`webdav`/`aliyun`/`quark`/`iflytek`) |
| `bduss` | 百度网盘登录凭证 |
| `baidu_cookies` | 完整 Cookie 集合(数组, 每项 `{name,value,domain}`; 比单个 `bduss` 更可靠) |
| `baidu_user_agent` | 与 Cookie 配套的浏览器 UA (官方登录时自动记录) |
| `download_dir` | 默认下载保存目录 |
| `threads` | 并发线程数 (建议 4-8，不能保证突破服务端账号限速) |
| `speed_limit` | 下载限速 (字节/秒, 0 不限速) |
| `max_concurrent` | Web 界面同时下载的最大任务数 (1-8) |
| `proxy` | HTTP/HTTPS 代理地址 |
| `auto_retry` | 失败自动重试开关 |
| `webdav_url` / `username` / `password` | WebDAV 凭证 |
| `aliyun_refresh_token` / `aliyun_drive_id` | 阿里云盘凭证 |
| `quark_cookie` | 夸克网盘 Cookie |
| `iflytek_url` / `iflytek_token` | 讯飞网盘凭证 |

## 断点续传原理

- 下载时创建 `<文件名>.part` 临时文件与 `<文件名>.download.json` 状态文件
- 状态文件记录每个分段区间是否完成, 供下次启动恢复
- 全部完成后自动合并为最终文件并清理临时文件

## 项目结构

```
网盘优化和直链传输/
├── main.py              # 程序入口 (交互式 / 一次性命令 / Web 界面)
├── config.json          # 配置文件
├── requirements.txt     # 依赖
└── netdisk/
    ├── api.py           # 百度网盘 API 客户端
    ├── credentials.py   # 粘贴 Cookie / BDUSS 的解析与校验
    ├── browser_login.py # 官方网页登录 (Playwright 隔离会话)
    ├── downloader.py    # 直链多线程下载器 (分段/续传/重试/限速/统计)
    ├── cli.py           # 交互式命令行界面
    ├── webui.py         # Web 管理界面 (Flask + 内嵌前端)
    ├── config.py        # 配置读写
    ├── history.py       # 下载历史记录
    ├── providers/       # 网盘提供者插件 (统一接口)
    │   ├── base.py      #   NetdiskProvider 抽象基类
    │   ├── baidu/webdav/aliyun/quark/iflytek ...
    └── __init__.py
```

## 技术路线

| 环节 | 方案 |
| --- | --- |
| 认证 | 多网盘 Provider 插件 (BDUSS / OAuth / Basic / Cookie 等) |
| 列目录 | 各提供者实现统一 `list_files` 接口 |
| 直链解析 | 统一 `get_direct_link` 接口, 返回支持 Range 的直链 |
| 传输优化 | 多线程 `Range` 分段 + 断点续传 + 指数退避重试 + 限速 |

## 免责声明

本项目仅供技术学习与研究。使用本项目产生的一切后果由使用者自行承担, 请合理、合法地使用。


## 下载故障定位

- 已登录但返回 `dest idc equal src idc`：旧代码误用了 `superfile2` 下载接口，现已改为 `file?method=download`。重启服务后生效。
- 分享文件可列出但未返回 `dlink`：当前分享接口不提供可用直链，请先在百度官方页面转存到自己的网盘，再在工具「文件」页下载。重新登录不一定能解决。
- `SSL: UNEXPECTED_EOF_WHILE_READING`：连接百度 CDN 时 TLS 被提前中断。程序会有限重试，但仍可能因网络或服务端问题失败；此时请用百度官方网页或客户端验证同一文件是否可下载。

配置示例见 `config.example.json`；实际 `config.json` 包含登录凭证，仅在本机保存，不应提交到仓库。
