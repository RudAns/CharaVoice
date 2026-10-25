// Global dictation widget (Ctrl+Alt+Space): a small non-focusable pill near the bottom of the
// screen. The backend listens for one turn and types the text into the focused window.
import { invoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, conn, connect, onEvent } from "./api";
import { hotkeyLabel } from "./pages/Settings";

type Phase = "idle" | "listening" | "typing" | "done" | "error";

export function Dictation() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [text, setText] = useState("");
  const [level, setLevel] = useState(0);
  const hideTimer = useRef(0);
  const booted = useRef(false);
  const [key, setKey] = useState("");

  const toggle = () => api("/v1/dictation/toggle", { method: "POST" }).catch((e) => {
    setPhase("error");
    setText(e.message);
  });

  useEffect(() => {
    document.documentElement.classList.add("companion-root");
    connect();
    invoke<string>("dictation_hotkey").then(setKey);
    const un = tauriListen("dictation-hotkey", () => toggle());
    return () => {
      un.then((f) => f());
    };
  }, []);

  useEffect(() => {
    if (!conn.value) return;
    // Normally pre-created hidden; a visible window on load was just built by the hotkey itself.
    if (!booted.current) getCurrentWindow().isVisible().then((v) => v && toggle());
    booted.current = true;
    return onEvent((ev) => {
      if (ev.type === "level" && ev.source === "mic") setLevel(ev.level);
      if (ev.type !== "dictation") return;
      clearTimeout(hideTimer.current);
      setPhase(ev.state);
      if (ev.state === "listening") setText(ev.partial ?? "");
      else if (ev.state === "typing") setText(ev.text);
      else if (ev.state === "done") setText(ev.text || ev.reason || "");
      else if (ev.state === "error") setText(ev.message);
      if (ev.state === "done" || ev.state === "error") {
        hideTimer.current = window.setTimeout(() => getCurrentWindow().hide(), ev.state === "error" ? 3500 : 1400);
      }
    });
  }, [conn.value]);

  const label = { idle: "准备中…", listening: "正在听，说完会自动输入", typing: "正在输入", done: "完成", error: "出错了" }[phase];
  return (
    <div class={`dict dict-${phase}`}>
      <div class="dict-orb" style={{ transform: `scale(${1 + level * 0.35})` }} />
      <div class="dict-body">
        <div class="dict-label">{label}</div>
        <div class="dict-text">{tail(text) || (phase === "listening" ? "…" : "")}</div>
      </div>
      {key && <div class="dict-key">{hotkeyLabel(key)}</div>}
    </div>
  );
}

// Show the end of a long sentence (the newest words matter while speaking); CJK counts double.
function tail(s: string, width = 28) {
  let w = 0;
  let i = s.length;
  while (i > 0) {
    const c = s.charCodeAt(i - 1);
    w += c > 0x2e80 ? 2 : 1;
    if (w > width) return "…" + s.slice(i);
    i--;
  }
  return s;
}
