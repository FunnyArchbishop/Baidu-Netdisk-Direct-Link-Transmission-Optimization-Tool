# 百度网盘直链传输优化工具(Self USE)

基于**直链传输技术**的百度网盘传输优化程序。它绕过客户端限速, 将网盘文件解析为**可直接分段的 HTTP 直链**, 再通过**多线程分段并发下载**、**断点续传**与**失败自动重试**等策略大幅提升传输速度与稳定性。

> **声明**: 本项目仅用于个人学习、数据备份与合法内容下载, 请遵守百度网盘用户协议与相关法律法规。勿用于下载侵权内容。

## 特性

- **BDUSS 会话认证** — 无需 OAuth 复杂流程, 直接使用浏览器 Cookie 中的 BDUSS
- **直链解析** — 通过 PCS `superfile2` 接口获取支持 `Range` 的 CDN 直链
- **多线程分段下载** — 文件切分为多个区间并行请求, 充分利用带宽
- **断点续传** — 中断后保留 `.part` 与状态文件, 再次执行即可续传
- **自动重试** — 单块失败指数退避重试, 提高弱网环境成功率
- **实时进度** — 进度条、百分比、速度实时刷新
- **双模式** — 交互式 REPL 与一次性命令行均可使用

## 安装

需要 Python 3.9+。

```bash
pip install -r requirements.txt
```

## 获取 BDUSS

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

## 交互式命令一览

| 命令 | 说明 |
| --- | --- |
| `login <BDUSS>` | 设置/更新登录凭证 |
| `status` | 查看登录状态与配置 |
| `ls [路径]` | 列出目录内容 |
| `cd <路径>` | 切换目录 (`..` 返回上级) |
| `pwd` | 显示当前目录 |
| `download <文件> [本地目录]` | 直链多线程下载 |
| `set threads <N>` | 设置并发线程数 (1-32) |
| `set dir <本地目录>` | 设置默认下载目录 |
| `exit` / `quit` | 退出 |

## 配置说明

配置保存在 `config.json`:

| 字段 | 说明 |
| --- | --- |
| `bduss` | 登录凭证 (通过 `login` 命令写入) |
| `download_dir` | 默认下载保存目录 |
| `threads` | 并发线程数 (建议 4-8, 过大可能触发风控) |
| `auto_retry` | 失败自动重试开关 |

## 断点续传原理

- 下载时创建 `<文件名>.part` 临时文件与 `<文件名>.download.json` 状态文件
- 状态文件记录每个分段区间是否完成, 供下次启动恢复
- 全部完成后自动合并为最终文件并清理临时文件

## 项目结构

```
网盘优化和直链传输/
├── main.py              # 程序入口 (交互式 + 一次性命令)
├── config.json          # 配置文件
├── requirements.txt     # 依赖
└── netdisk/
    ├── api.py           # 百度网盘 API 客户端 (认证/列目录/直链解析)
    ├── downloader.py    # 直链多线程下载器 (分段/续传/重试/统计)
    ├── cli.py           # 交互式命令行界面
    ├── config.py        # 配置读写
    └── __init__.py
```

## 技术路线

| 环节 | 方案 |
| --- | --- |
| 认证 | BDUSS Cookie + `gettemplatevariable` 换取 `bdstoken`/`uk` |
| 列目录 | `GET /api/list` |
| 直链解析 | `GET /rest/2.0/pcs/superfile2?method=download` (302 定位 CDN 直链) |
| 传输优化 | 多线程 `Range` 分段 + 断点续传 + 指数退避重试 |

## 免责声明

本项目仅供技术学习与研究。使用本项目产生的一切后果由使用者自行承担, 请合理、合法地使用。
