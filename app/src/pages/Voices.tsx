import { useState } from "preact/hooks";
import { api, LANG_LABEL, url, type AvatarSlot, type Voice } from "../api";
import { Showcase } from "../components/Showcase";
import { notify, refreshVoices, select, selected, voices } from "../store";

const SLOTS: { slot: AvatarSlot; label: string; hint: string }[] = [
  { slot: "default", label: "通用形象", hint: "没有单独指定时，所有状态都用它" },
  { slot: "idle", label: "待机", hint: "" },
  { slot: "listening", label: "聆听", hint: "" },
  { slot: "speaking", label: "说话", hint: "" },
];
const AVATAR_ACCEPT = ".gif,.png,.apng,.webp,.webm,.mp4";

export function Voices() {
  const [creating, setCreating] = useState(false);
  const v = selected.value;
  return (
    <div class="page voices">
      <aside class="voice-list">
        <div class="list-head">
          <h2>声音库</h2>
          <button class="primary small" onClick={() => setCreating(true)}>
            新建
          </button>
        </div>
        {voices.value.map((x) => (
          <button key={x.id} class={`voice-item ${x.id === v?.id ? "on" : ""}`} onClick={() => select(x.id)}>
            <span class="vi-name">{x.name}</span>
            <span class="vi-meta">
              {LANG_LABEL[x.language]} · {x.trained ? "已训练" : x.ready ? "零样本" : "缺参考音频"}
              {x.default ? " · 默认" : ""}
            </span>
          </button>
        ))}
        {!voices.value.length && <p class="muted pad">还没有声音，点“新建”开始。</p>}
      </aside>
      <section class="voice-detail">{v ? <VoiceDetail key={v.id} v={v} /> : <p class="muted pad">选择或新建一个声音。</p>}</section>
      {creating && <CreateVoice onClose={() => setCreating(false)} />}
    </div>
  );
}

function VoiceDetail({ v }: { v: Voice }) {
  const [name, setName] = useState(v.name);
  const [speed, setSpeed] = useState(v.params.speed ?? 1);

  async function patch(body: Record<string, unknown>, msg = "已保存") {
    try {
      await api(`/v1/voices/${v.id}`, { method: "PATCH", json: body });
      await refreshVoices();
      notify(msg);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  async function upload(slot: AvatarSlot, f: File) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api(`/v1/voices/${v.id}/avatar/${slot}`, { method: "PUT", body: fd });
      await refreshVoices();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  async function replaceRef(f: File) {
    const fd = new FormData();
    fd.append("ref_audio", f);
    try {
      await api(`/v1/voices/${v.id}/ref`, { method: "POST", body: fd });
      await refreshVoices();
      notify("参考音频已更新，参考文本已自动识别");
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  async function remove() {
    if (!confirm(`删除声音「${v.name}」？\n文件夹会移到 data/trash，需要时可以找回。`)) return;
    try {
      await api(`/v1/voices/${v.id}`, { method: "DELETE" });
      await refreshVoices();
      notify("已移到回收站");
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  return (
    <div class="detail">
      <div class="detail-top">
        <div class="mini-show">
          <Showcase voice={v} compact />
        </div>
        <div class="detail-fields">
          <label class="field">
            <span>名称</span>
            <input value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} onBlur={() => name !== v.name && patch({ name })} />
          </label>
          <div class="field-row">
            <label class="field">
              <span>主要语种</span>
              <select value={v.language} onChange={(e) => patch({ language: (e.target as HTMLSelectElement).value })}>
                {["zh", "en", "ja"].map((l) => (
                  <option value={l}>{LANG_LABEL[l]}</option>
                ))}
              </select>
            </label>
            <label class="field">
              <span>基础模型</span>
              <select value={v.base} onChange={(e) => patch({ base: (e.target as HTMLSelectElement).value })}>
                <option value="v2Pro">v2Pro（推荐，训练用）</option>
                <option value="v2ProPlus">v2ProPlus</option>
                <option value="v5turbo">V5 turbo（零样本更像）</option>
              </select>
            </label>
          </div>
          <label class="field">
            <span>语速 {speed.toFixed(2)}×</span>
            <input type="range" min="0.6" max="1.6" step="0.05" value={speed} onInput={(e) => setSpeed(+(e.target as HTMLInputElement).value)} onChange={() => patch({ params: { speed } })} />
          </label>
          <div class="btn-row">
            {!v.default && <button onClick={() => patch({ default: true }, "已设为默认声音")}>设为默认</button>}
            <button class="danger" onClick={remove}>
              删除
            </button>
          </div>
        </div>
      </div>

      <h3>参考音频</h3>
      <div class="card">
        {v.ref ? (
          <>
            <audio controls src={url(`/v1/voices/${v.id}/ref/audio`, true) + `&d=${v.ref.duration}`} />
            <p class="ref-text">“{v.ref.text}”</p>
            <p class="muted">
              {v.ref.duration} 秒 · {LANG_LABEL[v.ref.lang] ?? v.ref.lang}
            </p>
          </>
        ) : (
          <p class="muted">还没有参考音频。</p>
        )}
        <FileButton accept="audio/*,video/*" label={v.ref ? "更换参考音频" : "上传参考音频"} onFile={replaceRef} />
      </div>

      <h3>角色形象</h3>
      <div class="card">
        <div class="seg inline">
          <button class={v.avatar_mode !== "free" ? "on" : ""} onClick={() => patch({ avatar_mode: "circle" })}>
            圆形头像（在圆环内）
          </button>
          <button class={v.avatar_mode === "free" ? "on" : ""} onClick={() => patch({ avatar_mode: "free" })}>
            透明立绘（圆环在身后）
          </button>
        </div>
        <div class="slots">
          {SLOTS.map(({ slot, label, hint }) => {
            const a = v.avatar[slot];
            return (
              <div class="slot">
                <div class="slot-preview">
                  {a ? a.kind === "video" ? <video src={url(a.url, true)} autoplay loop muted playsInline /> : <img src={url(a.url, true)} alt="" /> : <span>未设置</span>}
                </div>
                <strong>{label}</strong>
                {hint && <small class="muted">{hint}</small>}
                <div class="btn-row">
                  <FileButton accept={AVATAR_ACCEPT} label={a ? "更换" : "上传"} small onFile={(f) => upload(slot, f)} />
                  {a && (
                    <button class="small" onClick={() => api(`/v1/voices/${v.id}/avatar/${slot}`, { method: "DELETE" }).then(refreshVoices)}>
                      清除
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <p class="muted">支持 GIF、APNG、动态 WebP，以及短循环视频 WebM / MP4（WebM 可带透明通道）。</p>
      </div>

      <h3>使用授权</h3>
      <div class="card consent-card">
        <p>
          <strong>{v.consent.label}</strong> · 确认于 {new Date(v.consent.confirmed_at).toLocaleString()}
        </p>
        <p class="muted">{v.consent.notice}</p>
      </div>
    </div>
  );
}

export function FileButton({ accept, label, onFile, small }: { accept: string; label: string; onFile: (f: File) => void; small?: boolean }) {
  return (
    <label class={`file-btn ${small ? "small" : ""}`}>
      {label}
      <input
        type="file"
        accept={accept}
        onChange={(e) => {
          const f = (e.target as HTMLInputElement).files?.[0];
          if (f) onFile(f);
          (e.target as HTMLInputElement).value = "";
        }}
      />
    </label>
  );
}

function CreateVoice({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [lang, setLang] = useState("zh");
  const [consent, setConsent] = useState<"" | "self" | "authorized">("");
  const [ack, setAck] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [refText, setRefText] = useState("");
  const [busy, setBusy] = useState(false);
  const ok = name.trim() && consent && ack && file;

  async function submit() {
    if (!ok) return;
    setBusy(true);
    const fd = new FormData();
    fd.append("name", name.trim());
    fd.append("consent", consent);
    fd.append("consent_ack", "true");
    fd.append("language", lang);
    fd.append("ref_audio", file!);
    if (refText.trim()) fd.append("ref_text", refText.trim());
    try {
      const v = await api<Voice>("/v1/voices", { method: "POST", body: fd });
      await refreshVoices();
      select(v.id);
      notify(`已创建「${v.name}」`);
      onClose();
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="modal-back" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div class="modal" role="dialog" aria-modal="true" aria-label="新建声音">
        <h2>新建声音</h2>
        <label class="field">
          <span>名称</span>
          <input value={name} placeholder="例如：我的声音" onInput={(e) => setName((e.target as HTMLInputElement).value)} />
        </label>
        <label class="field">
          <span>主要语种</span>
          <select value={lang} onChange={(e) => setLang((e.target as HTMLSelectElement).value)}>
            <option value="zh">中文</option>
            <option value="en">English</option>
            <option value="ja">日本語</option>
          </select>
        </label>

        <fieldset class="consent">
          <legend>声音来源（必选）</legend>
          <label class={`choice ${consent === "self" ? "on" : ""}`}>
            <input type="radio" name="consent" checked={consent === "self"} onChange={() => setConsent("self")} />
            <span>
              <strong>本人声音</strong>
              <small>录音是我自己的声音</small>
            </span>
          </label>
          <label class={`choice ${consent === "authorized" ? "on" : ""}`}>
            <input type="radio" name="consent" checked={consent === "authorized"} onChange={() => setConsent("authorized")} />
            <span>
              <strong>已获授权的声音</strong>
              <small>声音属于他人，且对方已同意</small>
            </span>
          </label>
          <div class="notice">请告知被模仿的人并取得其单独同意。CharaVoice 只用于本人或已获授权的声音，生成的音频会标注为 AI 合成。</div>
          <label class="check">
            <input type="checkbox" checked={ack} onChange={(e) => setAck((e.target as HTMLInputElement).checked)} />
            我已知悉并确认
          </label>
        </fieldset>

        <label class="field">
          <span>参考音频（3-10 秒、只有这一个人说话、无背景音乐）</span>
          <FileButton accept="audio/*,video/*" label={file ? file.name : "选择文件"} onFile={setFile} />
        </label>
        <label class="field">
          <span>参考文本（可留空，自动识别）</span>
          <input value={refText} placeholder="录音里说的那句话" onInput={(e) => setRefText((e.target as HTMLInputElement).value)} />
        </label>
        <div class="btn-row end">
          <button onClick={onClose}>取消</button>
          <button class="primary" disabled={!ok || busy} onClick={submit}>
            {busy ? "正在处理…" : "创建"}
          </button>
        </div>
      </div>
    </div>
  );
}
