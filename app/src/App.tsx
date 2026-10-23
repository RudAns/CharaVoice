import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useState } from "preact/hooks";
import { conn, connError, connect, engine, onEvent } from "./api";
import { Ring } from "./components/Ring";
import { Integration } from "./pages/Integration";
import { Material } from "./pages/Material";
import { Settings } from "./pages/Settings";
import { Stage } from "./pages/Stage";
import { Voices } from "./pages/Voices";
import { go, page, refreshVoices, toast } from "./store";

const NAV = [
  { id: "stage", label: "舞台", icon: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z" },
  { id: "voices", label: "声音库", icon: "M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4" },
  { id: "material", label: "素材", icon: "M3 12h2l2-6 3 12 3-9 2 5 2-2h4" },
  { id: "integration", label: "接入 Agent", icon: "M8 9l-4 3 4 3M16 9l4 3-4 3M13 6l-2 12" },
  { id: "settings", label: "设置", icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.3l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2.2-1.3L14.4 3h-4l-.4 2.4a7.6 7.6 0 0 0-2.2 1.3l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2.2 1.3l.4 2.4h4l.4-2.4a7.6 7.6 0 0 0 2.2-1.3l2.4 1 2-3.4-2-1.6c.1-.4.1-.9.1-1.3Z" },
];

export default function App() {
  const [companion, setCompanion] = useState(false);
  useEffect(() => {
    connect();
    const un = listen<boolean>("companion", (e) => setCompanion(e.payload));
    return () => {
      un.then((f) => f());
    };
  }, []);
  useEffect(() => {
    if (!conn.value) return;
    refreshVoices().catch(() => {});
    return onEvent((ev) => {
      if (ev.type === "hello" || ev.type === "voices_changed") refreshVoices().catch(() => {});
    });
  }, [conn.value]);

  if (!conn.value) {
    return (
      <div class="splash">
        <div class="splash-ring">
          <Ring class="ring" />
        </div>
        <h1>CharaVoice</h1>
        <p class={connError.value ? "error" : "muted"}>{connError.value || "正在启动本地服务…"}</p>
      </div>
    );
  }

  const eng = engine.value;
  return (
    <div class="app">
      <nav class="side">
        <div class="brand">
          <span class="brand-mark" />
          CharaVoice
        </div>
        {NAV.map((n) => (
          <button key={n.id} class={`nav ${page.value === n.id ? "on" : ""}`} onClick={() => go(n.id)}>
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d={n.icon} />
            </svg>
            {n.label}
          </button>
        ))}
        <div class="grow" />
        <button
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
        </button>
        <div class={`engine-pill ${eng?.state ?? "stopped"}`} title={eng?.message}>
          <span class="dot" />
          引擎 {eng?.version ?? ""} · {eng?.state === "ready" ? "就绪" : eng?.state === "starting" ? "启动中" : eng?.state === "error" ? "出错" : "未启动"}
        </div>
      </nav>
      <main>
        {page.value === "stage" && <Stage />}
        {page.value === "voices" && <Voices />}
        {page.value === "material" && <Material />}
        {page.value === "integration" && <Integration />}
        {page.value === "settings" && <Settings />}
      </main>
      {toast.value && <div class={`toast ${toast.value.kind}`}>{toast.value.text}</div>}
    </div>
  );
}
