// Desktop companion: frameless, transparent, always-on-top window with only the ring and avatar.
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect } from "preact/hooks";
import { conn, connect, onEvent } from "./api";
import { Showcase } from "./components/Showcase";
import { refreshVoices, selected, selectedId } from "./store";

export function Companion() {
  useEffect(() => {
    document.documentElement.classList.add("companion-root");
    connect();
    const sync = (e: StorageEvent) => e.key === "cv.voice" && e.newValue && (selectedId.value = e.newValue);
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  useEffect(() => {
    if (!conn.value) return;
    refreshVoices().catch(() => {});
    return onEvent((ev) => {
      if (ev.type === "hello" || ev.type === "voices_changed") refreshVoices().catch(() => {});
    });
  }, [conn.value]);

  const v = selected.value;
  return (
    <div class="companion">
      <Showcase voice={v} compact />
      <div class="companion-drag" data-tauri-drag-region />
      <div class="companion-bar">
        <span>{v?.name ?? "CharaVoice"}</span>
        <button class="small" title="关闭伴随窗口" onClick={() => getCurrentWindow().close()}>
          ✕
        </button>
      </div>
    </div>
  );
}
