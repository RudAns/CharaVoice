import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "preact/hooks";
import { api, conn, EMOTION_LABEL } from "../api";
import { notify, selected, voices } from "../store";

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

interface Agent {
  name: string;
  voice: string | null;
  emotion: string | null;
  calls: number;
  last_seen: number;
  via: string;
}

const VIA: Record<string, string> = { mcp: "MCP", cli: "命令行", http: "HTTP" };

function ago(t: number) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

function AgentVoices() {
  const [list, setList] = useState<Agent[] | null>(null);
  const load = () => api<{ agents: Agent[] }>("/v1/agents").then((r) => setList(r.agents)).catch(() => setList([]));
  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);
  async function patch(name: string, p: Partial<Agent>) {
    try {
      await api(`/v1/agents/${encodeURIComponent(name)}`, { method: "PATCH", json: p });
      notify("已保存");
      load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  if (!list) return null;
  return (
    <div class="card">
      {list.length === 0 ? (
        <p class="muted">还没有 Agent 调用过。Agent 第一次说话后会出现在这里，然后就可以给它单独选一个声音。</p>
      ) : (
        <table class="agents">
          <thead>
            <tr>
              <th>Agent</th>
              <th>声音</th>
              <th>默认情绪</th>
              <th>最近</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((a) => (
              <tr>
                <td>
                  <strong>{a.name}</strong>
                  <div class="muted">
                    {VIA[a.via] ?? a.via} · {a.calls} 次
                  </div>
                </td>
                <td>
                  <select value={a.voice ?? ""} onChange={(e) => patch(a.name, { voice: (e.target as HTMLSelectElement).value || null })}>
                    <option value="">默认声音</option>
                    {voices.value.map((v) => (
                      <option value={v.id}>{v.name}</option>
                    ))}
                  </select>
                </td>
                <td>
                  <select value={a.emotion ?? ""} onChange={(e) => patch(a.name, { emotion: (e.target as HTMLSelectElement).value || null })}>
                    <option value="">不指定</option>
                    {Object.entries(EMOTION_LABEL).map(([k, label]) => (
                      <option value={k}>{label}</option>
                    ))}
                  </select>
                </td>
                <td class="muted">{ago(a.last_seen)}</td>
                <td>
                  <button class="small" onClick={() => api(`/v1/agents/${encodeURIComponent(a.name)}`, { method: "DELETE" }).then(load)}>
                    移除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p class="muted">
        调用时指定了 voice 就用指定的声音；没指定时，用这里给该 Agent 选的声音。MCP 客户端按它连接时报告的名字区分（如 claude-code）；
        命令行用 <code>--agent 名字</code>，HTTP 用请求头 <code>X-CharaVoice-Agent</code>。
      </p>
    </div>
  );
}

export function Integration() {
  const [home, setHome] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [info, setInfo] = useState<{ mcp_url: string; agent_token: string } | null>(null);
  useEffect(() => {
    invoke<string>("home_path").then((p) => setHome(p.replace(/^\\\\\?\\/, "")));
    api<{ mcp_url: string; agent_token: string }>("/v1/integration").then(setInfo);
  }, []);
  const c = conn.value;
  if (!c || !info) return null;
  const voice = selected.value?.id ?? "<声音id>";
  const exe = `${home}\\.venv\\Scripts`;
  const token = showToken ? info.agent_token : "•".repeat(12);
  const rotate = async () => {
    setInfo(await api("/v1/integration/rotate-token", { method: "POST" }));
    notify("已生成新口令，旧口令立即失效");
  };

  return (
    <div class="page doc">
      <h2>接入 Agent</h2>
      <p class="lead">
        CharaVoice 不含大语言模型，只负责“说”和“听”。任何 Agent 都可以通过 MCP、命令行或 HTTP 调用。语音默认由 CharaVoice 在本机播放，圆环会同步显示。
      </p>

      <div class="card kv">
        <div>
          <span>本地地址</span>
          <code>{c.base_url}</code>
        </div>
        <div>
          <span>Agent 口令</span>
          <code>{token}</code>
          <button class="small" onClick={() => setShowToken(!showToken)}>
            {showToken ? "隐藏" : "显示"}
          </button>
          <button class="small" onClick={() => navigator.clipboard.writeText(info.agent_token).then(() => notify("口令已复制"))}>
            复制
          </button>
          <button class="small" onClick={rotate}>
            重新生成
          </button>
        </div>
        <p class="muted">
          Agent 口令长期有效，用于写进 Agent 的配置（MCP over HTTP、OpenAI SDK 的 api_key）。命令行和 stdio 版 MCP 不需要它，会自动读取 data\runtime.json 里每次启动新生成的口令。
          只监听 127.0.0.1，局域网无法访问。
        </p>
      </div>

      <h3>按 Agent 分配声音</h3>
      <AgentVoices />

      <h3>1. MCP（Claude Code、Cursor 等）</h3>
      <p>提供 speak、list_voices、listen、transcribe 四个工具。两种连接方式任选其一。</p>
      <p>
        <strong>Streamable HTTP</strong>（推荐：不用另起进程，CharaVoice 开着就能用）：
      </p>
      <Code title="cmd" text={`claude mcp add --transport http charavoice ${info.mcp_url} --header "Authorization: Bearer <Agent 口令>"`} />
      <Code
        title=".mcp.json"
        text={JSON.stringify({ mcpServers: { charavoice: { type: "http", url: info.mcp_url, headers: { Authorization: "Bearer <Agent 口令>" } } } }, null, 2)}
      />
      <p>
        <strong>stdio</strong>（由 Agent 启动一个小进程转发）：
      </p>
      <Code title="cmd" text={`claude mcp add charavoice -- "${exe}\\charavoice-mcp.exe"`} />

      <h3>2. 命令行</h3>
      <Code
        title="cmd"
        text={[
          `"${exe}\\charavoice.exe" speak "你好，我们开始吧。" --voice ${voice}`,
          `"${exe}\\charavoice.exe" speak "我用自己的声音说话" --agent my-agent`,
          `"${exe}\\charavoice.exe" listen --timeout 15`,
          `"${exe}\\charavoice.exe" voices`,
        ].join("\n")}
      />

      <h3>3. HTTP（兼容 OpenAI 格式）</h3>
      <p>
        <code>POST /v1/audio/speech</code>：model 或 voice 填声音 id；可选 language、response_format（wav/mp3/opus/aac/flac/pcm）、stream、play（false = 只返回音频不播放）、emotion、streaming_mode。
      </p>
      <Code
        title="Python（openai SDK）"
        text={`from openai import OpenAI
client = OpenAI(base_url="${c.base_url}/v1", api_key="<Agent 口令>",
                default_headers={"X-CharaVoice-Agent": "my-agent"})
audio = client.audio.speech.create(model="${voice}", voice="${voice}", input="こんにちは！", response_format="wav")
audio.write_to_file("hello.wav")

text = client.audio.transcriptions.create(model="sensevoice", file=open("hello.wav", "rb")).text`}
      />
      <Code
        title="cmd（curl）"
        text={`curl -X POST ${c.base_url}/v1/speak -H "Authorization: Bearer <Agent 口令>" -H "X-CharaVoice-Agent: my-agent" -H "Content-Type: application/json" -d "{\\"text\\": \\"Hello there\\"}"`}
      />
      <p>
        其他接口：<code>GET /v1/voices</code>、<code>POST /v1/audio/transcriptions</code>、<code>POST /v1/listen</code>（听一句）、
        <code>WS /v1/listen</code>（实时听写事件 speech_start / partial / speech_end / transcript）、<code>POST /v1/audio/stop</code>。
        能力清单：<code>{c.base_url}/.well-known/charavoice.json</code>，完整列表见 <code>{c.base_url}/docs</code>。
      </p>
    </div>
  );
}
