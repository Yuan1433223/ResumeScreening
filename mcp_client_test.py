# -*- coding: utf-8 -*-
"""MCP 连通性测试：以官方 MCP 客户端连接本机 mcp_server（stdio）。

验证三要素并跑通一条真实链路：
  list_tools → create_batch(mock) → get_batch_task 轮询 → get_review → approve
  + read_resource + get_prompt

用法：python mcp_client_test.py [--server F:/ResumeScreening/mcp_server.py]
（后端 8017 需在运行）
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main(server_cmd: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[server_cmd],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 1. 协议层握手
            init = await session.initialize()
            si = getattr(init, "serverInfo", None)
            name = getattr(si, "name", None) if si is not None else None
            if name is None:
                name = str(si) if si else "unknown"
            proto = getattr(init, "protocolVersion", "?")
            print(f"[init] server={name} protocol={proto}")

            # 2. tools 清单
            tools = await session.list_tools()
            print(f"[tools] {len(tools.tools)} 个：")
            for t in tools.tools:
                print(f"  - {t.name}")

            # 3. resources 清单
            res = await session.list_resources()
            print(f"[resources] {len(res.resources)} 个：")
            for r in res.resources:
                print(f"  - {r.uri}")

            # 4. prompts 清单
            prompts = await session.list_prompts()
            print(f"[prompts] {len(prompts.prompts)} 个：")
            for p in prompts.prompts:
                print(f"  - {p.name}")

            # 5. 工具调用链路：批次 → 复核 → 审批
            batch = await session.call_tool(
                "create_batch", {"limit": 1, "extractor": "mock"})
            task = json.loads(batch.content[0].text)
            print(f"[call] create_batch → task={task['task_id']} "
                  f"batch={task['batch_id']}")

            import time
            for _ in range(120):
                r = await session.call_tool(
                    "get_batch_task", {"task_id": task["task_id"]})
                cur = json.loads(r.content[0].text)
                if cur["status"] != "running":
                    break
                time.sleep(1)
            print(f"[call] get_batch_task → {cur['done']}/{cur['total']} "
                  f"status={cur['status']}")
            doc_id = cur["rows"][0]["document_id"]
            print(f"        document_id={doc_id}")

            rev = await session.call_tool("get_review", {"document_id": doc_id})
            review = json.loads(rev.content[0].text)
            print(f"[call] get_review → ext={review['ext']} "
                  f"facts={len(review['facts'])} pages={len(review['pages'])}")

            ap = await session.call_tool(
                "approve", {"document_id": doc_id,
                            "decision": "approve", "reason": "MCP 连通性测试",
                            "assessment_id": review["assessment"]["id"],
                            "expected_version": review["assessment"]["job_version"],
                            "request_id": f"mcp_{review['assessment']['id']}"})
            print(f"[call] approve → {json.loads(ap.content[0].text)}")

            # 6. resource 读取
            res_read = await session.read_resource(f"resumes://{doc_id}/review")
            print(f"[resource] resumes://{doc_id}/review → "
                  f"{len(res_read.contents[0].text)} 字符")

            # 7. prompt 获取
            pr = await session.get_prompt("review_resume",
                                          {"document_id": doc_id})
            print(f"[prompt] review_resume → {len(pr.messages[0].content.text)} 字符")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=str(ROOT / "mcp_server.py"))
    args = parser.parse_args()
    asyncio.run(main(args.server))
