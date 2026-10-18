# MCP 薄代理暴露（Thin Proxy Exposure）

把工作台按 MCP 标准暴露给外部 Agent，核心纪律只有一条：**MCP 层不做业务判定，只转发 HTTP；唯一事实源永远是 FastAPI 工作台。**

## 为什么是薄代理

一旦 MCP 层自己写排序/审批/状态机，就和工作台形成双份业务逻辑，两边升级必然漂移。本项目 `mcp_server.py` 全部工具都是 `_call(method, path)` 一行转发到 `http://127.0.0.1:8017`；批次后台任务、审批、pages 缓存、trace 全部与浏览器工作台同一份数据。

## 三要素清单（本项目实测）

server 名 `resume-screening`（mcp SDK 2.x，`MCPServer`）：

- **tools（8）**：`list_resumes` / `upload_resume` / `create_batch` / `get_batch_task` / `get_review` / `approve` / `get_scheduling` / `run_scheduling`。
- **resources（2）**：`resumes://list`、`resumes://{document_id}/review`。
- **prompts（1）**：`review_resume(document_id)`——给外部 Agent 的评审剧本（先取事实证据→核对硬条件/评分→必要时 approve）。

命名约定：tool 用动宾短名、资源 URI 用 `协议://集合/标识`、prompt 是一段可复用的工作指令而非一次调用。

## 传输与配置

- stdio（默认）：`python mcp_server.py`，供 MCP 客户端/豆包 Agent 直接连接。
- HTTP：`python mcp_server.py --transport http --port 8018`，挂在 `http://127.0.0.1:8018/mcp`（StreamableHTTP，供客户端填 URL）。
- 后端地址：环境变量 `RESUME_API_BASE` 覆盖（默认 `http://127.0.0.1:8017`）。后端没起时 `_call` 抛明确错误：提示先 `python -m uvicorn app.api:app --port 8017`。

## 测试形态

没有 pytest 用例，而是根目录连通性脚本 `mcp_client_test.py`（stdio 真实握手）：

1. `initialize()` 拿 serverInfo/protocolVersion；
2. `list_tools` / `list_resources` / `list_prompts` 打印三要素清单；
3. 跑通一条真实链路：`create_batch(mock)` → `get_batch_task` 轮询 → `get_review` → `approve` → `read_resource` → `get_prompt`。

用法：后端 8017 在运行时 `python mcp_client_test.py`。它固化的是"三要素齐全 + 端到端链路通"，不是业务断言。

## 何时该 / 不该暴露

**该暴露**：只读查询（列表、复核详情、排期状态）、有幂等保证的写操作（create_batch、approve——内部已 request_id 幂等）。这些操作经工作台就安全，MCP 转发无额外风险。

**不该暴露**：需要在 MCP 层做业务判定的流程（例如"按分数自动决定 approve"）——那是工作台评分/路由的职责，搬到 MCP 层就是制造第二事实源；危险写操作（purge 清理、删文档、改岗位规则）默认不暴露，等工作台有明确鉴权与审计再议。当前单用户认证 `AUTH_ENABLED` 默认关闭，暴露前应确认网络边界。
