# 让本机 Agent 使用工作台

在工作台点「复制给 Agent」，把剪贴板里的整段指令发给能访问这台电脑的 Agent。它会得到当前项目 ID、可执行的连接检查和 MCP 配置，兼容 stdio MCP 的客户端可以注册这些工具。

在 macOS 或 Ubuntu（含 WSL）可以从仓库根目录手动启动 MCP 进程。它复用正在运行的工作台服务：

```bash
uv run --python 3.12 --with mcp==1.27.0 python workbench/agent_mcp.py
```

| 操作 | MCP 工具 | 本机 API |
|---|---|---|
| 查项目和镜头 | `list_projects`、`read_project` | `GET /api/projects`、`GET /api/project/{id}` |
| 建项目和改分镜 | `create_draft`、`save_storyboard` | `POST /api/new`、`POST /api/project/{id}` |
| 提交任务 | `submit_task` | `POST /api/run` |
| 看任务和成片 | `read_job`、`read_project` | `GET /api/job/{id}`、`GET /api/project/{id}` |
| 查 Colab 算力 | `colab_usage` | `GET /api/usage` |

可以先让 Agent “根据项目中已有的场景，起草两个各六秒的镜头，保存分镜后给我看”。保存时使用当前 `revision`；并发变更后先重读。提交生成时必须明确选中镜头并传 `gpu_authorized: true`，确认镜头时必须传 `reviewed: true`。可用 `gpus` 指定抢卡顺序，例如 `["G4", "H100", "A100"]`，A100 只接受 80 GB。

HTTP API 默认绑定 `127.0.0.1:8765`；写请求需要同源 `Origin`、`X-Workbench: 1` 和 `Content-Type: application/json`。回传的素材和成片路径以该服务地址为根。
