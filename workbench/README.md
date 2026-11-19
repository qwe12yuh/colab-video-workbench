# 使用 Colab 影像工作台

## 启动

在 Ubuntu 24.04 安装 Python 3.12、FFmpeg 和 Git，从 [仓库首页](../README.md) 克隆项目。从仓库根目录运行 `python3 workbench/app.py serve`，在浏览器打开 [本机工作台](http://127.0.0.1:8765)。

Windows 桌面窗口需要 WSL2 Ubuntu 24.04、Edge WebView2 Runtime 和 Windows 版 [uv](https://docs.astral.sh/uv/getting-started/installation/)。把仓库克隆在 WSL 文件系统。在 PowerShell 7 进入该仓库的 Windows UNC 路径，再执行：

```powershell
./workbench/install-windows.ps1 -Launch
```

脚本自动识别仓库所在的 WSL 分发和路径，并创建「Colab 影像工作台」桌面快捷方式。也可用 `-Distro` 和 `-LinuxRoot` 显式指定。Ubuntu 端另外安装 uv 以使用 MCP、转写和 Colab CLI。

## 连接 Colab

在 Ubuntu 安装 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install-sdk)，从工作台「连接与首次设置」安装依赖并授权。也可以在 Ubuntu 终端运行：

```bash
bash workbench/bootstrap.sh
bash workbench/bootstrap.sh auth
bash workbench/colab.sh usage
```

在 Google 官方授权页确认账号和范围。凭证存于本机工作台数据目录。授权后点「刷新 Colab」查看余额、消耗速度和运行时数量。发起生成才申请 G4。任务完成会尝试释放自己申请的运行时，释放失败时可在任务页重试。

## 从参考图到成片

1. 在「快速生成」上传一张 PNG/JPEG，写中文要求，再选时长和画幅。可以先保存草稿，也可以直接提交。
2. 做多镜头短片时，在「角色素材 → 导入角色」上传设定图，可选填外观说明并导入 2–15 秒 WAV/MP3 音色。一个镜头可选最多三个角色，音色参考总长最多 15 秒。
3. 在分镜中写动作、运镜、场景和对白。也可以通过「剪辑与字幕」导入已有 MP4、调整取段和字幕。
4. 生成完成后播放检查，可选用本机 CPU 转写检查对白。确认全部镜头后导出 MP4 和 SRT。成片和历史导出都在项目页可下载。

每个项目都可选用「模型网关」。兼容 API 连接填写以 `/v1` 结尾的 HTTPS 或本机回环 HTTP 地址、API Key 和模型 ID。也可启用已登录的官方 Codex CLI；首次登录由 CLI 引导完成。连接后可用三项 AI 功能：快速生成里润色中文要求；时间线的「AI 分镜」把一句创意拆成带景别、运镜、动作、声音和对白的多镜头方案，勾选后追加或替换到时间线；镜头编辑器的「AI 改写」一键改得更有电影感、强化情绪、补全运镜、精简或衔接前后，可撤销。只有点击这些按钮时才会把文字发给当前模型，结果都要保存并逐镜确认后才会生成。图像和所用角色音色随视频任务上传到 Colab。 [Codex CLI 认证](https://developers.openai.com/codex/auth)

## 本机状态与 Agent

项目、任务、模型连接和 Colab 凭证默认存放于 `~/.local/share/colab-video-workbench/`。可用 `COLAB_WORKBENCH_STATE` 指定独立目录。生成的镜头按版本保存在项目内 `work/takes/`，成片写在 `dist/`。桌面窗口关闭后，WSL 后台仍可继续任务。

点「复制给 Agent」，把剪贴板内容发给能在同一台电脑运行终端的 Agent。内容包含当前项目、HTTP API 和 MCP 注册参数。 [接入指南](AGENT.md)

## 角色音色

H3 渲染时会直接生成与口型同步的中文对白。给角色设一段 2–15 秒的参考音色，它就用这个音色念台词。没有录音时，在「角色素材 → AI 合成音色」里选一个预置模型：Qwen3-TTS 音色设计（用文字描述声音）、预置音色（9 个内置声音加语气指令）、克隆录音（1.7B 或更快的 0.6B），或接任意 OpenAI 兼容的 `/v1/audio/speech` 服务。Colab 模型在 L4/T4 小卡上运行，作为独立任务排队，不会和 H3 渲染同时占卡，结束后自动释放。完成后在角色卡里试听候选，点「用这个」并保存；更换音色后，用到该角色对白的镜头需要重新生成。

## 助手

点右上角「助手」打开侧栏。没有连接模型时，它按项目状态给出下一步（配音色、待审片、可导出、释放 GPU 等），每步都有直达按钮。

连接模型网关后，助手是一个会用工具的 Agent（沿用 DeepSeek Harness 的 Agent 循环、一次性审批和操作卡片设计）：遇到问题时先自己查任务状态、读任务日志、读项目分镜、查 Colab 余额、做环境自检，再说明原因。能修的直接处理：释放未确认释放的 GPU、取消任务、按原参数或换抢卡顺序重试；这些操作会先显示审批卡，你批准一次只执行那一次，拒绝后助手会改提其他办法。改分镜、打开某个面板之类的建议以操作卡片给出，点了才执行。Google 授权、安装系统依赖、充值这类需要本人完成的事，助手会说明步骤。每次审批请求和决定都记录在数据目录的 `assistant-audit.jsonl`。
