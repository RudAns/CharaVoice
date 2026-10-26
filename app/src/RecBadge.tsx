// Recording badge (simple mode hotkey, Ctrl+Alt+R by default): a small non-focusable pill at the
// top of the screen with a red dot and a timer. It toggles the loopback recording itself, so the
// hotkey works while the main window is minimized; when a recording stops it brings the main
// window back to the trim editor.
import { invoke } from "@tauri-apps/api/core";
import { emit, listen as tauriListen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, conn, connect, onEvent } from "./api";
import { hotkeyLabel } from "./pages/Settings";

type Phase = "idle" | "recording" | "saved" | "error";
const mmss = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

export function RecBadge() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const [msg, setMsg] = useState("");
  const [key, setKey] = useState("");
  const hideTimer = useRef(0);
  const simpleRec = useRef(false);
  const booted = useRef(false);

  const later = (ms: number) => {
    clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => {
      getCurrentWindow().hide();
      setPhase("idle");
    }, ms);
  };
  const last = useRef(0);
  const toggle = () => {
    // one press, one toggle: ignore repeats (a held key, or an event and a fresh page at once)
    if (Date.now() - last.current < 700) return;
    last.current = Date.now();
    return api("/v1/simple/record/toggle", { method: "POST", json: {} }).catch((e) => {
      setPhase("error");
      if (e.code === "consent_required") {
        setMsg("首次使用请先在主窗口确认");
        emit("simple-consent-needed");
        invoke("show_main");
      } else setMsg(e.message);
      later(3500);
    });
  };

  useEffect(() => {
    document.documentElement.classList.add("companion-root");
    connect();
    invoke<string>("record_hotkey").then(setKey);
    const un = tauriListen("record-hotkey", () => toggle());
    return () => {
      un.then((f) => f());
    };
  }, []);

  useEffect(() => {
    if (!conn.value) return;
    // normally pre-created hidden; "#recbadge-start" means the hotkey itself just built it
    if (!booted.current && location.hash === "#recbadge-start") toggle();
    booted.current = true;
    return onEvent((ev) => {
      if (ev.type === "rec_start") {
        clearTimeout(hideTimer.current);
        simpleRec.current = ev.voice === "__simple__";
        setPhase("recording");
        setSeconds(0);
        getCurrentWindow().show();
      } else if (ev.type === "rec_level") {
        setSeconds(ev.seconds);
        setLevel(ev.level);
      } else if (ev.type === "rec_stop") {
        setPhase(ev.error ? "error" : "saved");
        setMsg(ev.error ?? `已停止 · ${mmss(ev.seconds)}`);
        later(ev.error ? 3500 : 1600);
      } else if (ev.type === "simple_recorded" && simpleRec.current) {
        setMsg(`已停止 · ${mmss(ev.seconds)}，在主窗口截取`);
        invoke("show_main");
      }
    });
  }, [conn.value]);

  return (
    <div class={`recbadge rb-${phase}`}>
      <span class="rb-dot" style={{ transform: `scale(${1 + level * 0.5})` }} />
      <span class="rb-text">{phase === "recording" ? `录制中 ${mmss(seconds)}` : phase === "idle" ? "准备录制…" : msg}</span>
      {phase === "recording" && key && <span class="rb-key">{hotkeyLabel(key)} 停止</span>}
    </div>
  );
}
