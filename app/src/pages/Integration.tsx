import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "preact/hooks";
import { conn } from "../api";
import { notify, selected } from "../store";

function Code({ title, text }: { title: string; text: string }) {
  return (
    <div class="code">
      <div class="code-head">
        <span>{title}</span>
        <button
          class="small"
          onClick={() => navigator.clipboard.writeText(text).then(() => notify("已复制"), () => notify("复制失败", "error"))}
        >
          复制
        </button>
      </div>
      <pre>{text}</pre>
    </div>
  );
}

export function Integration() {
  const [home, setHome] = useState("");
  const [showToken, setShowToken] = useState(false);
  useEffect(() => {
    invoke<string>("home_path").then((p) => setHome(p.replace(/^\\\\\?\\/, "")));
  }, []);
  const c = conn.value;
  if (!c) return null;
  const voice = selected.value?.id ?? "<声音id>";
  const exe = `${home}\\.venv\\Scripts`;
  const token = showToken ? c.token : "•".repeat(12);

  return (
    <div class="page doc">
      <h2>接入 Agent</h2>
      <p class="lead">
        CharaVoice 不含大语言模型，只负责“说”和“听”。任何 Agent 都可以通过下面三种方式调用。语音默认由 CharaVoice 在本机播放，圆环会同步显示。
      </p>

      <div class="card kv">
        <div>
          <span>本地地址</span>
          <code>{c.base_url}</code>
        </div>
        <div>
          <span>访问口令</span>
          <code>{token}</code>
          <button class="small" onClick={() => setShowToken(!showToken)}>
            {showToken ? "隐藏" : "显示"}
          </button>
          <button class="small" onClick={() => navigator.clipboard.writeText(c.token).then(() => notify("口令已复制"))}>
            复制
          </button>
        </div>
        <p class="muted">口令每次启动都会重新生成，保存在 data\runtime.json，命令行和 MCP 服务器会自动读取。只监听 127.0.0.1，局域网无法访问。</p>
      </div>

      <h3>1. MCP（Claude Code 等）</h3>
      <p>提供 speak(text, voice?, language?)、list_voices()、listen(timeout?) 三个工具。在 cmd 里运行：</p>
      <Code title="cmd" text={`claude mcp add charavoice -- "${exe}\\charavoice-mcp.exe"`} />
      <p>或写入项目的 .mcp.json：</p>
      <Code
        title=".mcp.json"
        text={JSON.stringify({ mcpServers: { charavoice: { command: `${exe}\\charavoice-mcp.exe`, args: [] } } }, null, 2)}
      />

      <h3>2. 命令行</h3>
      <Code
        title="cmd"
        text={[
          `"${exe}\\charavoice.exe" speak "你好，我们开始吧。" --voice ${voice}`,
          `"${exe}\\charavoice.exe" listen --timeout 15`,
          `"${exe}\\charavoice.exe" voices`,
        ].join("\n")}
      />

      <h3>3. HTTP（兼容 OpenAI 格式）</h3>
      <p>
        <code>POST /v1/audio/speech</code>：model 或 voice 填声音 id；可选 language、response_format（wav/mp3/opus/aac/flac/pcm）、stream、play（false = 只返回音频不播放）。
      </p>
      <Code
        title="Python（openai SDK）"
        text={`from openai import OpenAI
client = OpenAI(base_url="${c.base_url}/v1", api_key="<访问口令>")
audio = client.audio.speech.create(model="${voice}", voice="${voice}", input="こんにちは！", response_format="wav")
audio.write_to_file("hello.wav")

text = client.audio.transcriptions.create(model="sensevoice", file=open("hello.wav", "rb")).text`}
      />
      <Code
        title="cmd（curl）"
        text={`curl -X POST ${c.base_url}/v1/speak -H "Authorization: Bearer <访问口令>" -H "Content-Type: application/json" -d "{\\"text\\": \\"Hello there\\", \\"voice\\": \\"${voice}\\"}"`}
      />
      <p>
        其他接口：<code>GET /v1/voices</code>、<code>POST /v1/audio/transcriptions</code>、<code>POST /v1/listen</code>（听一句）、
        <code>WS /v1/listen</code>（实时听写事件 speech_start / speech_end / transcript）、<code>POST /v1/audio/stop</code>。
        完整列表见 <code>{c.base_url}/docs</code>。
      </p>
    </div>
  );
}
