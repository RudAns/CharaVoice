import { useEffect, useState } from "preact/hooks";
import { api, EMOTION_LABEL, LANG_LABEL, url, type AvatarSlot, type Tweak, type Voice } from "../api";
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
                <option value="v5turbo">V5 turbo（零样本最像，推荐）</option>
                <option value="v2ProPlus">v2ProPlus（训练默认底模）</option>
                <option value="v2Pro">v2Pro</option>
                <option value="omnivoice">OmniVoice（零样本高相似，较慢，需安装）</option>
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

      <h3>声线调节</h3>
      <VoiceTweak key={v.id} v={v} />

      <BreathLib key={`b-${v.id}`} v={v} />

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

      <h3>情绪参考</h3>
      <EmotionRefs v={v} />

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

const TWEAK_SLIDERS: { key: keyof Tweak; label: string; min: number; max: number; step: number; fmt: (x: number) => string; hint: string }[] = [
  { key: "pitch", label: "音高", min: -6, max: 6, step: 0.5, fmt: (x) => `${x > 0 ? "+" : ""}${x} 半音`, hint: "整体更高或更低，音色不变；幅度越大越不自然，建议 ±3 以内" },
  { key: "formant", label: "年龄感", min: -20, max: 20, step: 1, fmt: (x) => `${x > 0 ? "+" : ""}${x}%`, hint: "向右更年轻、偏女声；向左更成熟、偏男声" },
  { key: "brightness", label: "明亮度", min: -6, max: 6, step: 0.5, fmt: (x) => `${x > 0 ? "+" : ""}${x} dB`, hint: "向右更明亮清脆，向左更温暖厚实" },
  { key: "breath", label: "气声", min: 0, max: 1, step: 0.05, fmt: (x) => `${Math.round(x * 100)}%`, hint: "加一点气息感，适合温柔、亲近的角色" },
];
const PRESETS: Record<string, { label: string; t: Tweak }> = {
  sweet: { label: "更甜", t: { pitch: 2, formant: 8, brightness: 2, breath: 0.15 } },
  deep: { label: "更低沉", t: { pitch: -3, formant: -8, brightness: -2, breath: 0 } },
  younger: { label: "更年轻", t: { pitch: 1.5, formant: 12, brightness: 1.5, breath: 0 } },
  mature: { label: "更成熟", t: { pitch: -1.5, formant: -10, brightness: -1, breath: 0.1 } },
};
const ZERO: Tweak = { pitch: 0, formant: 0, brightness: 0, breath: 0 };
const SAMPLE_LINE: Record<string, string> = {
  zh: "你好呀，今天想聊点什么？我一直在这里陪着你。",
  en: "Hi there! What would you like to talk about today? I'm right here with you.",
  ja: "こんにちは！今日は何について話しましょうか？",
};

function VoiceTweak({ v }: { v: Voice }) {
  const [t, setT] = useState<Tweak>({ ...ZERO, ...v.tweak });
  const [busy, setBusy] = useState(false);
  const saved = { ...ZERO, ...v.tweak };
  const dirty = (Object.keys(ZERO) as (keyof Tweak)[]).some((k) => Math.abs(t[k] - saved[k]) > 1e-6);
  const same = (a: Tweak, b: Tweak) => (Object.keys(ZERO) as (keyof Tweak)[]).every((k) => Math.abs(a[k] - b[k]) < 1e-6);

  async function preview() {
    setBusy(true);
    try {
      await api("/v1/audio/stop", { method: "POST" });
      await api("/v1/speak", { method: "POST", json: { text: SAMPLE_LINE[v.language] ?? SAMPLE_LINE.zh, voice: v.id, ...t } });
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  async function save(next: Tweak, msg: string) {
    try {
      await api(`/v1/voices/${v.id}`, { method: "PATCH", json: { tweak: next } });
      await refreshVoices();
      notify(msg);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  return (
    <div class="card tweak">
      <div class="preset-row" role="group" aria-label="声线预设">
        {Object.values(PRESETS).map((p) => (
          <button class={`pchip ${same(t, p.t) ? "on" : ""}`} onClick={() => setT(p.t)}>
            {p.label}
          </button>
        ))}
        <button class={`pchip ${same(t, ZERO) ? "on" : ""}`} onClick={() => setT(ZERO)}>
          原声
        </button>
      </div>
      <div class="tweak-grid">
        {TWEAK_SLIDERS.map((s) => (
          <label class="tweak-item">
            <span class="tweak-head">
              <strong>{s.label}</strong>
              <span class="tweak-val">{s.fmt(t[s.key])}</span>
            </span>
            <input
              type="range"
              min={s.min}
              max={s.max}
              step={s.step}
              value={t[s.key]}
              onInput={(e) => setT({ ...t, [s.key]: +(e.target as HTMLInputElement).value })}
              onDblClick={() => setT({ ...t, [s.key]: 0 })}
            />
            <small class="muted">{s.hint}</small>
          </label>
        ))}
      </div>
      <div class="btn-row">
        <button class="primary" disabled={busy} onClick={preview}>
          {busy ? "合成中…" : "试听"}
        </button>
        <button disabled={!dirty} onClick={() => save(t, "已保存为这个声音的默认声线")}>
          保存为默认
        </button>
        {dirty && (
          <button class="ghost" onClick={() => setT(saved)}>
            撤销修改
          </button>
        )}
        <span class="muted grow-text">在合成之后处理，不需要重新训练；Agent 调用时也可以临时传 pitch / formant / brightness / breath / preset。双击滑块回到 0。</span>
      </div>
    </div>
  );
}

function BreathLib({ v }: { v: Voice }) {
  const [n, setN] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ breaths: unknown[] }>(`/v1/voices/${v.id}/breaths`).then((r) => setN(r.breaths.length)).catch(() => setN(0));
  }, [v.id]);
  async function build() {
    setBusy(true);
    try {
      const r = await api<{ breaths: unknown[]; scanned: number }>(`/v1/voices/${v.id}/breaths/build`, { method: "POST" });
      setN(r.breaths.length);
      notify(r.breaths.length ? `从 ${r.scanned} 个素材里找到 ${r.breaths.length} 段呼吸` : `扫描了 ${r.scanned} 个素材，没有找到呼吸（修剪得很干净的录音里通常没有）`);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div class="card breath-line">
      <span>
        呼吸库：<strong>{n ?? "…"}</strong> 段
      </span>
      <span class="muted grow-text">自然模式会在较长的新句子前偶尔插入这个声音自己的呼吸。素材越像日常说话（不是修剪过的朗读），越容易找到。</span>
      <button disabled={busy} onClick={build}>
        {busy ? "正在扫描…" : "从素材提取"}
      </button>
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

interface Candidate {
  id: string;
  url: string;
  duration: number;
  text: string;
}

function CreateVoice({ onClose }: { onClose: () => void }) {
  const [mode, setMode] = useState<"upload" | "design">("upload");
  const [name, setName] = useState("");
  const [lang, setLang] = useState("zh");
  const [consent, setConsent] = useState<"" | "self" | "authorized">("");
  const [ack, setAck] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [refText, setRefText] = useState("");
  const [busy, setBusy] = useState(false);
  // design mode
  const [gender, setGender] = useState("female");
  const [age, setAge] = useState("young adult");
  const [pitch, setPitch] = useState("");
  const [whisper, setWhisper] = useState(false);
  const [accent, setAccent] = useState("");
  const [cands, setCands] = useState<Candidate[]>([]);
  const [pick, setPick] = useState("");
  const [gen, setGen] = useState(false);
  const designLang = lang === "en" ? "en" : "zh";
  const ok = name.trim() && ack && (mode === "upload" ? consent && file : pick);

  async function generate() {
    setGen(true);
    setCands([]);
    setPick("");
    try {
      const r = await api<{ candidates: Candidate[] }>("/v1/voices/design", {
        method: "POST",
        json: { language: designLang, gender: gender || null, age: age || null, pitch: pitch || null, whisper, accent: designLang === "en" && accent ? accent : null, n: 3 },
      });
      setCands(r.candidates);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setGen(false);
    }
  }

  async function submit() {
    if (!ok) return;
    setBusy(true);
    const fd = new FormData();
    fd.append("name", name.trim());
    fd.append("consent_ack", "true");
    if (mode === "upload") {
      fd.append("consent", consent);
      fd.append("language", lang);
      fd.append("ref_audio", file!);
      if (refText.trim()) fd.append("ref_text", refText.trim());
    } else {
      const c = cands.find((x) => x.id === pick)!;
      fd.append("consent", "designed");
      fd.append("language", designLang);
      fd.append("design_id", pick);
      fd.append("ref_text", c.text);
    }
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
        <div class="seg" role="tablist">
          <button role="tab" aria-selected={mode === "upload"} class={mode === "upload" ? "on" : ""} onClick={() => setMode("upload")}>
            用录音克隆
          </button>
          <button role="tab" aria-selected={mode === "design"} class={mode === "design" ? "on" : ""} onClick={() => setMode("design")}>
            用文字设计
          </button>
        </div>
        <label class="field">
          <span>名称</span>
          <input value={name} placeholder={mode === "upload" ? "例如：我的声音" : "例如：元气少女"} onInput={(e) => setName((e.target as HTMLInputElement).value)} />
        </label>
        <label class="field">
          <span>主要语种</span>
          <select value={lang} onChange={(e) => setLang((e.target as HTMLSelectElement).value)}>
            <option value="zh">中文</option>
            <option value="en">English</option>
            {mode === "upload" && <option value="ja">日本語</option>}
          </select>
        </label>

        {mode === "upload" ? (
          <>
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
            <p class="muted small-note">
              只有一段录音、想快速用：建好后在声音库把基础模型选成 OmniVoice（零样本最像）。有 10 分钟以上素材、要长期使用：去“训练”页训练，开口更快、跨语言更稳。
            </p>
          </>
        ) : (
          <>
            <div class="design-grid">
              <label class="field">
                <span>性别</span>
                <select value={gender} onChange={(e) => setGender((e.target as HTMLSelectElement).value)}>
                  <option value="female">女</option>
                  <option value="male">男</option>
                  <option value="">不指定</option>
                </select>
              </label>
              <label class="field">
                <span>年龄</span>
                <select value={age} onChange={(e) => setAge((e.target as HTMLSelectElement).value)}>
                  <option value="child">儿童</option>
                  <option value="teenager">少年</option>
                  <option value="young adult">青年</option>
                  <option value="middle-aged">中年</option>
                  <option value="elderly">老年</option>
                  <option value="">不指定</option>
                </select>
              </label>
              <label class="field">
                <span>音调</span>
                <select value={pitch} onChange={(e) => setPitch((e.target as HTMLSelectElement).value)}>
                  <option value="">不指定</option>
                  <option value="very low pitch">极低</option>
                  <option value="low pitch">偏低</option>
                  <option value="moderate pitch">适中</option>
                  <option value="high pitch">偏高</option>
                  <option value="very high pitch">极高</option>
                </select>
              </label>
              {designLang === "en" && (
                <label class="field">
                  <span>口音</span>
                  <select value={accent} onChange={(e) => setAccent((e.target as HTMLSelectElement).value)}>
                    <option value="">不指定</option>
                    <option value="american accent">美式</option>
                    <option value="british accent">英式</option>
                    <option value="australian accent">澳式</option>
                    <option value="canadian accent">加拿大</option>
                    <option value="indian accent">印度</option>
                  </select>
                </label>
              )}
            </div>
            <label class="check">
              <input type="checkbox" checked={whisper} onChange={(e) => setWhisper((e.target as HTMLInputElement).checked)} />
              耳语
            </label>
            <div class="btn-row">
              <button onClick={generate} disabled={gen}>
                {gen ? "正在生成（首次需启动 OmniVoice）…" : cands.length ? "换一批" : "生成 3 个候选"}
              </button>
            </div>
            {cands.length > 0 && (
              <div class="cands" role="radiogroup" aria-label="候选声音">
                {cands.map((c, i) => (
                  <label class={`cand ${pick === c.id ? "on" : ""}`}>
                    <input type="radio" name="cand" checked={pick === c.id} onChange={() => setPick(c.id)} />
                    <span class="cand-n">候选 {i + 1}</span>
                    <audio controls src={url(c.url, true)} />
                  </label>
                ))}
              </div>
            )}
            <p class="muted">同一段描述每次会生成不同的人，挑一个最喜欢的保存。需要已安装 OmniVoice，只支持中文和英文描述。</p>
            <label class="check">
              <input type="checkbox" checked={ack} onChange={(e) => setAck((e.target as HTMLInputElement).checked)} />
              这是合成的虚构声音，我不会用它冒充真实人物
            </label>
          </>
        )}
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

function EmotionRefs({ v }: { v: Voice }) {
  const [emotion, setEmotion] = useState("happy");
  async function add(f: File) {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("emotion", emotion);
    try {
      await api(`/v1/voices/${v.id}/refs`, { method: "POST", body: fd });
      await refreshVoices();
      notify(`已添加「${EMOTION_LABEL[emotion]}」参考音频`);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  return (
    <div class="card">
      <p class="muted">给声音再挂几段带情绪的 3-10 秒录音。Agent 调用时传 emotion（例如 happy），就会用对应的参考音频说话；没有的情绪用主参考音频。也可以在素材页的审阅区把某个片段直接设为情绪参考。</p>
      <div class="refs">
        {(v.refs ?? []).map((r) => (
          <div class="ref-row">
            <span class={`emo emo-${r.emotion}`}>{EMOTION_LABEL[r.emotion] ?? r.emotion}</span>
            <audio controls src={url(r.url, true)} />
            <span class="ref-line" title={r.text}>“{r.text}” · {r.duration}s</span>
            <button class="small" onClick={() => api(`/v1/voices/${v.id}/refs/${r.id}`, { method: "DELETE" }).then(refreshVoices)}>移除</button>
          </div>
        ))}
        {!(v.refs ?? []).length && <p class="muted">还没有情绪参考。</p>}
      </div>
      <div class="btn-row">
        <select value={emotion} onChange={(e) => setEmotion((e.target as HTMLSelectElement).value)} aria-label="情绪">
          {Object.entries(EMOTION_LABEL).filter(([k]) => k !== "neutral").map(([k, l]) => (
            <option value={k}>{l}</option>
          ))}
        </select>
        <FileButton accept="audio/*,video/*" label="上传这个情绪的参考音频" onFile={add} />
      </div>
    </div>
  );
}
