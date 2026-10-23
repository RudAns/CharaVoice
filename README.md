# CharaVoice

**在自己电脑上运行的角色声音：让任何 Agent 都能“说”和“听”。**

CharaVoice 是一个本地桌面软件，只做两件事：

1. **说**：用你自己的素材训练（或用一段 3-10 秒录音零样本克隆）角色声音，把文字合成为语音，支持中文、英文、日语。
2. **听**：实时听麦克风，自动断句，转成文字并识别语种。

它不包含大语言模型。Claude Code、Open-LLM-VTuber 或你自己写的程序，都可以通过本地 HTTP 接口、命令行或 MCP 工具调用它。语音默认由 CharaVoice 在本机播放，界面中央的圆环会随真实音量起伏，显示待机、聆听、合成、说话四种状态。

> 当前进度：开发中（第一阶段里程碑：引擎、说话接口、命令行、MCP、Tauri 界面与状态圆环已可用）。

## 功能

| 能力 | 说明 |
| --- | --- |
| 零样本声音 | 上传 3-10 秒参考录音即可使用，参考文本由 SenseVoice 自动识别 |
| 三语合成 | 中 / 英 / 日，按文字自动判断语种，也可手动指定 |
| 本地播放 + 状态圆环 | Agent 调用时由 CharaVoice 播放，圆环跟随实际输出音量 |
| 实时听写 | Silero VAD 断句 + SenseVoice 识别（CPU），输出文字和语种 |
| 角色形象 | 每个声音可设置 GIF / APNG / 动态 WebP / WebM / MP4 形象，可按待机、聆听、说话分别指定 |
| 三种接入方式 | OpenAI 兼容 HTTP、`charavoice` 命令行、MCP 服务器（stdio） |
| 使用边界 | 新建声音必须确认“本人声音 / 已获授权的声音”，接口输出的音频标注为 AI 合成 |

## 系统要求

- Windows 10/11 x64
- NVIDIA 显卡（推荐 8GB 显存以上；推理约占 3-4GB）
- 约 20GB 磁盘空间（引擎整合包约 11GB）
- 界面开发模式需要 Node.js LTS、pnpm 和 Rust 工具链

## 安装

在 cmd 里进入本仓库目录后运行：

```bat
setup.bat
```

它会依次完成：创建 Python 3.11 虚拟环境并安装后端；下载听写模型（Silero VAD、SenseVoice）；下载 GPT-SoVITS 官方 Windows 整合包并解压到 `engines\GPT-SoVITS`；安装界面依赖。下载都支持断点续传。国内网络可先设置镜像：

```bat
set HF_ENDPOINT=https://hf-mirror.com
setup.bat
```

引擎、模型和你的声音数据分别放在 `engines\`、`models\`、`data\` 下，这三个目录不进入版本库。

## 启动

```bat
start.bat
```

打开 CharaVoice 窗口，窗口会自动启动后端和声音引擎，关闭窗口时一并退出。只需要给 Agent 用、不需要窗口时：

```bat
start-backend.bat
```

后端只监听 `127.0.0.1:9870`。每次启动都会生成新的访问口令，写在 `data\runtime.json`；命令行和 MCP 服务器会自动读取它。

## 接入 Agent

### MCP（以 Claude Code 为例）

```bat
claude mcp add charavoice -- "<CharaVoice 目录>\.venv\Scripts\charavoice-mcp.exe"
```

或在项目的 `.mcp.json` 里加入：

```json
{
  "mcpServers": {
    "charavoice": {
      "command": "<CharaVoice 目录>\\.venv\\Scripts\\charavoice-mcp.exe",
      "args": []
    }
  }
}
```

提供三个工具：

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `speak` | `text`，可选 `voice`、`language`、`wait` | 用角色声音在本机说出来 |
| `list_voices` | 无 | 列出可用声音 |
| `listen` | 可选 `timeout` | 听用户说一句话，返回文字和语种 |

### 命令行

```bat
.venv\Scripts\charavoice.exe speak "你好，我们开始吧。" --voice <声音id>
.venv\Scripts\charavoice.exe speak "Save this instead." --out hello.wav --no-play
.venv\Scripts\charavoice.exe listen --timeout 15
.venv\Scripts\charavoice.exe voices
```

后端没运行时，命令会立即报错并以退出码 2 结束，不会卡住。

### HTTP（兼容 OpenAI 格式）

所有 `/v1` 接口都需要 `Authorization: Bearer <访问口令>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/audio/speech` | 合成。`model` 或 `voice` 填声音 id；可选 `language`、`response_format`（wav / mp3 / opus / aac / flac / pcm）、`stream`、`play`（false = 只返回音频不播放） |
| POST | `/v1/audio/transcriptions` | 上传音频转文字（multipart，`file`），返回 `text` 与 `language` |
| GET | `/v1/voices` | 声音列表 |
| POST | `/v1/speak` | 便捷接口：播放并返回 JSON，`wait=true` 等播完 |
| POST | `/v1/listen` | 听一句话并返回文字 |
| WS | `/v1/listen?token=…` | 实时听写事件：`speech_start`、`speech_end`、`transcript`（含 `text`、`language`） |
| WS | `/v1/events?token=…` | 界面用的状态与音量推送 |
| POST | `/v1/audio/stop` | 停止当前播放 |

用 OpenAI Python SDK 调用：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9870/v1", api_key="<访问口令>")
client.audio.speech.create(model="<声音id>", voice="<声音id>", input="こんにちは！").write_to_file("hello.wav")
print(client.audio.transcriptions.create(model="sensevoice", file=open("hello.wav", "rb")).text)
```

引擎未启动或正在启动时，接口返回 503 和明确的中文说明；合成超过 90 秒没有输出会自动重启引擎并报错，不会让调用方一直等待。完整接口文档见运行时的 `http://127.0.0.1:9870/docs`。

## 目录结构

```
backend/charavoice/   Python 后端（FastAPI、引擎管理、播放、听写、CLI、MCP）
app/                  Tauri v2 界面（Vite + Preact + TypeScript）
scripts/setup.py      安装脚本
engines/ models/ data/  本地大文件与用户数据（不进版本库）
```

## 使用边界

- 只能用于**本人的声音**或**已获得本人明确授权的声音**。新建声音时必须选择其一并确认，确认时间写入该声音的项目文件。
- 模仿他人声音前，请告知被模仿的人并取得其单独同意。
- 不得用于冒充他人、诈骗、诽谤或其他违法用途。接口生成的音频会写入“AI 合成”标识。

## 许可证

本仓库代码以 [MIT](LICENSE) 许可发布。CharaVoice 不包含、也不分发任何第三方模型权重或引擎文件，它们由 `setup.bat` 从官方地址下载，各自遵循原许可：

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)（代码与预训练权重） | 语音合成与训练引擎 | MIT |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 语音识别与 VAD 推理 | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | 语音活动检测 | MIT |
| [SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice)（sherpa-onnx 转换版） | 中英日韩粤语音识别 | FunASR Model Open Source License v1.1：模型来自阿里巴巴通义实验室 FunAudioLLM / FunASR 项目，使用时需保留来源与模型名称 |
| [Tauri](https://tauri.app/) | 桌面外壳 | MIT / Apache-2.0 |
| [FastAPI](https://fastapi.tiangolo.com/)、[Preact](https://preactjs.com/) | 后端框架、界面框架 | MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MCP 服务器 | MIT |

听写模型可以在设置里切换为 Whisper（sherpa-onnx 转换版，MIT）。
