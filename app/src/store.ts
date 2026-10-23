// App-wide state shared by pages.
import { computed, signal } from "@preact/signals";
import { api, type Voice } from "./api";

export const voices = signal<Voice[]>([]);
export const selectedId = signal<string>(safeGet("cv.voice") ?? "");
export const page = signal<string>(safeGet("cv.page") ?? "stage");
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
