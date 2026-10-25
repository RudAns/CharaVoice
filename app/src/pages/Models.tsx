import { openUrl } from "@tauri-apps/plugin-opener";
import { useEffect, useState } from "preact/hooks";
import { api, onEvent } from "../api";
import { missingAssets, notify } from "../store";

interface Job {
  status: "running" | "done" | "error" | "cancelled";
  progress: number;
  message: string;
}
interface Asset {
  id: string;
  name: string;
  purpose: string;
  license: string;
  license_url: string;
  size_mb: number;
  required: boolean;
  installed: boolean;
  note: string;
  job: Job | null;
}

const size = (mb: number) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`);

export function Models() {
  const [list, setList] = useState<Asset[] | null>(null);
  const [home, setHome] = useState("");
  const load = () =>
    api<{ assets: Asset[]; missing_required: string[]; home: string }>("/v1/assets").then((r) => {
      setList(r.assets);
      setHome(r.home);
      missingAssets.value = r.missing_required;
    });
  useEffect(() => {
    load();
    return onEvent((ev) => {
      if (ev.type !== "asset") return;
      setList((cur) => cur && cur.map((a) => (a.id === ev.id ? { ...a, job: ev.status === "removed" ? null : { status: ev.status, progress: ev.progress, message: ev.message } } : a)));
      if (ev.status !== "running") load();
    });
  }, []);
  async function act(path: string, method: string) {
    try {
      await api(path, { method });
      load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  if (!list) return <div class="page doc" />;
  const busy = list.some((a) => a.job?.status === "running");
  return (
    <div class="page doc">
      <h2>模型与引擎</h2>
      <p class="lead">
        CharaVoice 本身不带任何模型文件，第一次使用时在这里按需下载，都来自各项目的官方地址，可断点续传。带“必需”的是说话和听写的基本功能要用的。
      </p>
      <div class="assets">
        {list.map((a) => {
          const running = a.job?.status === "running";
          return (
            <div class={`card asset ${a.installed ? "ok" : ""}`}>
              <div class="asset-head">
                <div>
                  <strong>{a.name}</strong>
                  {a.required && <span class="tag">必需</span>}
                  <div class="muted">{a.purpose}</div>
                </div>
                <div class="asset-side">
                  <span class={`state ${a.installed ? "ok" : ""}`}>{a.installed ? "已安装" : running ? "安装中" : "未安装"}</span>
                  {!a.installed && !running && (
                    <button class="primary" disabled={busy} onClick={() => act(`/v1/assets/${a.id}/install`, "POST")}>
                      {a.job?.status === "cancelled" || a.job?.status === "error" ? "继续 / 重试" : "安装"}
                    </button>
                  )}
                  {running && <button onClick={() => act(`/v1/assets/${a.id}/cancel`, "POST")}>取消</button>}
                  {a.installed && (
                    <button class="ghost" onClick={() => confirm(`删除「${a.name}」？之后需要时可以重新下载。`) && act(`/v1/assets/${a.id}`, "DELETE")}>
                      删除
                    </button>
                  )}
                </div>
              </div>
              <div class="asset-meta">
                <span>大小 {size(a.size_mb)}</span>
                <span>
                  许可：
                  <a
                    href={a.license_url}
                    onClick={(e) => {
                      e.preventDefault();
                      openUrl(a.license_url).catch(() => notify(a.license_url));
                    }}
                  >
                    {a.license}
                  </a>
                </span>
                {a.note && <span class="muted">{a.note}</span>}
              </div>
              {a.job && a.job.status !== "done" && (
                <div class={`asset-job ${a.job.status}`}>
                  {running && (
                    <div class="bar">
                      <i style={{ width: `${Math.round(a.job.progress * 100)}%` }} />
                    </div>
                  )}
                  <span>{a.job.message}</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p class="muted">文件保存在 {home}。国内网络可以在设置文件里把 download.hf_endpoint 设为 Hugging Face 镜像地址。</p>
    </div>
  );
}
