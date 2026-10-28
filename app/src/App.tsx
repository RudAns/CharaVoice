import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useState } from "preact/hooks";
import { conn, connError, connect, engine, onEvent, serviceStopped } from "./api";
import { CloseGuard } from "./components/CloseGuard";
import { Ring } from "./components/Ring";
import { Integration } from "./pages/Integration";
import { Mcp } from "./pages/Mcp";
import { Models } from "./pages/Models";
import { Material } from "./pages/Material";
import { Train } from "./pages/Train";
import { Settings } from "./pages/Settings";
import { Simple } from "./pages/Simple";
import { Stage } from "./pages/Stage";
import { Voices } from "./pages/Voices";
import { api } from "./api";
import { go, missingAssets, notify, page, pendingRec, refreshVoices, showAdvanced, toast } from "./store";

// The three things the app is for: the material library, training, and connecting agents (MCP)
const MAIN_NAV = [
  { id: "library", label: "素材库", icon: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" },
  { id: "training", label: "训练", icon: "M4 19h16M7 16V9M12 16V5M17 16v-4" },
  { id: "mcp", label: "接入 Agent", icon: "M8 9l-4 3 4 3M16 9l4 3-4 3M13 6l-2 12" },
];
const SETTINGS_ICON = "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.3l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2.2-1.3L14.4 3h-4l-.4 2.4a7.6 7.6 0 0 0-2.2 1.3l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2.2 1.3l.4 2.4h4l.4-2.4a7.6 7.6 0 0 0 2.2-1.3l2.4 1 2-3.4-2-1.6c.1-.4.1-.9.1-1.3Z";
// advanced features (shown with Settings → 显示高级功能); the code stays, only the nav hides it
const NAV = [
  { id: "stage", label: "舞台", icon: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z" },
  { id: "voices", label: "声音库", icon: "M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4" },
  { id: "material", label: "素材", icon: "M3 12h2l2-6 3 12 3-9 2 5 2-2h4" },
  { id: "train", label: "训练（完整）", icon: "M4 19h16M7 16V9M12 16V5M17 16v-4" },
  { id: "integration", label: "接入（完整）", icon: "M8 9l-4 3 4 3M16 9l4 3-4 3M13 6l-2 12" },
  { id: "models", label: "模型", icon: "M12 3 3 7.5l9 4.5 9-4.5L12 3ZM3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5" },
];

const ICON = (d: string) => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d={d} />
  </svg>
);

export default function App() {
  const [companion, setCompanion] = useState(false);
  useEffect(() => {
    connect();
    const un = listen<boolean>("companion", (e) => setCompanion(e.payload));
    // push to talk: pressed -> microphone on, released -> recognise and read back (backend decides the voice)
    const unPtt = listen<boolean>("ptt-hotkey", (e) => {
      if (e.payload) api("/v1/ptt/start", { method: "POST" }).catch((er) => notify(er.message, "error"));
      else api("/v1/ptt/stop", { method: "POST", json: {} }).catch((er) => notify(er.message, "error"));
    });
    return () => {
      un.then((f) => f());
      unPtt.then((f) => f());
    };
  }, []);
  useEffect(() => {
    if (!conn.value) return;
    refreshVoices().catch(() => {});
    const checkAssets = () => api<{ missing_required: string[] }>("/v1/assets").then((r) => (missingAssets.value = r.missing_required)).catch(() => {});
    checkAssets();
    return onEvent((ev) => {
      if (ev.type === "hello" || ev.type === "voices_changed") refreshVoices().catch(() => {});
      if (ev.type === "asset" && ev.status !== "running") checkAssets();
      if (ev.type === "simple_recorded" && page.value !== "library") {
        pendingRec.value = { rec: ev.rec, seconds: ev.seconds };
        go("library");
      }
    });
  }, [conn.value]);

  if (!conn.value) {
    return (
      <div class="splash">
        <div class="splash-ring">
          <Ring class="ring" />
        </div>
        <h1>CharaVoice</h1>
        {serviceStopped.value ? (
          <>
            <p class="muted">后台服务已停止，显存已释放。Agent 下次调用时会自动启动它。</p>
            <button
              class="primary"
              onClick={async () => {
                serviceStopped.value = false;
                await invoke("set_backend_paused", { paused: false }).catch(() => {});
                connect();
              }}
            >
              重新启动
            </button>
          </>
        ) : (
          <p class={connError.value ? "error" : "muted"}>{connError.value || "正在启动本地服务…"}</p>
        )}
        <CloseGuard />
      </div>
    );
  }

  const eng = engine.value;
  return (
    <div class="app">
      <CloseGuard />
      <nav class="side">
        <div class="brand">
          <span class="brand-mark" />
          CharaVoice
        </div>
        {MAIN_NAV.map((n) => (
          <button key={n.id} class={`nav ${page.value === n.id ? "on" : ""}`} onClick={() => go(n.id)}>
            {ICON(n.icon)}
            {n.label}
          </button>
        ))}
        {showAdvanced.value && <div class="nav-group">高级功能</div>}
        {showAdvanced.value &&
          NAV.map((n) => (
            <button key={n.id} class={`nav sub ${page.value === n.id ? "on" : ""}`} onClick={() => go(n.id)}>
              {ICON(n.icon)}
              {n.label}
            </button>
          ))}
        <div class="grow" />
        <button class={`nav ${page.value === "settings" ? "on" : ""}`} onClick={() => go("settings")}>
          {ICON(SETTINGS_ICON)}
          设置
        </button>
        {showAdvanced.value && <button
          class={`nav ${companion ? "on" : ""}`}
          title="在屏幕角落显示一个置顶的小窗，只有角色和圆环"
          onClick={async () => setCompanion(await invoke<boolean>("toggle_companion"))}
        >
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
            <rect x="3" y="4" width="18" height="13" rx="2" />
            <circle cx="16" cy="12.5" r="2.5" />
            <path d="M8 21h8" />
          </svg>
          桌面伴随
        </button>}
        <div class={`engine-pill ${eng?.state ?? "stopped"}`} title={eng?.message}>
          <span class="dot" />
          引擎 {eng?.version ?? ""} · {eng?.state === "ready" ? "就绪" : eng?.state === "starting" ? "启动中" : eng?.state === "error" ? "出错" : "未启动"}
        </div>
      </nav>
      <main>
        {missingAssets.value.length > 0 && page.value !== "models" && (
          <div class="setup-banner">
            还缺少 {missingAssets.value.length} 个必需组件（听写模型或语音引擎），装好后才能完整使用。
            <button class="primary small" onClick={() => go("models")}>
              去安装
            </button>
          </div>
        )}
        {page.value === "library" && <Simple view="library" />}
        {page.value === "training" && <Simple view="train" />}
        {page.value === "mcp" && <Mcp />}
        {page.value === "models" && <Models />}
        {page.value === "stage" && <Stage />}
        {page.value === "voices" && <Voices />}
        {page.value === "material" && <Material />}
        {page.value === "train" && <Train />}
        {page.value === "integration" && <Integration />}
        {page.value === "settings" && <Settings />}
      </main>
      {toast.value && <div class={`toast ${toast.value.kind}`}>{toast.value.text}</div>}
    </div>
  );
}
