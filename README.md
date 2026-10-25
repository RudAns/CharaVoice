# CharaVoice

**在自己电脑上运行的角色声音：让任何 Agent 都能“说”和“听”。**

CharaVoice 是一个本地桌面软件，只做两件事：

1. **说**：用一段 3-10 秒录音零样本克隆角色声音、用文字描述设计一个新声音，或者用自己的素材训练，合成中文、英文、日语语音。
2. **听**：实时听麦克风，自动断句，转成文字并识别语种；也可以在任何输入框里按快捷键直接听写。

它不包含大语言模型。Claude Code、Cursor、Open-LLM-VTuber 或你自己写的程序，都可以通过本地 HTTP 接口（OpenAI 兼容）、命令行或 MCP 工具调用它，而且每个 Agent 可以有自己的声音。语音默认由 CharaVoice 在本机播放，界面中央的光环和能量球随真实音量起伏，显示待机、聆听、合成、说话四种状态；也可以缩成一个置顶的桌面伴随小窗。

技术实现说明见 [Docs/Implementation.html](Docs/Implementation.html)（下载后用浏览器打开）。如需使用，可以把工程代码和这份 HTML 一起交给 AI，让它帮忙接入自己的项目。

## 功能

| 能力 | 说明 |
| --- | --- |
| 零样本声音 | 上传 3-10 秒参考录音即可使用，参考文本自动识别；可选 GPT-SoVITS V5 turbo 或 OmniVoice 两种零样本引擎 |
| 文字设计声音 | 选性别、年龄、音调、耳语、口音，一次生成 3 个候选，试听后选一个保存为新声音（OmniVoice，中英文） |
| 训练声音 | 素材 ≥1 分钟即可训练（建议 10 分钟），训练向导含开训前检查、按显存选参数、进度与日志、中断后继续、训完自动试听 |
| 素材流水线 | 导入音视频或录制电脑播放的声音 → 波形裁剪 → 人声分离（BS-Roformer）→ 切成 2-10 秒单句 → 自动转写 → 质量分、声纹相似度、语调起伏 → 审阅 → 导出训练列表 |
| 情绪参考 | 每个声音可挂多段带情绪标签的参考录音，调用时传 `emotion`（happy、sad、gentle 等）切换 |
| 声线调节 | 音高（保持音色）、年龄感（共振峰）、明亮度、气声四个滑块和“更甜 / 更低沉 / 更年轻 / 更成熟”预设；可存为声音默认值，也可每次调用临时指定 |
| 自然度 | 按标点决定停顿长短并带随机变化、响度统一；可选呼吸（取自该声音自己的素材）、动态与去齿音、轻微房间感 |
| 流式合成 | 边合成边播放 / 边返回，首包最快约 0.2 秒；客户端中途断开会立即停止合成 |
| 三语合成 | 中 / 英 / 日，按文字自动判断语种，也可手动指定 |
| 实时听写 | Silero VAD 断句 + SenseVoice 识别（CPU），说话过程中每 0.4 秒更新中间结果 |
| 全局听写 | 在任何程序的输入框里按 Ctrl+Alt+Space（被占用时自动换成 Ctrl+Shift+Space 等），说完约 0.7 秒后文字直接输入到光标处 |
| 按 Agent 分配声音 | 自动识别 MCP 客户端名字（如 claude-code），也可用请求头或 `--agent` 指定；在界面里给每个 Agent 选声音和默认情绪 |
| 接入方式 | OpenAI 兼容 HTTP、`charavoice` 命令行、MCP（stdio 与 Streamable HTTP）、`/.well-known/charavoice.json` 能力清单 |
| 模型管理 | 安装包不带任何模型，首次运行在“模型”页按需下载，显示用途、大小、许可和进度，可断点续传 |
| 合规 | 新建声音必须确认“本人声音 / 已获授权的声音”（文字设计的声音标记为合成声音）并记录确认时间；接口生成的音频写入“AI 合成”元数据并嵌入 AudioSeal 水印（可在设置里关闭） |

## 实测性能

测试机：RTX 3060 12GB、i7-13700KF、Windows 11。显存为使用中的峰值，已扣除其他程序占用。

| 引擎 / 声音 | 首包延迟（逐句流式，英 / 中） | 首包延迟（低延迟流式，英 / 中） | 实时率 RTF（英 / 中） | 显存 |
| --- | --- | --- | --- | --- |
| GPT-SoVITS v2Pro，训练后的声音 | 0.61 / 0.35 秒 | 0.18 / 0.20 秒 | 0.29 / 0.21 | 约 3.2 GB |
| GPT-SoVITS V5 turbo，零样本 | 0.66 / 0.43 秒 | 0.55 / 0.50 秒 | 0.38 / 0.28 | 约 3.8 GB |
| OmniVoice，零样本（16 步 / 快速 8 步） | 1.26 / 1.16 秒 | 0.65 / 0.60 秒 | 0.35 / 0.38 | 约 4.7 GB |

| 指标 | 结果 |
| --- | --- |
| 零样本声纹相似度（英文，与真人参考比，CAM++） | GPT-SoVITS v2Pro 0.48、V5 turbo 0.78、OmniVoice 0.86 |
| 训练 | 10 分钟 LJSpeech 素材，从开始到可用共 13 分钟；相似度 0.49 → 0.86 |
| 全局听写 | 说完到文字出现 0.70-0.74 秒（VAD 判定 0.5 秒 + 识别 0.08-0.22 秒 + 输入 0.02 秒） |
| 文件转写 | 52 秒中英混合音频 1.8 秒转完（CPU，RTF 0.035） |
| 声线调节 | 每秒音频 4-20 毫秒处理；流式首块额外延迟 5-18 毫秒；±3 半音时与原声相似度 0.85-0.86 |
| 自然度 | 按标点停顿与响度统一不增加首包延迟（194 对 189 毫秒）；停顿时长的变异系数从 0.31 提高到 0.44 |

首包延迟是引擎预热后的中位数；开机后第一次调用和切换到另一个声音时需要多 1-2 秒加载模型。

## 安装

### 安装包（推荐）

运行 `CharaVoice_<版本>_x64-setup.exe`（装到当前用户目录，不需要管理员权限）。第一次打开时界面顶部会提示缺少的组件，进入“模型”页按需安装：

| 组件 | 用途 | 下载大小 |
| --- | --- | --- |
| Silero VAD、SenseVoice、3D-Speaker CAM++ | 听写、转写、声纹（必需） | 约 270 MB |
| GPT-SoVITS 官方整合包 | 语音合成与训练（必需，需 NVIDIA 显卡） | 约 10.9 GB，解压后约 15 GB |
| OmniVoice | 可选的零样本引擎和文字设计声音 | 约 5 GB，首次启动再下载约 3 GB 权重 |

模型、引擎和声音数据都放在 `%LOCALAPPDATA%\io.github.rudans.charavoice`，卸载程序不会删除它们。国内网络可以在该目录的 `data\settings.json` 里加上 `"download": {"hf_endpoint": "https://hf-mirror.com"}`。

### 从源码运行

需要 Python 3.11；界面还需要 Node.js LTS、pnpm 和 Rust 工具链。在 cmd 里进入本仓库目录：

```bat
setup.bat
start.bat
```

`setup.bat` 会创建虚拟环境、下载听写模型和 GPT-SoVITS 整合包（`setup.bat --with-omnivoice` 另装 OmniVoice）；`start.bat` 打开窗口，窗口会自动启动后端和声音引擎，关闭时一并退出。只给 Agent 用、不需要窗口时运行 `start-backend.bat`。源码模式下数据放在仓库里的 `engines\`、`models\`、`data\`，这三个目录不进入版本库。

自己打安装包：

```bat
scripts\build-installer.bat
```

## 使用流程

1. **模型**：首次使用先安装必需组件。
2. **声音库 → 新建**：用录音克隆（选择“本人声音”或“已获授权的声音”并确认，上传 3-10 秒清晰录音），或用文字设计一个合成声音。马上就能说话。
3. **声线调节**：在声音详情里拖动滑块或点预设，点“试听”立刻听到效果，满意后保存为默认。
4. **素材**：导入音视频，或点“录制电脑声音”录下正在播放的内容；在裁剪编辑器里剪掉别人的声音和音乐；处理完在审阅区试听、改字、按阈值删掉低质量或声纹不像的片段，把语调起伏大又清晰的片段设为参考。
5. **训练**：检查通过后开始训练，训完自动切换到新权重并生成中英日试听。
6. **接入 Agent**：见下文，或界面里的“接入 Agent”页（命令会自动填好本机路径和口令）。

## 接入 Agent

后端只监听 `127.0.0.1:9870`。所有 `/v1` 接口和 `/mcp` 都需要 `Authorization: Bearer <口令>`：

- **Agent 口令**：长期有效，写进 Agent 配置用，在“接入 Agent”页复制或重新生成。
- **运行时口令**：每次启动重新生成，写在 `data\runtime.json`，命令行和 stdio 版 MCP 会自动读取。

### MCP

Streamable HTTP（CharaVoice 开着就能用，不需要另起进程）：

```bat
claude mcp add --transport http charavoice http://127.0.0.1:9870/mcp --header "Authorization: Bearer <Agent 口令>"
```

stdio（由 Agent 启动一个小进程转发；安装版用 `charavoice-backend.exe mcp`，源码版用 `.venv\Scripts\charavoice-mcp.exe`）：

```bat
claude mcp add charavoice -- "<CharaVoice 安装目录>\backend\charavoice-backend.exe" mcp
```

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `speak` | `text`，可选 `voice`、`language`、`wait`、`low_latency`、`emotion`、`pitch`、`formant`、`brightness`、`breath`、`preset`、`naturalness` | 用角色声音在本机说出来；不传 `voice` 时用界面里给这个 Agent 分配的声音 |
| `list_voices` | 无 | 列出可用声音及各自支持的情绪 |
| `listen` | 可选 `timeout` | 听用户说一句话，返回文字和语种 |
| `transcribe` | `path`，可选 `language` | 把本机的音视频文件转成文字，含分段时间 |

### 命令行

```bat
charavoice-backend.exe speak "你好，我们开始吧。" --voice <声音id>
charavoice-backend.exe speak "我用自己的声音说话" --agent my-agent --preset sweet
charavoice-backend.exe speak "Save this instead." --out hello.wav --no-play
charavoice-backend.exe listen --timeout 15
charavoice-backend.exe voices
```

源码版把 `charavoice-backend.exe` 换成 `.venv\Scripts\charavoice.exe`。后端没运行时命令会立即报错并以退出码 2 结束，不会卡住。

### HTTP（兼容 OpenAI 格式）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/audio/speech` | 合成。`model` 或 `voice` 填声音 id；可选 `language`、`response_format`（wav / mp3 / opus / aac / flac / pcm）、`stream`、`streaming_mode`、`play`（false = 只返回音频）、`emotion`、`pitch`、`formant`、`brightness`、`breath`、`preset`、`naturalness`、`agent` |
| POST | `/v1/audio/transcriptions` | 上传音频转文字（multipart，`file`），返回 `text` 与 `language` |
| GET | `/v1/voices` | 声音列表 |
| POST | `/v1/speak` | 便捷接口：播放并返回 JSON（含首包延迟），`wait=true` 等播完 |
| POST | `/v1/listen` | 听一句话并返回文字 |
| WS | `/v1/listen?token=…` | 实时听写事件：`speech_start`、`partial`、`speech_end`、`transcript` |
| WS | `/v1/events?token=…` | 状态（idle / listening / thinking / speaking）与实时音量 |
| POST | `/v1/audio/stop` | 停止当前播放 |
| GET | `/v1/agents` | 接入过的 Agent 及其声音绑定 |
| POST | `/v1/audio/watermark/detect` | 检测音频里的 AudioSeal 水印 |
| GET | `/.well-known/charavoice.json` | 能力清单，供 Agent 自动发现 |

请求头 `X-CharaVoice-Agent: <名字>` 表明调用方身份，未指定声音时使用该 Agent 绑定的声音。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9870/v1", api_key="<Agent 口令>",
                default_headers={"X-CharaVoice-Agent": "my-agent"})
client.audio.speech.create(model="<声音id>", voice="<声音id>", input="こんにちは！").write_to_file("hello.wav")
print(client.audio.transcriptions.create(model="sensevoice", file=open("hello.wav", "rb")).text)
```

引擎未启动、正在启动或正在训练时，接口返回 503 和明确的中文说明；合成 90 秒没有输出会自动重启引擎并报错，不会让调用方一直等待。完整接口文档见运行时的 `http://127.0.0.1:9870/docs`。

## 目录结构

```
backend/charavoice/   Python 后端（FastAPI、引擎管理、播放、听写、素材、训练、声线调节、自然度、模型管理、CLI、MCP）
app/                  Tauri v2 界面（Vite + Preact + TypeScript）
Docs/                 技术实现说明
scripts/              源码安装脚本、后端打包与安装包构建脚本
engines/ models/ data/  本地大文件与用户数据（不进版本库）
```

## 使用边界

- 只能用于**本人的声音**、**已获得本人明确授权的声音**，或用文字设计的合成声音。新建声音时必须选择其一并确认，确认时间写入该声音的项目文件；录制电脑声音前会再次确认。
- 模仿他人声音前，请告知被模仿的人并取得其单独同意。
- 不得用于冒充他人、诈骗、诽谤或其他违法用途。接口生成的音频会写入“AI 合成”标识和 AudioSeal 水印（流式输出和本机播放不加水印；经有损压缩后水印仍可检测，但 16 位标识可能读不出）。

## 许可证

本仓库代码以 [MIT](LICENSE) 许可发布。CharaVoice 不包含、也不分发任何第三方模型权重或引擎文件，它们在“模型”页（或 `setup.bat`）里从官方地址下载，各自遵循原许可：

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)（代码与预训练权重） | 语音合成与训练引擎 | MIT |
| UVR5 / BS-Roformer 分离模型（随 GPT-SoVITS 整合包提供） | 人声分离 | 随整合包分发，遵循其各自来源的许可 |
| [OmniVoice](https://github.com/k2-fsa/OmniVoice)（可选） | 零样本引擎、文字设计声音 | 代码 Apache-2.0；权重许可以模型页为准 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 语音识别、VAD、声纹推理 | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | 语音活动检测 | MIT |
| [SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoice)（sherpa-onnx 转换版） | 中英日韩粤语音识别 | FunASR Model Open Source License v1.1：模型来自阿里巴巴通义实验室 FunAudioLLM / FunASR 项目，使用时需保留来源与模型名称 |
| [3D-Speaker](https://github.com/modelscope/3D-Speaker) CAM++（sherpa-onnx 转换版） | 声纹相似度 | Apache-2.0 |
| [AudioSeal](https://github.com/facebookresearch/audioseal) | 音频水印 | MIT（代码与权重） |
| [PyTorch](https://pytorch.org/)（CPU 版） | AudioSeal 推理 | BSD-3-Clause |
| [pyworld](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)（WORLD） | 声线调节的基频检测（DIO / StoneMask） | MIT（WORLD 为修改版 BSD） |
| [SciPy](https://scipy.org/) | 自然度后处理链的滤波、重采样 | BSD-3-Clause |
| [pyloudnorm](https://github.com/csteinmetz1/pyloudnorm) | 响度测量（ITU-R BS.1770） | MIT |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | WASAPI 回环录音 | Apache-2.0（内含 PortAudio，MIT） |
| [Tauri](https://tauri.app/) | 桌面外壳 | MIT / Apache-2.0 |
| [FastAPI](https://fastapi.tiangolo.com/)、[Preact](https://preactjs.com/)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 后端框架、界面框架、MCP 服务器 | MIT |
| [PyInstaller](https://pyinstaller.org/)、[NSIS](https://nsis.sourceforge.io/) | 构建安装包（不随代码分发） | GPL-2.0 含 bootloader 例外（打包产物不受 GPL 约束）、zlib |

听写模型可以在设置里切换为 Whisper（sherpa-onnx 转换版，MIT）。开发测试只使用了公有领域的 [LJSpeech](https://keithito.com/LJ-Speech-Dataset/) 数据集、[CREMA-D](https://github.com/CheyneyComputerScience/CREMA-D)（ODbL / DbCL）片段和程序生成的音频，仓库里不包含任何音频文件。
