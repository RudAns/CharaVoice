// Connection to the CharaVoice backend: REST helper + the /v1/events stream.
import { invoke } from "@tauri-apps/api/core";
import { signal } from "@preact/signals";

export type Presence = "idle" | "listening" | "thinking" | "speaking";

export interface EngineInfo {
  state: "stopped" | "starting" | "ready" | "error";
  message: string;
  installed: boolean;
  port: number | null;
  version: string;
}

export type AvatarSlot = "default" | "idle" | "listening" | "speaking";

export interface Voice {
  id: string;
  name: string;
  language: "zh" | "en" | "ja";
  base: string;
  ready: boolean;
  trained: boolean;
  default?: boolean;
  ref: { audio: string; text: string; lang: string; duration: number } | null;
  consent: { kind: string; label: string; notice: string; confirmed_at: string };
  refs?: { id: string; text: string; lang: string; duration: number; emotion: string; url: string }[];
  emotions?: string[];
  params: Record<string, number>;
  tweak: Tweak;
  avatar: Record<AvatarSlot, { url: string; kind: "image" | "video" } | null>;
  avatar_mode?: "circle" | "free";
  created_at: string;
}

export interface Tweak {
  pitch: number;
  formant: number;
  brightness: number;
  breath: number;
}

interface BackendInfo {
  base_url: string;
  token: string;
  managed: boolean;
}

export const conn = signal<BackendInfo | null>(null);
export const connError = signal<string>("");
export const presence = signal<Presence>("idle");
export const engine = signal<EngineInfo | null>(null);
export const listening = signal(false);

type Listener = (ev: any) => void;
const listeners = new Set<Listener>();
export function onEvent(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code = "") {
    super(message);
  }
}

export function url(path: string, withToken = false): string {
  const c = conn.value!;
  const u = c.base_url + path;
  return withToken ? `${u}${u.includes("?") ? "&" : "?"}token=${encodeURIComponent(c.token)}` : u;
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const c = conn.value;
  if (!c) throw new ApiError(0, "后端尚未连接");
  const headers: Record<string, string> = { Authorization: `Bearer ${c.token}` };
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  let r: Response;
  try {
    r = await fetch(c.base_url + path, { ...init, body, headers: { ...headers, ...(init.headers as any) } });
  } catch {
    throw new ApiError(0, "无法连接 CharaVoice 后端");
  }
  if (!r.ok) {
    let msg = `${r.status}`;
    let code = "";
    try {
      const j = await r.json();
      msg = j?.error?.message ?? j?.detail ?? msg;
      code = j?.error?.code ?? "";
    } catch {}
    throw new ApiError(r.status, msg, code);
  }
  const ct = r.headers.get("content-type") ?? "";
  return (ct.includes("json") ? r.json() : r.blob()) as Promise<T>;
}

let ws: WebSocket | null = null;
function openEvents() {
  const c = conn.value;
  if (!c) return;
  ws = new WebSocket(c.base_url.replace("http", "ws") + `/v1/events?token=${encodeURIComponent(c.token)}`);
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "hello") {
      presence.value = ev.state;
      engine.value = ev.engine;
    } else if (ev.type === "state") presence.value = ev.state;
    else if (ev.type === "engine") engine.value = ev;
    else if (ev.type === "listen_start") listening.value = true;
    else if (ev.type === "listen_stop") listening.value = false;
    listeners.forEach((fn) => fn(ev));
  };
  ws.onclose = () => {
    ws = null;
    setTimeout(() => connect(), 1500);
  };
}

let connecting = false;
export async function connect(): Promise<void> {
  if (connecting) return;
  connecting = true;
  try {
    conn.value = await invoke<BackendInfo>("backend_info");
    connError.value = "";
    const st = await api("/v1/status");
    presence.value = st.state;
    engine.value = st.engine;
    listening.value = st.listening;
    openEvents();
  } catch (e: any) {
    connError.value = typeof e === "string" ? e : e?.message ?? String(e);
    setTimeout(() => {
      connecting = false;
      connect();
    }, 3000);
    return;
  }
  connecting = false;
}

export const LANG_LABEL: Record<string, string> = { zh: "中文", en: "English", ja: "日本語", auto: "自动" };
export const EMOTION_LABEL: Record<string, string> = {
  neutral: "平静", happy: "开心", angry: "生气", sad: "悲伤", surprised: "惊讶", fearful: "害怕", gentle: "温柔",
};
