// 接入 Agent: connect CharaVoice voices (the material-library folders) to agents over MCP.
// Paths and the token are filled in at run time; nothing machine-specific is stored in the app.
import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "preact/hooks";
import { api, conn } from "../api";
import { ServiceCard } from "../components/ServiceCard";
import { notify } from "../store";

interface McpVoice {
  voice: string;
  name: string;
  status: "ready" | "training" | "not trained";
  default: boolean;
}
interface Agent {
  name: string;
  voice: string | null;
  calls: number;
  last_seen: number;
  via: string;
}
interface Tools {
  cli: string;
  mcp: string;
  mcp_args: string[];
}

const STATUS: Record<string, string> = { ready: "已就绪", training: "训练中", "not trained": "未训练" };

function Code({ title, shown, copy }: { title: string; shown: string; copy?: string }) {
  return (
    <div class="code">
      <div class="code-head">
        <span>{title}</span>
        <button class="small" onClick={() => navigator.clipboard.writeText(copy ?? shown).then(() => notify("已复制"), () => notify("复制失败", "error"))}>
          复制
        </button>
      </div>
      <pre>{shown}</pre>
    </div>
  );
}

function ago(t: number) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

export function Mcp() {
  const [tools, setTools] = useState<Tools | null>(null);
  const [info, setInfo] = useState<{ mcp_url: string; agent_token: string } | null>(null);
  const [list, setList] = useState<McpVoice[]>([]);
  const [agentsList, setAgents] = useState<Agent[]>([]);
  const [client, setClient] = useState<"claude-code" | "claude-desktop" | "cursor" | "http">("claude-code");
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<any>(null);

  const loadVoices = () => api<{ voices: McpVoice[] }>("/v1/simple/mcp/voices").then((r) => setList(r.voices)).catch(() => {});
  // the page's own test call identifies itself as "charavoice-test": not an agent to bind
  const loadAgents = () => api<{ agents: Agent[] }>("/v1/agents").then((r) => setAgents(r.agents.filter((a) => a.name !== "charavoice-test"))).catch(() => {});
  useEffect(() => {
    invoke<Tools>("tool_paths").then(setTools, () => {});
    api<{ mcp_url: string; agent_token: string }>("/v1/integration").then(setInfo);
    loadVoices();
    loadAgents();
    const t = setInterval(loadAgents, 5000);
    return () => clearInterval(t);
  }, []);

  async function setDefault(id: string) {
    await api("/v1/settings", { method: "PATCH", json: { speech: { default_voice: id } } }).catch((e) => notify(e.message, "error"));
    loadVoices();
  }
  async function bind(name: string, voice: string | null) {
    await api(`/v1/agents/${encodeURIComponent(name)}`, { method: "PATCH", json: { voice } }).catch((e) => notify(e.message, "error"));
    notify("已保存");
    loadAgents();
  }
  async function runTest() {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api("/v1/simple/mcp/test", { method: "POST" }));
    } catch (e: any) {
      setTest({ ok: false, error: e.message });
    } finally {
      setTesting(false);
    }
  }

  const c = conn.value;
  if (!c || !info) return <div class="page doc" />;
  const exe = tools?.mcp ?? "<CharaVoice MCP 程序>";
  const args = tools?.mcp_args ?? [];
  const stdioCmd = [`"${exe}"`, ...args].join(" ");
  const stdioJson = JSON.stringify({ mcpServers: { charavoice: { command: exe, args } } }, null, 2);
  const httpJson = (token: string) =>
    JSON.stringify({ mcpServers: { charavoice: { type: "http", url: info.mcp_url, headers: { Authorization: `Bearer ${token}` } } } }, null, 2);
  const masked = "<Agent 口令>";
  const ready = list.filter((v) => v.status === "ready");

  return (
    <div class="page doc mcp">
      <h2>接入 Agent</h2>
      <p class="lead">
        训练好的声音可以通过 MCP 给任何 Agent 使用：Agent 调用 <code>speak</code> 就会用这个声音在本机念出日语。软件没开也没关系，Agent 第一次调用时会在后台自动启动 CharaVoice；软件开着时共用同一个后台，不会重复加载模型。
      </p>

      <div class="card kv">
        <div>
          <span>MCP 服务</span>
          <span class="dot ready" /> 运行中 · stdio 与 Streamable HTTP
        </div>
        <div>
          <span>HTTP 地址</span>
          <code>{info.mcp_url}</code>
        </div>
        <div>
          <span>工具</span>
          <code>list_voices</code> <code>speak</code> <code>synthesize</code> <code>stop</code>
        </div>
      </div>

      <ServiceCard />

      <h3>声音</h3>
      <div class="card">
        {list.length === 0 ? (
          <p class="muted">还没有素材文件夹。先到“素材库”录制，再到“训练”学习声线。</p>
        ) : (
          <table class="agents">
            <thead>
              <tr>
                <th>声音</th>
                <th>MCP 里的名字（voice 参数）</th>
                <th>状态</th>
                <th>默认</th>
              </tr>
            </thead>
            <tbody>
              {list.map((v) => (
                <tr>
                  <td>
                    <strong>{v.name}</strong>
                  </td>
                  <td>
                    <code>{v.voice}</code>
                  </td>
                  <td class={v.status === "ready" ? "" : "muted"}>{STATUS[v.status]}</td>
                  <td>
                    <label class="check slim">
                      <input type="radio" name="defvoice" checked={v.default} disabled={v.status !== "ready"} onChange={() => setDefault(v.voice)} />
                      {v.default ? "默认声音" : ""}
                    </label>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p class="muted small-note">Agent 调用时不指定 voice，就用它在下面绑定的声音；没有绑定就用默认声音。</p>
      </div>

      <h3>接入配置</h3>
      <div class="seg mcp-tabs">
        {(
          [
            ["claude-code", "Claude Code"],
            ["claude-desktop", "Claude Desktop"],
            ["cursor", "Cursor"],
            ["http", "其他（HTTP）"],
          ] as const
        ).map(([k, label]) => (
          <button class={client === k ? "on" : ""} onClick={() => setClient(k)}>
            {label}
          </button>
        ))}
      </div>
      {client === "claude-code" && (
        <>
          <p>在 cmd 里运行（stdio，推荐：软件没开也能用）：</p>
          <Code title="cmd" shown={`claude mcp add charavoice -- ${stdioCmd}`} />
          <p class="muted">加上 <code>-s user</code> 可以让所有项目都能用。</p>
        </>
      )}
      {client === "claude-desktop" && (
        <>
          <p>
            打开 Claude Desktop 的“设置 → 开发者 → 编辑配置”，把下面的内容合并进 <code>claude_desktop_config.json</code>，然后重启 Claude Desktop：
          </p>
          <Code title="claude_desktop_config.json" shown={stdioJson} />
        </>
      )}
      {client === "cursor" && (
        <>
          <p>
            放进项目里的 <code>.cursor\mcp.json</code>（只对这个项目生效），或用户目录下的 <code>.cursor\mcp.json</code>（所有项目）：
          </p>
          <Code title="mcp.json" shown={stdioJson} />
        </>
      )}
      {client === "http" && (
        <>
          <p>支持 Streamable HTTP 的 Agent 可以直接连接正在运行的 CharaVoice（需要软件或后台开着）。复制时会带上真实口令：</p>
          <Code title="mcp.json" shown={httpJson(masked)} copy={httpJson(info.agent_token)} />
          <p class="muted">口令在“高级功能 → 接入（完整）”里可以重新生成。只监听 127.0.0.1，局域网访问不到。</p>
        </>
      )}

      <h3>按 Agent 绑定声音</h3>
      <div class="card">
        {agentsList.length === 0 ? (
          <p class="muted">还没有 Agent 调用过。Agent 第一次说话后会出现在这里，然后就可以给它绑定一个声音。</p>
        ) : (
          <table class="agents">
            <thead>
              <tr>
                <th>Agent</th>
                <th>声音</th>
                <th>最近</th>
              </tr>
            </thead>
            <tbody>
              {agentsList.map((a) => (
                <tr>
                  <td>
                    <strong>{a.name}</strong>
                    <div class="muted">{a.calls} 次</div>
                  </td>
                  <td>
                    <select value={a.voice ?? ""} onChange={(e) => bind(a.name, (e.target as HTMLSelectElement).value || null)}>
                      <option value="">默认声音</option>
                      {a.voice && !ready.some((v) => v.voice === a.voice) && <option value={a.voice}>{a.voice}（不在素材库）</option>}
                      {ready.map((v) => (
                        <option value={v.voice}>{v.name}</option>
                      ))}
                    </select>
                  </td>
                  <td class="muted">{ago(a.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p class="muted small-note">
          MCP 客户端按连接时报告的名字区分（如 claude-code）；也可以用环境变量 <code>CHARAVOICE_AGENT</code>（stdio）或请求头 <code>X-CharaVoice-Agent</code>（HTTP）指定。
        </p>
      </div>

      <h3>测试</h3>
      <div class="card">
        <div class="btn-row">
          <button class="primary" disabled={testing || !ready.length} onClick={runTest}>
            {testing ? "测试中…" : "模拟 Agent 调用一次"}
          </button>
          <span class="muted">像 Agent 一样连上 MCP，调用 list_voices 和 speak，用默认声音说一句“テストです。聞こえますか？”</span>
        </div>
        {test && (
          <div class={`mcp-test ${test.ok ? "ok" : "bad"}`}>
            {test.ok ? (
              <>
                <strong>通过</strong>：连接并列出声音 {test.connect_ms} ms，说话 {test.speak_ms} ms（含播放完）。
                <div class="muted">{test.speak}</div>
              </>
            ) : (
              <>
                <strong>失败</strong>：{test.error ?? test.speak ?? test.list_voices}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
