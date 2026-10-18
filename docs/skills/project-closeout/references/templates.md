# 收束模板（Templates）

复制后按项目替换占位符 `<...>`。保持精简，别把模板全部小节都强行保留——没有的内容删掉。

## 1. AGENTS.md / Agent.md 骨架

```markdown
# <项目名> 协作手册（给改代码的 AI / 未来的自己）

> 这是操作手册，不是设计文档。改代码前先读本页；约定以 `<源码目录>/` 为准，不凭记忆。

## 1. 这是什么
<一句话目标>。技术栈：<语言/框架/存储/前端/集成>。

### 首次环境准备
- 依赖安装、必要外部服务、环境变量（见参数键字典 / .env.example）。

## 2. 目录职责
| 路径 | 职责 | 关键入口 |
| --- | --- | --- |
| `<dir>/` | <管什么> | `<file>: <class/func>` |

## 3. 常用命令
- 测试：`<cmd>`（当前 <N> 项全绿）
- 启动后端：`<cmd>`（端口 <port>）
- 启动前端：`<cmd>`（端口 <port>）
- 构建：`<cmd>`
- CLI：`<cmd> <subcommands>`
- 外部集成：`<MCP/worker/...> 起法与传输方式`

## 4. 架构与核心约定
- 主链路：<节点/状态机序列>。
- 关键不变量：<如：证据=原文切片且偏移由程序计算；幂等键；状态三态 unknown 不等于通过>。
- 身份/租户边界：<文档身份/版本键/线程身份；org_id 预留情况>。
- 数据红线：<真实数据、密钥、PII 不入库；敏感字段加密方式>。

## 5. 改代码规则
1. 改完全量测试必须全绿（基线 <N>）；新增功能同步加测试。
2. 不把真实数据/PII 写进仓库、文档、日志。
3. 不破坏 <开关名> 的默认语义与数据访问单点边界。
4. <框架> 自定义类型/迁移的注意事项。
5. 配置键统一在 <config 文件> 装配，新增键同步参数键字典与 .env.example。

## 6. 评测与安全现状
- 指标：<如 F1、P50/P95、成本>，口径与日期。
- 已知防线：<注入校验、鉴权、加密、审计>。

## 7. 已知边界 / 易错点
- <单用户/本地部署、mock 外部服务、未实机验证项、回退路径、慢操作>。
```

## 2. README 代码地图（精简版）

```markdown
## 代码地图
| 层 | 关键文件 / 类 | 职责 | 对外契约 |
| --- | --- | --- | --- |
| 入口 | `main.py`、`app/api.py` | CLI / HTTP | `<路由前缀 / 子命令>` |
| 工作流 | `<workflow>.py:<Graph/节点>` | <编排与人工节点> | `<state 字段 / interrupt 点>` |
| 数据与安全 | `repository.py`、`auth.py`、`<pii>.py` | 持久化/鉴权/加密 | `<表 / 依赖 / 开关>` |
| 能力/评测 | `providers/`、`eval/` | 抽取/外部能力/评测 | `<接口 / 指标>` |
| 前端 | `frontend/src/pages/*.tsx` | 页面 | `↔ <对应 API>` |
| 部署集成 | `Dockerfile`、`<mcp>.py` | 容器/对外协议 | `<端口 / tools 数>` |

端到端调用链：<动作A> → <处理B> → <人工节点C> → <外部动作D>。
```

## 3. README 参数键字典（完整版）

```markdown
## 配置参考（参数键字典）
| 键 | 用途 | 默认值 | 出处 | 必填 |
| --- | --- | --- | --- | --- |
| `<SERVICE>_ENABLED` | 总开关 | `0` | `src/config.py` | 启用时必填 |
| `<SERVICE>_API_KEY` | 密钥（不入库） | 空 | `.env` / config | 启用时是 |
| `<SERVICE>_TIMEOUT` | 超时秒数 | `60` | config.py | 否 |
| `<DATA>_PATH` | 数据文件路径 | `data/x.db` | config.py | 否 |
```
> 规则：默认值逐个与配置源码比对；派生/空值如实写；"必填"写明触发条件（如"开模型/开鉴权时必填"）。

## 4. 界面演示占位

```markdown
## 界面演示
> 截图/演示视频由维护者补充；素材须使用合成或脱敏数据，禁止真实用户数据。

<!-- 核心页大图：替换 docs/screenshots/03-<core>.png -->
![<核心页>](docs/screenshots/03-<core>.png)

| <页面 A> | <页面 B> |
| --- | --- |
| ![](docs/screenshots/01-<a>.png) | ![](docs/screenshots/02-<b>.png) |

<!-- 演示视频：补充 docs/video/demo.gif 后取消注释 -->
<!-- ![demo](docs/video/demo.gif) -->
```
路径约定：截图 `docs/screenshots/NN-<page>.png`；视频/GIF `docs/video/demo.gif`。

## 5. 常用徽章（shields.io，替换 owner/repo/工作流名）

```markdown
[![CI](https://github.com/<owner>/<repo>/actions/workflows/ci.yml/badge.svg)](https://github.com/<owner>/<repo>/actions/workflows/ci.yml)
[![tests: <N> passing](https://img.shields.io/badge/tests-<N>%20passing-2e7d32?style=flat-square)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-<color>?style=flat-square)](LICENSE)
```
徽章数字必须与实跑一致；不为不存在的服务/指标加徽章。
