// App-wide state shared by pages.
import { computed, signal } from "@preact/signals";
import { api, type Voice } from "./api";

export const voices = signal<Voice[]>([]);
export const selectedId = signal<string>(safeGet("cv.voice") ?? "");
// The app opens on the material library. The older, fuller pages (stage, voice library, emotions,
// voice tweaks, …) are hidden unless "show advanced features" is on in Settings.
export const page = signal<string>("library");
export const showAdvanced = signal<boolean>(safeGet("cv.adv") === "1");
export const ADVANCED_PAGES = ["stage", "voices", "material", "train", "integration", "models"];
export function setShowAdvanced(v: boolean) {
  showAdvanced.value = v;
  remember("cv.adv", v ? "1" : "0");
  if (!v && ADVANCED_PAGES.includes(page.value)) go("library");
}
/** A hotkey recording that just stopped and waits for trimming (set from any page). */
export const pendingRec = signal<{ rec: string; seconds: number } | null>(null);
/** Required models/engines that are not installed yet (filled from /v1/assets). */
export const missingAssets = signal<string[]>([]);
export const toast = signal<{ text: string; kind: "info" | "error" } | null>(null);

export const selected = computed<Voice | undefined>(
  () => voices.value.find((v) => v.id === selectedId.value) ?? voices.value.find((v) => v.default) ?? voices.value[0],
);

function safeGet(k: string): string | null {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
}
export function remember(k: string, v: string) {
  try {
    localStorage.setItem(k, v);
  } catch {}
}

export function go(p: string) {
  page.value = p;
  remember("cv.page", p);
}

export function select(id: string) {
  selectedId.value = id;
  remember("cv.voice", id);
}

export async function refreshVoices() {
  const r = await api<{ data: Voice[] }>("/v1/voices");
  voices.value = r.data;
}

let toastTimer = 0;
export function notify(text: string, kind: "info" | "error" = "info") {
  toast.value = { text, kind };
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (toast.value = null), kind === "error" ? 7000 : 3500);
}
