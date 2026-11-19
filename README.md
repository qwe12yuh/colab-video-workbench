# Colab 影像工作台 · Colab Video Workbench

一个跑在本机的短片工作台：一张参考图加一句中文要求，就能在 Google Colab 上用 MiniMax H3 生成带对白的视频镜头；也可以导入角色、编排多镜头分镜、给角色合成音色，逐镜审片后导出 MP4 和 SRT。

A local short-video workbench for macOS and Ubuntu, with a Windows WebView2 desktop app backed by WSL2. It renders shots with MiniMax H3 on Google Colab GPUs, synthesizes character voices with Qwen3-TTS, and edits and exports locally with FFmpeg. An optional LLM gateway adds an AI director and a tool-using assistant.

## 功能

| 功能 | 说明 |
|---|---|
| 快速生成 | 上传 PNG/JPEG，写中文要求，选横竖屏和 3–12 秒时长，确认算力消耗后提交。 |
| 分镜工作台 | 导入角色设定图和参考音色，写动作、运镜、场景和对白；导入已有片段调整取段与字幕；逐镜确认后导出。 |
| 显卡策略 | 按优先级抢 G4 / H100 / A100，每张卡用 `nvidia-smi` 核验显存，A100 分到 40GB 会自动释放并重抢。 |
| 角色音色 | 预置 Qwen3-TTS 音色设计、内置音色、录音克隆等模型，在 Colab 小卡（L4/T4）上合成，也可接兼容 `/v1/audio/speech` 的服务。 |
| AI 分镜与改写 | 一句创意拆成带景别、运镜、动作、声音和对白的多镜头方案；单个镜头一键改得更有电影感、补全运镜或衔接前后。 |
| 助手 | 不接模型时按项目状态给出下一步；接入模型后是会用工具的 Agent，能自己查日志、定位问题，经你批准后释放 GPU、取消或重试任务。 |
| Agent 接入 | 通过 MCP 或本机 HTTP API，让外部 Agent 读项目、改分镜、提交和查询任务。 |

工作台本身不依赖任何大模型；模型网关只让操作更方便。

## 快速开始

**macOS**（使用 [Homebrew](https://brew.sh)）

```bash
brew install git python@3.12 ffmpeg
git clone https://github.com/qwe12yuh/colab-video-workbench.git
cd colab-video-workbench
open "$(python3.12 workbench/app.py ensure)"
```

**Ubuntu 24.04 / Windows WSL2**

```bash
sudo apt-get install git python3 ffmpeg
git clone https://github.com/qwe12yuh/colab-video-workbench.git
cd colab-video-workbench
python3 workbench/app.py serve
```

打开 [本机工作台](http://127.0.0.1:8765)。随附的两段合成示例可以直接播放、确认和导出，不需要 Colab。

Windows 桌面快捷方式、角色导入和完整操作流程见 [使用指南](workbench/README.md)，外部 Agent 接入见 [MCP 指南](workbench/AGENT.md)。

## 连接 Colab

在运行工作台的环境中安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install-sdk)（macOS 可用 `brew install uv`）。在仓库根目录执行：

```bash
bash workbench/bootstrap.sh
bash workbench/bootstrap.sh auth
bash workbench/colab.sh usage
```

也可以在工作台「连接与首次设置」里一步步完成。授权后，「快速生成」和分镜里的「生成」都会按显卡策略申请运行时，任务结束自动释放。

| 显卡 | 用途 | 核验标准 |
|---|---|---|
| G4 | H3 渲染（默认首选） | 显存 ≥ 90 GB |
| H100 | H3 渲染 | 80 GB |
| A100 | H3 渲染 | 只接受 80 GB，40 GB 自动释放 |
| L4 | 音色合成（默认） | 24 GB |
| T4 | 音色合成（备选） | 16 GB |

- H3 渲染 6 秒镜头显存峰值约 74 GiB，80 GB 卡上单镜建议不超过 7 秒。新运行时首次要准备环境并下载约 135 GiB 权重。
- 在工作台右上角的显卡标签里勾选、排序候选卡，设置轮数和间隔。命令行用 `VIDEO_COLAB_GPUS=G4,H100,A100 bash run-colab.sh examples/gpu-smoke/project.json s01`，MCP 的 `submit_task` 用 `gpus` 参数。
- 任务排队执行，同一时间只占一张卡，音色合成不会和 H3 渲染同时运行。
- 计算单元从你授权的 Colab 账号扣除；模型网关的 API 或订阅另行计费。

## 角色音色

H3 会直接生成与口型同步的中文对白，角色有一段 2–15 秒参考音色时就用这个声音念台词。没有录音时，在「角色素材 → AI 合成音色」里选模型：

| 预置 | 模型 | 适合 |
|---|---|---|
| 音色设计 | Qwen3-TTS 1.7B VoiceDesign | 用一句话描述声音，例如“三十岁女性，低沉温柔，语速偏慢” |
| 内置音色 | Qwen3-TTS 1.7B / 0.6B CustomVoice | 9 个内置声音加语气指令 |
| 录音克隆 | Qwen3-TTS 1.7B / 0.6B Base | 有一段录音和对应文字时复刻声音 |
| 外部服务 | 任意 OpenAI 兼容 `/v1/audio/speech` | 已有 TTS 服务或 API |

在新的 L4 运行时上首次合成约 2 分钟（含安装和下载权重）。候选音色出现在角色卡里，试听后点「用这个」并保存；换了音色的角色，相关镜头需要重新生成。

## 模型网关与助手

在「模型网关」里填写 OpenAI 兼容 API（以 `/v1` 结尾的地址、API Key、模型 ID），或启用已登录的本机 [Codex CLI](https://developers.openai.com/codex/auth)。只有点击 AI 按钮或向助手提问时，才会把文字发给当前模型；生成结果都要保存并逐镜确认后才会提交渲染。

助手的工作方式沿用 DeepSeek Harness 的 Agent 循环、一次性审批和操作卡片设计：

- **直接执行（只读）**：查任务状态、读任务日志、读项目分镜、查 Colab 余额、环境自检。
- **需要批准**：释放 GPU、取消任务、重试任务。每次批准只执行那一次，拒绝后助手会改提别的办法。
- **操作卡片**：改分镜、打开面板、调整抢卡顺序等建议，点了才执行。
- **只给步骤**：Google 授权、安装系统依赖、充值等需要本人完成的事。

每次请求、审批和执行结果都记录在数据目录的 `assistant-audit.jsonl`。

## 目录

```text
workbench/            本机服务、前端和桌面外壳
  app.py              HTTP 服务与 API
  jobs.py             任务队列（生成、音色、审片、导出）
  gpu.py              抢卡与显存核验
  voice*.py           音色模型目录、Colab 合成脚本
  director.py         AI 分镜与改写
  agent.py            助手的工具循环与审批
  gateway.py          模型网关（兼容 API / Codex CLI）
  agent_mcp.py        MCP 服务
  ui/                 前端
render_h3.py          Colab 上的 H3 渲染脚本
setup-h3.sh           Colab 运行时环境准备
run-colab.sh          命令行生成入口
studio.py · qa.py     本机剪辑导出与对白转写
examples/             合成示例项目
tests/                测试
```

项目、任务、模型连接和 Colab 凭证默认存放在 `~/.local/share/colab-video-workbench/`，可用 `COLAB_WORKBENCH_STATE` 指定其他目录。

## 测试

```bash
python3 tests/smoke.py
python3 tests/workbench_smoke.py
python3 tests/gpu_acquire.py
python3 tests/director.py
python3 tests/voice_job.py
python3 tests/agent_loop.py
bash tests/colab-lifecycle.sh
uv run --python 3.12 --with mcp==1.27.0 python tests/workbench_mcp.py
```

覆盖镜头审批、18 秒 MP4/SRT 导出、参考图上传、抢卡与换卡、AI 分镜、音色任务、助手审批流程、模型网关和 MCP。Colab、模型和 TTS 服务都用模拟替身，测试不消耗算力。

## 许可

本项目代码与合成示例采用 [MIT 许可证](LICENSE)，随仓库提供的 MP4、音轨和 PNG 均由程序生成。运行时下载的第三方代码和权重适用各自的许可证：[MiniMax H3](https://github.com/MiniMax-AI/MiniMax-H3) · [Pika H3 Flex](https://github.com/Pika-Labs/Pika-H3-Flex) · [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) · [Colab CLI](https://github.com/googlecolab/google-colab-cli)
