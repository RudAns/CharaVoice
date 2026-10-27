// Background service status: is the backend running, are the voice engines holding GPU memory,
// and when will it give the GPU back. Shown on the agent page and in Settings.
import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "preact/hooks";
import { api, serviceStopped } from "../api";
import { notify } from "../store";

interface Service {
  pid: number;
  uptime: number;
  idle_seconds: number;
  app_windows: number;
  engines: { name: string; label: string; state: string }[];
  unload_minutes: number;
  exit_minutes: number;
  gpu: { name?: string; used_mb?: number; total_mb?: number };
}

const mins = (s: number) => (s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分钟` : `${(s / 3600).toFixed(1)} 小时`);

export function ServiceCard({ editable = false }: { editable?: boolean }) {
  const [s, setS] = useState<Service | null>(null);
  const load = () => api<Service>("/v1/service").then(setS).catch(() => {});
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);
  if (!s) return null;
  const loaded = s.engines.filter((e) => e.state === "ready" || e.state === "starting");
  async function unload() {
    await api("/v1/service/unload", { method: "POST" }).catch((e) => notify(e.message, "error"));
    notify("已卸载模型，显存已释放；下次说话时会自动重新加载");
    load();
  }
  async function stop() {
    serviceStopped.value = true;
    await invoke("set_backend_paused", { paused: true }).catch(() => {});
    await api("/v1/service/stop", { method: "POST" }).catch(() => {});
  }
  async function save(patch: Record<string, number>) {
    await api("/v1/settings", { method: "PATCH", json: { idle: patch } }).catch((e) => notify(e.message, "error"));
    load();
  }
  return (
    <div class="card kv service">
      <div>
        <span>后台服务</span>
        <span class="dot ready" /> 运行中 · 已运行 {mins(s.uptime)} · 空闲 {mins(s.idle_seconds)}
      </div>
      <div>
        <span>显存</span>
        {loaded.length ? (
          <>
            <strong>{loaded.map((e) => e.label).join("、")} 已加载</strong>（约 3.5 GB）
          </>
        ) : (
          <strong>没有加载模型（不占显存）</strong>
        )}
        {s.gpu.used_mb != null && (
          <span class="muted">
            · 显卡总占用 {(s.gpu.used_mb / 1024).toFixed(1)} / {((s.gpu.total_mb ?? 0) / 1024).toFixed(0)} GB
          </span>
        )}
      </div>
      <div>
        <span>自动释放</span>
        {editable ? (
          <>
            空闲
            <input class="num" type="number" min="0" max="240" value={s.unload_minutes} onChange={(e) => save({ unload_minutes: +(e.target as HTMLInputElement).value })} />
            分钟后卸载模型；窗口关着时空闲
            <input class="num" type="number" min="0" max="1440" value={s.exit_minutes} onChange={(e) => save({ exit_minutes: +(e.target as HTMLInputElement).value })} />
            分钟后退出后台（0 = 不自动）
          </>
        ) : (
          <span class="muted">
            空闲 {s.unload_minutes} 分钟后卸载模型、释放显存；窗口关着时空闲 {s.exit_minutes} 分钟后后台自动退出（可在设置里改）
          </span>
        )}
      </div>
      <div class="btn-row">
        <button class="small" disabled={!loaded.length} onClick={unload}>
          立即释放显存
        </button>
        <button class="small danger" onClick={stop} title="停止后台服务和引擎；Agent 下次调用时会自动重新启动">
          停止后台服务
        </button>
      </div>
    </div>
  );
}
