# Colab 影像工作台 · Colab Video Workbench

用一张参考图和一段中文要求，在本机提交 Colab 视频生成任务。也可以导入角色、编排多镜头短片，审片并导出 MP4 和 SRT。

- **快速生成**：上传 PNG/JPEG，填写中文要求，选择横竖屏与 3–12 秒时长。
- **分镜工作台**：导入人物参考图、可选音色和已有片段，调整取段和字幕，逐镜确认。
- **Colab 连接**：通过官方 CLI 查算力、申请 G4、回传镜头与释放运行时。
- **模型网关**：可选接入 OpenAI 兼容 API 或已登录的本机 Codex CLI，用于润色要求。
- **Agent 接入**：复制连接指令给本机 Agent，通过 MCP 读项目、改分镜、提交和查询任务。

A local video workbench with a Windows WebView2 shell and a WSL/Python backend. Uses Google Colab for MiniMax H3 rendering and FFmpeg for local editing and export.

## 快速开始

在 Ubuntu 24.04（Windows 用户使用 WSL2）安装 Git、Python 3.12 和 FFmpeg。启动后可直接播放、确认和导出随附的两段合成示例。

```bash
sudo apt-get install git python3 ffmpeg
git clone https://github.com/qwe12yuh/colab-video-workbench.git
cd colab-video-workbench
python3 workbench/app.py serve
```

打开 [本机工作台](http://127.0.0.1:8765)。桌面安装、授权和角色导入请看 [使用指南](workbench/README.md)。Agent 接入请看 [MCP 指南](workbench/AGENT.md)。

## 连接 Colab 生成

在 Ubuntu 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install-sdk)，再从仓库根目录执行：

```bash
bash workbench/bootstrap.sh
bash workbench/bootstrap.sh auth
bash workbench/colab.sh usage
```

在工作台点「快速生成」，上传参考图、填写要求，确认授权消耗计算单元后提交。也可以运行 `bash run-colab.sh examples/gpu-smoke/project.json s01` 生成示例镜头。

渲染使用 MiniMax H3 Ref2VA 和 Pika H3 Flex，需要可分配的 96 GB 级 G4 GPU。新运行时需准备环境并下载约 135 GiB 权重。计费从你授权的 Colab 账号扣除，模型网关有独立的订阅或 API 计费。准备剪辑和导出时可以用本机已有素材。 [Colab CLI](https://github.com/googlecolab/google-colab-cli) · [Pika H3 Flex](https://github.com/Pika-Labs/Pika-H3-Flex)

## 验证

随仓库提供的 MP4、音轨和 PNG 均由程序生成。以下检查可在本机运行：

```bash
python3 tests/smoke.py
python3 tests/workbench_smoke.py
bash tests/colab-lifecycle.sh
uv run --python 3.12 --with mcp==1.27.0 python tests/workbench_mcp.py
```

测试覆盖镜头审批、18 秒 MP4/SRT 导出、参考图上传、兼容模型网关和 MCP。Colab 生命周期用模拟 CLI 验证。

本项目的代码与合成示例使用 [MIT 许可证](LICENSE)。运行时下载的第三方代码和权重适用各自的许可证。 [MiniMax H3](https://github.com/MiniMax-AI/MiniMax-H3) · [Pika H3 Flex](https://github.com/Pika-Labs/Pika-H3-Flex)
