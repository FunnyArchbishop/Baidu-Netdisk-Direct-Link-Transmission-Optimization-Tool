# README_UPDATE — 迭代优化记录

> 本文档记录对「网盘直链传输优化工具」的一次完整迭代优化（版本 1.0.0 → 1.9.0）。
> 覆盖环境重建、核心下载器增强、文件管理、搜索、目录下载、历史记录、JSON 输出、代理、分享链接解析、Web 管理界面、分享下载、下载队列并发控制、主题切换、单文件打包，以及**多网盘通用化的 Provider 插件架构**（内置百度网盘 / WebDAV / 阿里云盘 / 夸克网盘 / 讯飞 五个提供者）等能力。

---

## 目录

1. [Iter 1：重建虚拟环境](#iter-1重建虚拟环境)
2. [Iter 2：下载限速 + ETA 显示](#iter-2下载限速--eta-显示)
3. [Iter 3：文件管理 API](#iter-3文件管理-api)
4. [Iter 4：按名称搜索文件](#iter-4按名称搜索文件)
5. [Iter 5：递归下载目录](#iter-5递归下载目录)
6. [Iter 6：下载历史记录](#iter-6下载历史记录)
7. [Iter 7：JSON 输出模式](#iter-7json-输出模式)
8. [Iter 8：HTTP 代理支持](#iter-8http-代理支持)
9. [Iter 9：分享链接解析](#iter-9分享链接解析)
10. [Iter 10：收尾与测试](#iter-10收尾与测试)
11. [Web UI 管理界面](#web-ui-管理界面)
12. [分享链接浏览 / 下载](#分享链接浏览--下载)
13. [下载队列并发控制](#下载队列并发控制)
14. [暗色 / 亮色主题切换](#暗色--亮色主题切换)
15. [单文件可执行程序打包](#单文件可执行程序打包)
16. [网盘通用化：Provider 插件架构](#网盘通用化provider-插件架构)
17. [新增命令速查](#新增命令速查)
18. [配置字段变更](#配置字段变更)
19. [测试情况](#测试情况)

---

## Iter 1：重建虚拟环境

原项目的 `.venv` 存在**依赖文件损坏**问题：`site-packages` 下 20 个 `.py` 文件（`charset_normalizer/constant.py`、`requests/status_codes.py`、`idna/uts46data.py` 及 `pip` 内嵌 `rich` 系列等）的非 ASCII 内容被破坏，导致 `import requests` 直接报 `SyntaxError`，pip 也无法使用。

处理方式：

- 删除损坏的 `.venv`，用系统 Python 3.13 重新创建：
  ```bash
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  ```
- 依赖安装成功：`requests 2.34.2`、`charset_normalizer 3.5.1`、`idna 3.18`、`urllib3 2.7.0`、`certifi 2026.7.22`。

> 备注：本机 pip 使用清华镜像源（`pypi.tuna.tsinghua.edu.cn`），本次网络可达、安装正常。

---

## Iter 2：下载限速 + ETA 显示

### 下载限速（令牌桶）

`netdisk/downloader.py` 新增线程安全的 `_TokenBucket` 令牌桶限速器：

- `DirectLinkDownloader(..., speed_limit=0)`，`0` 表示不限速。
- 分段下载与整体下载两条路径均在读取分块后调用 `_rate_limit()`，多线程共享同一个桶，实现**全局速率上限**。

### ETA 剩余时间

`netdisk/cli.py` 进度条新增 `ETA mm:ss` / `hh:mm:ss` 显示：

```text
[##############--------------]  47.3%  1.4 GB/3.0 GB  8.2 MB/s  ETA 03:12  下载中...
```

### 配置与命令

- `config.json` 新增 `speed_limit` 字段。
- 新增命令 `set speed <速率>`，支持 `512k` / `1m` / `1048576` 等写法，`0`/`off`/`unlimited` 为不限速。
- `main.py` 新增 `--speed <速率>` 参数。

---

## Iter 3：文件管理 API

`netdisk/api.py` 新增基于 PCS `filemanager` 接口的文件管理能力（统一走 `_file_operate` 封装）：

| 方法 | 说明 |
| --- | --- |
| `mkdir(path)` | 创建目录 |
| `delete(*paths)` | 批量删除 |
| `rename(path, newname)` | 重命名 |
| `move(src, dest_dir)` | 移动 |
| `copy(src, dest_dir)` | 复制 |

同时新增 `_norm_path` 路径规范化辅助函数。

`netdisk/cli.py` 新增交互命令：

- `mkdir <目录>`、`rm <路径> [...]`、`mv <源> <目标目录>`、`cp <源> <目标目录>`、`rename <路径> <新名称>`。

---

## Iter 4：按名称搜索文件

`netdisk/api.py` 新增 `search(keyword, dir_path="/", recursion=True)`，调用 `/api/search` 接口。

`netdisk/cli.py` 新增 `search <关键词>` / `find` 命令，默认在当前目录递归搜索并打印结果路径。

---

## Iter 5：递归下载目录

- `netdisk/api.py` 新增 `walk(dir_path)`：用栈递归遍历目录，返回其中所有文件（不含目录项）。
- `netdisk/cli.py` 的 `download` 命令支持目录：
  - `download /我的资料` 会递归下载该目录下所有文件，并按相对路径映射到本地目录结构。
  - 单文件下载逻辑抽取为 `_download_file()`，目录下载复用同一逻辑，支持 `Ctrl+C` 中途取消整个批次。

---

## Iter 6：下载历史记录

新增模块 `netdisk/history.py`：

- `HistoryRecord`：记录网盘路径、文件名、大小、本地路径、状态（`done`/`failed`/`cancelled`）、速度与错误信息、时间。
- `append_record()` / `read_records()`：以 **JSON Lines** 追加式写入 `download_history.jsonl`，失败静默容错。

`netdisk/cli.py`：

- `_download_file()` 每次下载结束自动记录历史。
- 新增 `history` / `log` 命令查看最近记录。

`main.py` 一次性下载同样写入历史。

---

## Iter 7：JSON 输出模式

`main.py` 新增 `--json` 参数，便于脚本调用：

- `--list --json`：输出文件对象数组（`NetdiskFile.to_dict()`）。
- `--download --json`：输出结果对象，`{"name","path","dest","size","done","error"}`，且**抑制进度条**避免污染标准输出。
- 出错时也输出 `{"error": "..."}`，便于脚本判断。

`netdisk/api.py` 的 `NetdiskFile` 新增 `to_dict()` 序列化方法。

---

## Iter 8：HTTP 代理支持

- `config.json` 新增 `proxy` 字段。
- `BaiduNetdiskAPI(..., proxy=)` 与 `DirectLinkDownloader(..., proxy=)` 均支持代理，传入 `http`/`https` 代理地址。
- 新增命令 `set proxy <URL>`（`none`/`off` 清除代理）与 `main.py --proxy <URL>`。

---

## Iter 9：分享链接解析

`netdisk/api.py` 新增：

- `parse_share_url(url)`：从 `https://pan.baidu.com/s/xxx?pwd=yyy` 提取 `(shorturl, pwd)`。
- `list_share_files(share_url, dir_path="/")`：调用 `share/wxlist` 接口列出分享内容。

`netdisk/cli.py` 新增 `share <链接>` 命令，解析并列出分享文件。

> 说明：当前实现为**浏览分享内容**。从分享链接下载文件（需 `STOKEN` cookie 与 `dlink` 直链）可作为后续迭代方向。

---

## Iter 10：收尾与测试

- `.gitignore` 补充 `__pycache__`、`*.pyc`、`*.part`、`*.download.json`、`download_history.jsonl` 等忽略项。
- 版本号更新为 `1.5.0`。
- 补全并跑通三套测试（见下）。

---

## Web UI 管理界面

新增基于 **Flask** 的网页管理界面（`netdisk/webui.py`），单文件、无外部 CDN 依赖。

启动方式：

```bash
.venv\Scripts\python.exe main.py --web              # 默认 127.0.0.1:5000
.venv\Scripts\python.exe main.py --web --port 8080  # 自定义端口
```

浏览器打开 `http://127.0.0.1:5000` 即可使用。

### 界面能力

- **登录**：页头粘贴 BDUSS 登录，退出登录。
- **文件浏览**：路径跳转、上级目录、刷新、双击目录进入。
- **搜索**：按关键词搜索文件。
- **文件管理**：新建目录、重命名、删除。
- **下载**：多线程直链下载，带实时进度条 / 速度 / 大小；可取消任务；支持限速与代理。
- **历史**：查看下载历史记录。

### 后端接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 返回前端页面 |
| GET | `/api/status` | 登录状态与配置 |
| POST | `/api/login` | 设置并校验 BDUSS |
| POST | `/api/logout` | 退出登录 |
| POST | `/api/config` | 更新线程数/限速/下载目录/代理 |
| GET | `/api/list?path=` | 列出目录 |
| GET | `/api/search?q=` | 搜索文件 |
| POST | `/api/fileop` | 文件操作（mkdir/delete/rename/move/copy） |
| POST | `/api/download` | 发起下载（返回 task_id） |
| GET | `/api/downloads` | 所有下载任务与进度 |
| POST | `/api/cancel/<id>` | 取消任务 |
| GET | `/api/history` | 下载历史 |

> 依赖新增：`flask>=3.0.0`（已写入 `requirements.txt`）。

---

## 分享链接浏览 / 下载

`netdisk/api.py` 新增分享下载能力：

- `_share_list_raw(surl, pwd, dir)`：调用 `share/wxlist` 返回原始条目。
- `_strip_surl(surl)`：分享 `verify`/`init` 接口需使用去掉前缀 `1` 的 surl。
- `_resolve_share_meta(surl, pwd)`：先 `share/verify` 校验提取码（去前缀 surl + Referer + 表单提交），再从分享页 HTML 的 `window.yunData` 解析 `shareid` / `share_uk`。
- `get_share_dlink(share_url, path)`：从分享条目拿到 `dlink`，携带 `Referer` 请求得到 CDN 直链，返回 `(url, name, size)`。

Web 界面新增「分享」标签页：粘贴分享链接 → 解析出文件列表 → 点击下载（走同一套下载队列与进度条）。

> **实测结论（真实分享链接验证）**：解析链接与列出文件无需登录；但百度对未登录会话不返回 `dlink` 下载直链，**下载分享文件需要登录 BDUSS**。`verify` 流程、`shareid/share_uk` 解析已按实测修复并验证通过。

---

## 下载队列并发控制

`netdisk/webui.py` 新增并发槽位控制：

- 配置字段 `max_concurrent`（默认 `2`，范围 1–8），可在 `/api/config` 修改。
- 下载任务先进入「排队中」状态，通过 `_acquire_slot()/_release_slot()` 控制同时运行的任务数；排队任务在 `downloads` 面板中以「排队中」显示，有槽位后自动开始。
- 支持排队中取消（任务创建后即标记 `cancel_requested`，拿到槽位后立即取消）。

---

## 暗色 / 亮色主题切换

Web 界面新增主题切换：

- 通过 CSS 变量 `:root`（暗色）与 `[data-theme="light"]`（亮色）两套配色实现。
- 页头「🌙 / ☀️」按钮一键切换，偏好写入 `localStorage`，刷新后保持。

---

## 单文件可执行程序打包

使用 **PyInstaller** 打包为 Windows 单文件可执行程序：

```bash
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --onefile --name pan-opt --paths . --clean main.py
```

产物：`dist/pan-opt.exe`（约 13 MB），双击或命令行运行：

```bash
dist\pan-opt.exe --version
dist\pan-opt.exe --web --port 5000   # 直接运行 Web 界面
```

为适配打包环境，配置与历史文件的落盘位置改为「可写目录」：

- `netdisk/config.py` 新增 `app_dir()`：打包后（`sys.frozen`）指向 exe 所在目录，否则指向项目根目录。
- 因此单文件 exe 会在其**同目录**生成 `config.json` 与 `download_history.jsonl`，避免写入临时解压目录而丢失。

---

## 网盘通用化：Provider 插件架构

将「认证 + 列目录 + 解析直链」抽象为统一接口，使工具不再只支持百度网盘。

### 架构

```
netdisk/providers/
├── __init__.py      # 注册表 + create_provider 工厂
├── base.py          # NetdiskProvider 抽象基类 (统一接口)
└── webdav.py        # WebDAV 提供者 (坚果云/Nextcloud/ownCloud 等)
```

`base.NetdiskProvider` 定义了统一接口：

| 方法 | 是否必须 | 说明 |
| --- | --- | --- |
| `check_login()` | 是 | 校验凭证 |
| `list_files(dir)` | 是 | 列目录，返回 `NetdiskFile` 列表 |
| `get_direct_link(path)` | 是 | 返回支持 Range 的下载直链 |
| `download_headers(path)` | 否 | 下载直链所需额外请求头（如鉴权） |
| `get_file_info` / `walk` | 否 | 通用默认实现 |
| `search` / `mkdir` / `delete` / `rename` / `move` / `copy` | 否 | 未覆盖时抛「不支持」 |
| `list_share_files` / `get_share_dlink` | 否 | 分享能力（仅百度实现） |

下载器 `DirectLinkDownloader` 本就是「直链通用」的，任何提供者只要给出直链即可复用多线程分段、断点续传、限速、MD5 校验等全部能力。

### 内置提供者

| 名称 | 凭证 | 说明 |
| --- | --- | --- |
| `baidu` | BDUSS | 原有全部能力（含搜索/分享/文件管理） |
| `webdav` | 地址 + 用户名 + 密码 | 覆盖所有支持 WebDAV 的服务（坚果云/Nextcloud/ownCloud 等） |
| `aliyun` | Refresh Token | 官方 Open API（OAuth + openFile），支持搜索/文件管理 |
| `quark` | Cookie | 社区逆向接口，稳定性一般，需抓取网页版 Cookie |
| `iflytek` | 服务地址 + Token | 通用 REST 模板，需按具体讯飞服务微调 |

### 如何新增一个网盘

1. 在 `netdisk/providers/` 新建模块，继承 `NetdiskProvider` 并实现三个核心方法。
2. 在 `providers/__init__.py` 注册构造器：`register("名称", builder)`。
3. 在 `AppConfig` 中补充该网盘所需的凭证字段。

### 使用方式

- **CLI**：`set provider webdav` + `set webdav_url/username/password`，随后 `login`、`ls`、`download` 均按当前网盘工作。
- **Web UI**：页头下拉切换「百度网盘 / WebDAV」，动态显示对应登录表单。
- **配置文件**：`config.json` 中 `provider` 字段决定当前网盘。

---

## 新增命令速查

| 命令 | 说明 |
| --- | --- |
| `mkdir <目录>` | 创建目录 |
| `rm <路径> [...]` | 删除文件/目录 |
| `mv <源> <目标目录>` | 移动 |
| `cp <源> <目标目录>` | 复制 |
| `rename <路径> <新名称>` | 重命名 |
| `search <关键词>` | 搜索文件 |
| `share <链接>` | 解析分享链接 |
| `history` | 查看下载历史 |
| `set speed <速率>` | 设置下载限速 |
| `set proxy <URL>` | 设置代理 |
| `set provider <baidu\|webdav>` | 切换网盘类型 |
| `set webdav_url <URL>` | 设置 WebDAV 地址 |
| `set username <用户名>` | 设置 WebDAV 用户名 |
| `set password <密码>` | 设置 WebDAV 密码/应用密码 |

一次性命令新增：

```bash
python main.py --speed 1m            # 设置限速
python main.py --proxy http://127.0.0.1:7890
python main.py --list /我的资料 --json   # JSON 输出
python main.py --download /xx.zip --json
```

---

## 配置字段变更

`config.json` 新增字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `speed_limit` | `0` | 下载限速（字节/秒），`0` 不限速 |
| `proxy` | `""` | HTTP/HTTPS 代理地址，空串不使用 |
| `max_concurrent` | `2` | Web 界面同时下载的最大任务数（1–8） |
| `provider` | `baidu` | 当前网盘提供者（`baidu` / `webdav` / `aliyun` / `quark` / `iflytek`） |
| `webdav_url` | `""` | WebDAV 服务地址 |
| `username` | `""` | WebDAV 用户名 |
| `password` | `""` | WebDAV 密码 / 应用密码 |
| `aliyun_refresh_token` | `""` | 阿里云盘 Open API refresh_token |
| `aliyun_drive_id` | `""` | 阿里云盘 drive_id（留空自动获取） |
| `quark_cookie` | `""` | 夸克网盘 Cookie |
| `iflytek_url` | `""` | 讯飞网盘服务地址 |
| `iflytek_token` | `""` | 讯飞网盘 Token |

---

## 测试情况

五套测试全部通过：

1. `tests/test_downloader.py` — 真实本地 HTTP 服务器的集成测试（分段下载 / 断点续传 / 空文件）。
2. `tests/test_downloader_stub.py` — 假 `requests` 的核心逻辑测试，共 8 项。
3. `tests/test_api_stub.py` — 假 `requests` 的 API 测试，共 4 项。
4. `tests/test_webdav_provider.py` — 本地 WebDAV 服务器集成测试，共 6 项。
5. `tests/test_providers_extra.py` — 阿里云盘/夸克/讯飞提供者的请求构造测试，共 4 项。

运行方式：

```bash
.venv\Scripts\python.exe tests\test_downloader.py
.venv\Scripts\python.exe tests\test_downloader_stub.py
.venv\Scripts\python.exe tests\test_api_stub.py
.venv\Scripts\python.exe tests\test_webdav_provider.py
.venv\Scripts\python.exe tests\test_providers_extra.py
```

---

## 后续可迭代方向

- 分享链接**直接下载**（解析 `dlink` + `STOKEN`）。
- 下载任务队列 / 后台并行任务管理。
- 上传文件、秒传（`rapidupload`）。
- 更强的限速策略（动态自适应、分时段限速）。
- 交互式 `tab` 补全与历史命令。
