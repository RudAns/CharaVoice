# CharaVoice

**在自己电脑上运行的角色声音：让任何 Agent 都能“说”和“听”。**

CharaVoice 是一个本地桌面软件，只做两件事：

1. **说**：用一段 3-10 秒录音零样本克隆角色声音，或者用自己的素材训练，合成中文、英文、日语语音。
2. **听**：实时听麦克风，自动断句，转成文字并识别语种。

它不包含大语言模型。Claude Code、Open-LLM-VTuber 或你自己写的程序，都可以通过本地 HTTP 接口、命令行或 MCP 工具调用它。语音默认由 CharaVoice 在本机播放，界面中央的光环和能量球随真实音量起伏，显示待机、聆听、合成、说话四种状态；也可以缩成一个置顶的桌面伴随小窗。

技术实现说明见 [Docs/Implementation.html](Docs/Implementation.html)（下载后用浏览器打开）。如需使用，可以把工程代码和这份 HTML 一起交给 AI，让它帮忙接入自己的项目。

## 功能

| 能力 | 说明 |
| --- | --- |
| 零样本声音 | 上传 3-10 秒参考录音即可使用，参考文本自动识别 |
| 训练声音 | 素材 ≥1 分钟即可训练（建议 10 分钟），训练向导含开训前检查、按显存选参数、进度与日志、中断后继续、训完自动试听 |
| 素材流水线 | 导入音视频或录制电脑播放的声音 → 波形裁剪 → 人声分离（BS-Roformer）→ 切成 2-10 秒单句 → 自动转写 → 质量分与声纹相似度 → 审阅 → 导出训练列表 |
| 三语合成 | 中 / 英 / 日，按文字自动判断语种，也可手动指定 |
| 实时听写 | Silero VAD 断句 + SenseVoice 识别（CPU），输出文字、语种 |
| 状态展示 | 光环 + 能量球随实际音量变化；可为每个声音上传 GIF / APNG / 动态 WebP / WebM（可透明）/ MP4 形象，并按待机、聆听、说话分别指定 |
| 三种接入方式 | OpenAI 兼容 HTTP、`charavoice` 命令行、MCP 服务器（stdio） |
| 合规 | 新建声音必须确认“本人声音 / 已获授权的声音”并记录确认时间；接口生成的音频写入“AI 合成”元数据并嵌入 AudioSeal 水印（可在设置里关闭） |

## 系统要求

- Windows 10/11 x64，NVIDIA 显卡（推理约占 3-4GB 显存，训练需要约 4GB 以上空闲显存）
- 约 25GB 磁盘空间（引擎整合包约 11GB）
- 界面开发模式需要 Node.js LTS、pnpm 和 Rust 工具链；只用后端不需要

## 安装

在 cmd 里进入本仓库目录后运行：

```bat
setup.bat
```

它会依次完成：创建 Python 3.11 虚拟环境并安装后端（含 CPU 版 PyTorch 与 AudioSeal）；下载听写模型（Silero VAD、SenseVoice）和声纹模型；下载 GPT-SoVITS 官方 Windows 整合包并解压到 `engines\GPT-SoVITS`；安装界面依赖。下载都支持断点续传。国内网络可先设置镜像：

```bat
set HF_ENDPOINT=https://hf-mirror.com
setup.bat
```

引擎、模型和你的声音数据分别放在 `engines\`、`models\`、`data\`，这三个目录不进入版本库。

## 启动

```bat
start.bat
```

打开 CharaVoice 窗口，窗口会自动启动后端和声音引擎，关闭窗口时一并退出。只给 Agent 用、不需要窗口时：

```bat
start-backend.bat
```

后端只监听 `127.0.0.1:9870`，每次启动生成新的访问口令，写在 `data\runtime.json`；命令行和 MCP 服务器会自动读取它。

## 使用流程

1. **声音库 → 新建**：选择“本人声音”或“已获授权的声音”并确认，上传一段 3-10 秒清晰录音，马上就能说话（零样本）。
2. **素材**：导入音视频，或点“录制电脑声音”录下正在播放的内容；在裁剪编辑器里剪掉别人的声音和音乐；处理完在审阅区试听、改字、按阈值删掉低质量或声纹不像的片段。
3. **训练**：检查通过后开始训练，大约 20-30 分钟（10 分钟素材、RTX 3060）。训完自动切换到新权重并生成中英日试听。
4. **接入 Agent**：见下文，或界面里的“接入 Agent”页。

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

后端没运行时命令会立即报错并以退出码 2 结束，不会卡住。

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
| WS | `/v1/events?token=…` | 状态（idle / listening / thinking / speaking）与实时音量 |
| POST | `/v1/audio/stop` | 停止当前播放 |
| POST | `/v1/audio/watermark/detect` | 检测音频里的 AudioSeal 水印 |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9870/v1", api_key="<访问口令>")
client.audio.speech.create(model="<声音id>", voice="<声音id>", input="こんにちは！").write_to_file("hello.wav")
print(client.audio.transcriptions.create(model="sensevoice", file=open("hello.wav", "rb")).text)
```

引擎未启动、正在启动或正在训练时，接口返回 503 和明确的中文说明；合成 90 秒没有输出会自动重启引擎并报错，不会让调用方一直等待。完整接口文档见运行时的 `http://127.0.0.1:9870/docs`。

## 目录结构

```
backend/charavoice/   Python 后端（FastAPI、引擎管理、播放、听写、素材、训练、CLI、MCP）
app/                  Tauri v2 界面（Vite + Preact + TypeScript）
Docs/                 技术实现说明
scripts/setup.py      安装脚本
engines/ models/ data/  本地大文件与用户数据（不进版本库）
```

## 使用边界

- 只能用于**本人的声音**或**已获得本人明确授权的声音**。新建声音时必须选择其一并确认，确认时间写入该声音的项目文件；录制电脑声音前会再次确认。
- 模仿他人声音前，请告知被模仿的人并取得其单独同意。
- 不得用于冒充他人、诈骗、诽谤或其他违法用途。接口生成的音频会写入“AI 合成”标识和 AudioSeal 水印（流式输出和本机播放不加水印；经有损压缩后水印仍可检测，但 16 位标识可能读不出）。

## 许可证

本仓库代码以 [MIT](LICENSE) 许可发布。CharaVoice 不包含、也不分发任何第三方模型权重或引擎文件，它们由 `setup.bat` 从官方地址下载，各自遵循原许可：

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)（代码与预训练权重） | 语音合成与训练引擎 | MIT |
| UVR5 / BS-Roformer 分离模型（随 GPT-SoVITS 整合包提供） | 人声分离 | 随整合包分发，遵循其各自来源的许可 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 语音识别、VAD、声纹推理 | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | 语音活动检测 | MIT |
| [SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice)（sherpa-onnx 转换版） | 中英日韩粤语音识别 | FunASR Model Open Source License v1.1：模型来自阿里巴巴通义实验室 FunAudioLLM / FunASR 项目，使用时需保留来源与模型名称 |
| [3D-Speaker](https://github.com/modelscope/3D-Speaker) CAM++（sherpa-onnx 转换版） | 声纹相似度 | Apache-2.0 |
| [AudioSeal](https://github.com/facebookresearch/audioseal) | 音频水印 | MIT（代码与权重） |
| [PyTorch](https://pytorch.org/)（CPU 版） | AudioSeal 推理 | BSD-3-Clause |
| [OmniVoice](https://github.com/k2-fsa/OmniVoice)（可选，`setup.bat --with-omnivoice` 安装） | 零样本引擎、文字设计声音 | 代码 Apache-2.0；权重许可以模型页为准 |
| [pyworld](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)（WORLD） | 声线调节的基频检测（DIO / StoneMask） | MIT（WORLD 为修改版 BSD） |
| [SciPy](https://scipy.org/) | 自然度后处理链的滤波 | BSD-3-Clause |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | 响度测量（ITU-R BS.1770） | MIT |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | WASAPI 回环录音 | Apache-2.0（内含 PortAudio，MIT） |
| [Tauri](https://tauri.app/) | 桌面外壳 | MIT / Apache-2.0 |
| [FastAPI](https://fastapi.tiangolo.com/)、[Preact](https://preactjs.com/)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 后端框架、界面框架、MCP 服务器 | MIT |

听写模型可以在设置里切换为 Whisper（sherpa-onnx 转换版，MIT）。开发测试只使用了公有领域的 [LJSpeech](https://keithito.com/LJ-Speech-Dataset/) 数据集和程序生成的音频，仓库里不包含任何音频文件。
