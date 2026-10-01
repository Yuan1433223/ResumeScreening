# -*- coding: utf-8 -*-
"""MCP server：把简历筛选工作台按 MCP 标准（tools/resources/prompts）暴露。

- 复用本机 FastAPI（127.0.0.1:8017）为唯一事实来源：批次后台任务、审批、
  pages 缓存、trace 全部一致；MCP 层是薄代理，不重复业务逻辑。
- 运行：python mcp_server.py            # stdio 传输，供 MCP 客户端/豆包 Agent 连接
- 配置：RESUME_API_BASE 覆盖 API 地址（默认 http://127.0.0.1:8017）
- mcp SDK：2.x（FastMCP 已更名 MCPServer）

三要素：
  tools     ：list_resumes / upload_resume / create_batch / get_batch_task /
              get_review / approve / get_scheduling / run_scheduling
  resources ：resumes://list、resumes://{document_id}/review
  prompts   ：review_resume（按岗位标准评审指定简历）
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

from mcp.server.mcpserver import MCPServer

API_BASE = os.environ.get("RESUME_API_BASE", "http://127.0.0.1:8017")

mcp = MCPServer("resume-screening")


def _call(method: str, path: str, data: Dict[str, Any] | None = None,
          timeout: float = 30) -> Dict[str, Any]:
    req = urllib.request.Request(API_BASE + path, method=method)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"无法连接工作台 API（{API_BASE}）：{e.reason}。请先启动后端："
            "python -m uvicorn app.api:app --port 8017"
        ) from e


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_resumes(limit: int = 20) -> Dict[str, Any]:
    """列出简历库文件（总数 + 文件名/大小/类型）。document_id 由批次结果提供。"""
    return _call("GET", "/api/resumes")


@mcp.tool()
def upload_resume(file_path: str) -> Dict[str, Any]:
    """把本地简历文件（PDF/JPG/PNG/BMP）上传到简历库。"""
    import mimetypes
    from pathlib import Path

    fp = Path(file_path)
    if not fp.is_file():
        raise RuntimeError(f"文件不存在：{file_path}")
    boundary = "----mcp" + os.urandom(8).hex()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{fp.name}\"\r\n".encode("utf-8"),
        f"Content-Type: {mimetypes.guess_type(fp.name)[0] or 'application/octet-stream'}\r\n\r\n".encode(),
        fp.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        API_BASE + "/api/upload", method="POST", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


@mcp.tool()
def create_batch(limit: int = 10, extractor: str = "mock") -> Dict[str, Any]:
    """创建后台抽取批次（mock=规则 / model=云端大模型）。返回 task_id 供轮询。"""
    return _call("POST", "/api/batches", {"limit": limit, "extractor": extractor})


@mcp.tool()
def get_batch_task(task_id: str) -> Dict[str, Any]:
    """查询批次任务进度（done/total/current_file/rows）。"""
    return _call("GET", f"/api/batches/tasks/{task_id}")


@mcp.tool()
def get_review(document_id: str) -> Dict[str, Any]:
    """获取单份简历的复核详情：原始文件类型、识别文本、抽取事实与证据、评估明细。"""
    return _call("GET", f"/api/documents/{document_id}/review")


@mcp.tool()
def approve(document_id: str, decision: str, reason: str = "") -> Dict[str, Any]:
    """提交人工审批：decision ∈ approve / hold / needs_review。"""
    return _call("POST", f"/api/documents/{document_id}/approve",
                 {"decision": decision, "reason": reason})


@mcp.tool()
def get_scheduling() -> Dict[str, Any]:
    """查看当前排期轮次：候选人名单、时段、候补与通知状态。"""
    return _call("GET", "/api/scheduling")


@mcp.tool()
def run_scheduling() -> Dict[str, Any]:
    """触发一轮排期：冻结审批通过的名单 → 6 人争 4 时段 → 模拟建会与通知。"""
    return _call("POST", "/api/scheduling/run")


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------
@mcp.resource("resumes://list")
def resumes_list_resource() -> str:
    """简历库文件列表（JSON）。"""
    return json.dumps(_call("GET", "/api/resumes"), ensure_ascii=False)


@mcp.resource("resumes://{document_id}/review")
def resume_review_resource(document_id: str) -> str:
    """指定简历的复核详情（JSON）：识别文本、事实与证据、评估。"""
    return json.dumps(_call("GET", f"/api/documents/{document_id}/review"),
                      ensure_ascii=False)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
@mcp.prompt()
def review_resume(document_id: str) -> str:
    """按岗位标准评审一份简历：先看事实与证据，再核对硬条件与评分。"""
    return (
        f"请以招聘负责人身份评审简历（document_id={document_id}）。\n"
        "1. 调用 get_review 获取识别文本、抽取事实（含证据页码/引文）与评估明细；\n"
        "2. 逐条核对事实是否有原文证据支持、档位是否合理、是否存在矛盾或待确认项；\n"
        "3. 对照硬条件（钢结构全流程、海外项目、年限等）给出结论：通过/不通过/需人工；\n"
        "4. 需要时调用 approve 提交审批，并说明理由。"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="简历筛选 MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="stdio=本地命令连接；http=StreamableHTTP，供客户端填 URL（默认端口 8018）")
    parser.add_argument("--port", type=int, default=8018,
                        help="http 传输监听端口（默认 8018）")
    args = parser.parse_args()

    if args.transport == "http":
        print(f"MCP HTTP 服务启动：http://127.0.0.1:{args.port}/mcp "
              f"（后端 API 依赖 {API_BASE}）", flush=True)
        mcp.run(transport="streamable-http", host="127.0.0.1",
                port=args.port, streamable_http_path="/mcp")
    else:
        mcp.run(transport="stdio")
