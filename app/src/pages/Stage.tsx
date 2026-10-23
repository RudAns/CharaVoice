import { useEffect, useRef, useState } from "preact/hooks";
import { api, engine, LANG_LABEL, listening, onEvent, presence } from "../api";
import { Showcase } from "../components/Showcase";
import { go, notify, select, selected, voices } from "../store";

interface Line {
  id: string;
  who: "user" | "voice";
  text: string;
  meta?: string;
  t?: number;
}

const STATE_LABEL: Record<string, string> = { idle: "待机", listening: "正在听", thinking: "正在合成", speaking: "正在说话" };

export function Stage() {
  const v = selected.value;
  const [text, setText] = useState("");
  const [lang, setLang] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<Line[]>([]);
  const feed = useRef<HTMLDivElement>(null);

  useEffect(
    () =>
      onEvent((ev) => {
        if (ev.type === "transcript" && !ev.empty) {
          setLines((l) => {
            // Segments separated by a short pause belong to the same turn.
            const last = l[l.length - 1];
            if (last && last.who === "user" && last.t && ev.t - last.t < 2.5) {
              const sep = /[　-ヿ一-鿿＀-￯]$/.test(last.text) ? "" : " ";
              return [...l.slice(0, -1), { ...last, text: last.text + sep + ev.text, t: ev.t }];
            }
            return [...l.slice(-40), { id: `u${ev.id}-${ev.t}`, who: "user", text: ev.text, meta: LANG_LABEL[ev.language] ?? ev.language, t: ev.t }];
          });
        } else if (ev.type === "speak_start") {
          const name = voices.value.find((x) => x.id === ev.voice)?.name ?? ev.voice;
          setLines((l) => [...l.slice(-40), { id: `v${ev.id}-${ev.t}`, who: "voice", text: ev.text, meta: name }]);
        } else if (ev.type === "listen_error") notify(ev.message, "error");
      }),
    [],
  );
  useEffect(() => feed.current?.scrollTo({ top: 1e9, behavior: "smooth" }), [lines]);

  async function say() {
    const t = text.trim();
    if (!t || !v) return;
    setBusy(true);
    try {
      await api("/v1/speak", { method: "POST", json: { text: t, voice: v.id, language: lang === "auto" ? null : lang } });
      setText("");
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function toggleMic() {
    try {
      await api(`/v1/ui/listen?on=${!listening.value}`, { method: "POST" });
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  const eng = engine.value;
  const engineOk = eng?.state === "ready";

  if (!voices.value.length) {
    return (
      <div class="page stage empty-stage">
        <Showcase />
        <div class="empty-card">
          <h2>还没有声音</h2>
          <p>新建一个声音：上传一段 3-10 秒的清晰录音，就能立刻用它说话。之后还可以导入更多素材训练，让声音更像。</p>
          <button class="primary" onClick={() => go("voices")}>新建声音</button>
        </div>
      </div>
    );
  }

  return (
    <div class="page stage">
      <section class="stage-main">
        <header class="stage-head">
          <div class="voice-pick">
            <select value={v?.id} onChange={(e) => select((e.target as HTMLSelectElement).value)} aria-label="当前声音">
              {voices.value.map((x) => (
                <option value={x.id}>{x.name}</option>
              ))}
            </select>
            {v && <span class="tag">{v.trained ? "已训练" : "零样本"} · {LANG_LABEL[v.language]}</span>}
          </div>
          <div class={`presence presence-${presence.value}`}>
            <i />
            {STATE_LABEL[presence.value]}
          </div>
        </header>

        <Showcase voice={v} onUpload={() => go("voices")} />

        {!engineOk && (
          <div class={`engine-banner ${eng?.state === "error" ? "error" : ""}`}>
            {eng?.state === "starting" ? "声音引擎正在启动，稍等片刻就能说话。" : eng?.message ?? "声音引擎未连接"}
            {eng?.state !== "starting" && (
              <button onClick={() => api("/v1/engine/start", { method: "POST" }).catch((e) => notify(e.message, "error"))}>启动引擎</button>
            )}
          </div>
        )}

        <div class="say-box">
          <textarea
            value={text}
            placeholder={`让「${v?.name ?? ""}」说点什么…（Ctrl + Enter 发送）`}
            onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                say();
              }
            }}
            rows={2}
          />
          <div class="say-actions">
            <div class="seg" role="radiogroup" aria-label="语种">
              {["auto", "zh", "en", "ja"].map((l) => (
                <button class={lang === l ? "on" : ""} role="radio" aria-checked={lang === l} onClick={() => setLang(l)}>
                  {LANG_LABEL[l]}
                </button>
              ))}
            </div>
            <div class="grow" />
            <button
              class={`mic ${listening.value ? "on" : ""}`}
              onClick={toggleMic}
              title={listening.value ? "停止聆听" : "开始聆听（麦克风实时听写）"}
            >
              <MicIcon /> {listening.value ? "聆听中" : "聆听"}
            </button>
            {presence.value === "speaking" && (
              <button onClick={() => api("/v1/audio/stop", { method: "POST" })} title="停止播放">
                停止
              </button>
            )}
            <button class="primary" disabled={busy || !text.trim()} onClick={say}>
              {busy ? "合成中…" : "说"}
            </button>
          </div>
        </div>
      </section>

      <aside class="feed-panel">
        <h3>对话记录</h3>
        <div class="feed" ref={feed}>
          {lines.length === 0 && <p class="muted">角色说的话和听到的话会显示在这里，包括 Agent 通过接口调用的内容。</p>}
          {lines.map((l) => (
            <div key={l.id} class={`line ${l.who}`}>
              <span class="who">{l.who === "user" ? "听到" : "说出"}</span>
              <p>{l.text}</p>
              {l.meta && <span class="meta">{l.meta}</span>}
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}

export function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}
