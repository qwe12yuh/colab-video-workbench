"""Exercise the real MCP stdio client, with no GPU or model requests."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def smoke(state):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, COLAB_WORKBENCH_STATE=str(state), COLAB_WORKBENCH_PORT=str(port))
    app = subprocess.Popen([sys.executable, str(ROOT / "workbench/app.py"), "serve", "--port", str(port)], env=env)
    try:
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=.5):
                    break
            except OSError:
                await asyncio.sleep(.1)
        else:
            raise RuntimeError("test server is not ready")

        config = StdioServerParameters(command=sys.executable, args=[str(ROOT / "workbench/agent_mcp.py")], env=env)
        async with stdio_client(config) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {"list_projects", "create_draft", "read_project", "save_storyboard", "submit_task", "read_job"} <= names

                async def call(name, payload):
                    result = await client.call_tool(name, payload)
                    assert not result.isError, result.content
                    return result.structuredContent or json.loads(result.content[0].text)

                data = await call("create_draft", {"title": "验证 Agent 草稿"})
                pid = data["id"]
                draft = await call("read_project", {"project_id": pid})
                draft["manifest"]["shots"][0]["title"] = "Agent 编辑的镜头"
                saved = await call("save_storyboard", {"project_id": pid, "revision": draft["revision"],
                                                       "manifest": draft["manifest"]})
                assert saved["manifest"]["shots"][0]["title"] == "Agent 编辑的镜头"
                denied = await client.call_tool("submit_task", {"project_id": pid, "action": "generate", "shots": ["s01"]})
                assert denied.isError and "authorization" in denied.content[0].text
                async def wait_job(job):
                    for _ in range(150):
                        row = await call("read_job", {"job_id": job["id"]})
                        if row["status"] in {"complete", "failed"}:
                            assert row["status"] == "complete", row
                            return
                        await asyncio.sleep(.1)
                    raise AssertionError(f"Job {job['id']} did not finish")

                await wait_job(await call("submit_task", {"project_id": "demo", "action": "approve",
                                                         "shots": ["s01", "s02"], "reviewed": True}))
                await wait_job(await call("submit_task", {"project_id": "demo", "action": "cut"}))
                print("MCP PASS: protocol handshake, storyboard save, GPU gate, export job.")
    finally:
        app.terminate()
        app.wait(timeout=5)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="workbench-mcp-") as folder:
        asyncio.run(smoke(Path(folder)))
