// Closing the window: hide to the tray (hotkeys and agents keep working) or quit. Asked the first
// time; the choice is remembered on this computer and can be changed in Settings. Also runs the
// tray menu's "free the GPU" and "stop the background service", which need the backend token.
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useEffect, useState } from "preact/hooks";
import { stopService, unloadService } from "./ServiceCard";

export type CloseAction = "ask" | "tray" | "exit";
const KEY = "charavoice.closeAction";

export function closeAction(): CloseAction {
  try {
    const v = localStorage.getItem(KEY);
    return v === "tray" || v === "exit" ? v : "ask";
  } catch {
    return "ask";
  }
}

export function setCloseAction(v: CloseAction) {
  try {
    localStorage.setItem(KEY, v);
  } catch {}
}

export function CloseGuard() {
  const [asking, setAsking] = useState(false);
  const [remember, setRemember] = useState(true);
  useEffect(() => {
    const w = getCurrentWindow();
    // no preventDefault -> the window is destroyed and the app quits
    const unClose = w.onCloseRequested((e) => {
      const a = closeAction();
      if (a === "exit") return;
      e.preventDefault();
      if (a === "tray") w.hide();
      else setAsking(true);
    });
    const unTray = listen<string>("tray-action", (e) => {
      if (e.payload === "unload") unloadService();
      if (e.payload === "stop") stopService();
    });
    return () => {
      unClose.then((f) => f());
      unTray.then((f) => f());
    };
  }, []);
  if (!asking) return null;
  const choose = (a: "tray" | "exit") => {
    if (remember) setCloseAction(a);
    setAsking(false);
    const w = getCurrentWindow();
    if (a === "tray") w.hide();
    else w.destroy();
  };
  return (
    <div class="modal-back">
      <div class="modal" role="dialog" aria-modal="true" aria-label="关闭窗口">
        <h2>关闭窗口</h2>
        <p>缩到任务栏右下角的托盘后，CharaVoice 继续运行：录制和按住说话的快捷键照常可用，Agent 也能随时调用。点托盘图标重新打开窗口，右键托盘图标选“退出”彻底关闭。</p>
        <label class="check">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember((e.target as HTMLInputElement).checked)} />
          记住我的选择（以后可以在“设置”里改）
        </label>
        <div class="btn-row end">
          <button onClick={() => setAsking(false)}>取消</button>
          <button onClick={() => choose("exit")}>退出程序</button>
          <button class="primary" onClick={() => choose("tray")}>缩到托盘</button>
        </div>
      </div>
    </div>
  );
}
