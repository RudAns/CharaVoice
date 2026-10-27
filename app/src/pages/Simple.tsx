// Simple mode (the home page): clone one person's Japanese voice in four steps on one screen.
//   1 录制  global hotkey records what the computer is playing (WASAPI loopback)
//   2 截取  trim the recording (suggested start/end, other speakers marked) and save clips into
//           material folders; a folder is one person or character
//   3 克隆  learn from the whole folder: zero-shot under a minute, v2ProPlus fine-tune above
//   4 朗读  read any Japanese text with the cloned voice; export WAV / MP3
import { invoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, conn, engine, onEvent, url } from "../api";
import { ClipEditor } from "../components/ClipEditor";
import { Transcripts } from "../components/Transcripts";
import { notify, pendingRec, remember } from "../store";
import { hotkeyLabel } from "./Settings";

interface Job {
  state: "running" | "done" | "error" | "cancelled";
  phase: "material" | "train" | "done";
  stage: string;
  progress: number;
  message: string;
  mode: string;
  elapsed: number;
}
interface Folder {
  id: string;
  name: string;
  clips: number;
  seconds: number;
  tags: string[];
  ready: boolean;
  trained: boolean;
  cloned: { at: string; mode: string; seconds: number } | null;
  dirty: boolean;
  job: Job | null;
  path: string;
  est: { zero_shot: number; train: number };
  read_speed?: number;
  doubtful?: number;
  deess?: "off" | "light" | "medium" | "strong";
  language?: "ja" | "zh" | "en";
  lang_auto?: boolean;
  mode?: "trained" | "omnivoice";
  models?: { id: string; label: string; note: string }[];
  model?: string | null;
  texts_edited?: boolean;
}
interface Clip {
  id: string;
  name: string;
  duration: number;
  tag: string;
  rec: string;
  ranges: [number, number][];
}
type Editing = { rec: string; replace?: { id: string; folder: string; ranges: [number, number][] } | null };

export const LANG_NAME: Record<string, string> = { ja: "日语", zh: "中文", en: "英语" };
const mmss = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const lsGet = (k: string) => {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
};

// ------------------------------------------------------------------ small dialogs
type Ask = { title: string; label: string; value: string; ok: string; resolve: (v: string | null) => void };
type Confirm = { title: string; body: preact.ComponentChildren; actions: { label: string; kind?: string; value: string }[]; resolve: (v: string | null) => void };

// The same component renders two pages: the material library (folders, recording, trimming,
// clips) and training (voice status, learning, transcript review, a small try-out box).
export function Simple({ view = "library" }: { view?: "library" | "train" }) {
  const [folders, setFolders] = useState<Folder[]>([]);
  const [cur, setCur] = useState<string>(lsGet("cv.folder") ?? "");
  const [clips, setClips] = useState<Clip[]>([]);
  const [consentAt, setConsentAt] = useState<string | null | undefined>(undefined);
  const [consentAsk, setConsentAsk] = useState(false);
  const [rec, setRec] = useState<{ on: boolean; seconds: number; level: number; simple: boolean }>({ on: false, seconds: 0, level: 0, simple: true });
  const [hotkey, setHotkey] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; clip: Clip } | null>(null);
  const [dragClip, setDragClip] = useState<{ clip: Clip; x: number; y: number; over: string | null } | null>(null);
  const [dropOver, setDropOver] = useState<string | null>(null);
  const [devices, setDevices] = useState<{ index: number; name: string; default: boolean }[]>([]);
  const [device, setDevice] = useState<number | null>(null);
  const [separate, setSeparate] = useState(true);
  const [showTr, setShowTr] = useState(false);
  const playing = useRef<HTMLAudioElement | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);

  const folder = folders.find((f) => f.id === cur) ?? null;
  const pick = (id: string) => {
    setCur(id);
    remember("cv.folder", id);
  };

  async function loadAll() {
    const r = await api<{ consent_at: string | null; folders: Folder[]; recording: any; settings: any }>("/v1/simple");
    setFolders(r.folders);
    setConsentAt(r.consent_at);
    setDevice(r.settings.device ?? null);
    setSeparate(r.settings.separate !== false);
    if (r.recording?.recording) setRec((x) => ({ ...x, on: true, seconds: r.recording.seconds, simple: r.recording.simple }));
    if (!r.folders.some((f) => f.id === cur) && r.folders.length) pick(r.folders[0].id);
  }
  async function loadClips(id = cur) {
    if (!id) return setClips([]);
    try {
      const d = await api<Folder & { items: Clip[] }>(`/v1/simple/folders/${id}`);
      setClips(d.items);
    } catch {
      setClips([]);
    }
  }

  useEffect(() => {
    loadAll().catch((e) => notify(e.message, "error"));
    invoke<string>("record_hotkey").then(setHotkey, () => setHotkey(""));
    api<{ devices: any[] }>("/v1/record/devices").then((r) => setDevices(r.devices), () => {});
    const un = tauriListen("simple-consent-needed", () => setConsentAsk(true));
    return () => {
      un.then((f) => f());
    };
  }, []);
  useEffect(() => {
    loadClips();
  }, [cur]);
  useEffect(() => {
    if (pendingRec.value) {
      setEditing({ rec: pendingRec.value.rec });
      pendingRec.value = null;
    }
  }, [pendingRec.value]);
  useEffect(
    () =>
      onEvent((ev) => {
        if (ev.type === "simple_changed" || ev.type === "voices_changed") {
          loadAll().catch(() => {});
          if (!ev.folder || ev.folder === cur) loadClips();
        } else if (ev.type === "simple_job") {
          setFolders((fs) => fs.map((f) => (f.id === ev.voice ? { ...f, job: ev } : f)));
          if (ev.state !== "running") {
            loadAll().catch(() => {});
            if (ev.state === "error") notify(ev.message, "error");
            else if (ev.state === "done") notify(ev.message);
          }
        } else if (ev.type === "rec_start") setRec({ on: true, seconds: 0, level: 0, simple: ev.voice === "__simple__" });
        else if (ev.type === "rec_level") setRec((r) => ({ ...r, on: true, seconds: ev.seconds, level: ev.level }));
        else if (ev.type === "rec_stop") {
          setRec((r) => ({ ...r, on: false, level: 0 }));
          if (ev.error) notify(ev.error, "error");
        } else if (ev.type === "simple_recorded") {
          // a new recording replaces an unsaved one that is still open in the editor
          setEditing((cur) => {
            if (cur && !cur.replace && cur.rec !== ev.rec) api(`/v1/simple/recordings/${cur.rec}`, { method: "DELETE" }).catch(() => {});
            return { rec: ev.rec };
          });
          pendingRec.value = null;
        }
      }),
    [cur],
  );

  // files dropped onto the window: import into the folder under the cursor (or the current one)
  useEffect(() => {
    const un = getCurrentWebview().onDragDropEvent((e) => {
      const p = e.payload as any;
      const at = p.position ? folderAt(p.position.x / devicePixelRatio, p.position.y / devicePixelRatio) : null;
      if (p.type === "over" || p.type === "enter") setDropOver(at ?? cur ?? null);
      else if (p.type === "leave") setDropOver(null);
      else if (p.type === "drop") {
        setDropOver(null);
        const to = at ?? cur;
        const paths = (p.paths as string[]).filter((x) => /\.(wav|mp3|flac|m4a|ogg|aac|opus|mp4|mkv|webm)$/i.test(x));
        if (!to) return notify("先新建一个素材文件夹", "error");
        if (!paths.length) return notify("只能导入音频或视频文件", "error");
        notify(`正在导入 ${paths.length} 个文件…`);
        api<{ clips: Clip[]; errors: string[] }>(`/v1/simple/folders/${to}/import-paths`, { method: "POST", json: { paths } }).then(
          (r) => {
            pick(to);
            notify(`已导入 ${r.clips.length} 个片段` + (r.errors.length ? `；${r.errors.join("；")}` : ""), r.errors.length ? "error" : "info");
          },
          (err) => notify(err.message, "error"),
        );
      }
    });
    return () => {
      un.then((f) => f());
    };
  }, [cur]);

  function folderAt(x: number, y: number): string | null {
    const el = document.elementFromPoint(x, y)?.closest("[data-folder]") as HTMLElement | null;
    return el?.dataset.folder ?? null;
  }

  // ---------- dialogs
  const askName = (title: string, value = "", ok = "确定", label = "名称") =>
    new Promise<string | null>((resolve) => setAsk({ title, label, value, ok, resolve }));
  const askConfirm = (title: string, body: preact.ComponentChildren, actions: Confirm["actions"]) =>
    new Promise<string | null>((resolve) => setConfirm({ title, body, actions, resolve }));

  async function ensureConsent(): Promise<boolean> {
    if (consentAt) return true;
    setConsentAsk(true);
    return false;
  }
  async function newFolder(): Promise<string | null> {
    if (!(await ensureConsent())) return null;
    const name = await askName("新建素材文件夹", "", "下一步", "人物或角色名");
    if (!name?.trim()) return null;
    const lang = await askConfirm(
      `“${name.trim()}”说什么语言？`,
      <p class="muted">素材按这个语言转写和训练，克隆出的声音说这个语言最自然。不确定就选“自动识别”，用第一段保存的片段来判断，之后也可以改。</p>,
      [
        { label: "自动识别", value: "auto" },
        { label: "日语", value: "ja" },
        { label: "中文", value: "zh" },
        { label: "英语", value: "en" },
      ],
    );
    if (!lang) return null;
    try {
      const f = await api<Folder>("/v1/simple/folders", { method: "POST", json: { name: name.trim(), language: lang === "auto" ? null : lang } });
      setFolders((fs) => [...fs, f]);
      pick(f.id);
      return f.id;
    } catch (e: any) {
      notify(e.message, "error");
      return null;
    }
  }
  async function changeLanguage(f: Folder) {
    const lang = await askConfirm(
      `“${f.name}”的素材是什么语言？`,
      <p class="muted">改了以后点“重新学习”，素材会按新语言重新转写和训练。</p>,
      [
        { label: "取消", value: "" },
        { label: "日语", value: "ja" },
        { label: "中文", value: "zh" },
        { label: "英语", value: "en" },
      ],
    );
    if (!lang || lang === f.language) return;
    await api(`/v1/simple/folders/${f.id}`, { method: "PATCH", json: { language: lang } }).catch((e) => notify(e.message, "error"));
    loadAll();
  }
  async function renameFolder(f: Folder) {
    const name = await askName("重命名文件夹", f.name, "保存");
    if (!name?.trim() || name.trim() === f.name) return;
    await api(`/v1/simple/folders/${f.id}`, { method: "PATCH", json: { name: name.trim() } }).catch((e) => notify(e.message, "error"));
    loadAll();
  }
  async function deleteFolder(f: Folder) {
    const ok = await askConfirm(
      `删除“${f.name}”？`,
      <p>文件夹里的 {f.clips} 个片段（{mmss(f.seconds)}）和克隆出的声线会移到数据目录的 trash 文件夹，可以手动恢复。</p>,
      [{ label: "取消", value: "" }, { label: "删除", kind: "danger", value: "yes" }],
    );
    if (ok !== "yes") return;
    try {
      await api(`/v1/simple/folders/${f.id}`, { method: "DELETE" });
      if (cur === f.id) pick("");
      loadAll();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  // ---------- recording
  async function toggleRec() {
    if (!rec.on && !(await ensureConsent())) return;
    try {
      await api("/v1/simple/record/toggle", { method: "POST", json: { device } });
    } catch (e: any) {
      if (e.code === "consent_required") setConsentAsk(true);
      else notify(e.message, "error");
    }
  }
  async function giveConsent() {
    const r = await api<{ consent_at: string }>("/v1/simple/consent", { method: "POST" });
    setConsentAt(r.consent_at);
    setConsentAsk(false);
  }

  // ---------- clips
  function playClip(c: Clip) {
    playing.current?.pause();
    if (playingId === c.id) return setPlayingId(null);
    const a = new Audio(url(`/v1/simple/folders/${cur}/clips/${c.id}/audio`, true));
    a.onended = () => setPlayingId(null);
    a.play();
    playing.current = a;
    setPlayingId(c.id);
  }
  async function moveClip(c: Clip, to: string) {
    if (to === cur) return;
    try {
      await api(`/v1/simple/folders/${cur}/clips/move`, { method: "POST", json: { ids: [c.id], to } });
      notify(`已移到“${folders.find((f) => f.id === to)?.name}”`);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function deleteClip(c: Clip) {
    const ok = await askConfirm(`删除“${c.name}”？`, <p>片段文件会被删除。</p>, [
      { label: "取消", value: "" },
      { label: "删除", kind: "danger", value: "yes" },
    ]);
    if (ok === "yes") api(`/v1/simple/folders/${cur}/clips/delete`, { method: "POST", json: { ids: [c.id] } }).catch((e) => notify(e.message, "error"));
  }
  async function renameClip(c: Clip) {
    const name = await askName("重命名片段", c.name, "保存");
    if (name?.trim()) api(`/v1/simple/folders/${cur}/clips/${c.id}`, { method: "PATCH", json: { name } }).catch((e) => notify(e.message, "error"));
  }
  async function tagClip(c: Clip) {
    const tag = await askName("片段标签", c.tag, "保存", "标签（如情绪：开心、生气；留空清除）");
    if (tag !== null) api(`/v1/simple/folders/${cur}/clips/${c.id}`, { method: "PATCH", json: { tag } }).catch((e) => notify(e.message, "error"));
  }
  async function importFiles(files: FileList | null) {
    if (!files?.length || !cur) return;
    const fd = new FormData();
    for (const f of Array.from(files)) fd.append("files", f, f.name);
    notify(`正在导入 ${files.length} 个文件…`);
    try {
      const r = await api<{ clips: Clip[] }>(`/v1/simple/folders/${cur}/import`, { method: "POST", body: fd });
      notify(`已导入 ${r.clips.length} 个片段`);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  // pointer drag of a clip row onto a folder (HTML5 drag and drop is taken by the window's file drop)
  function startDrag(e: PointerEvent, c: Clip) {
    if ((e.target as HTMLElement).closest("button, input, select") || e.button !== 0) return;
    const x0 = e.clientX, y0 = e.clientY;
    let active = false;
    const move = (ev: PointerEvent) => {
      if (!active && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 6) return;
      active = true;
      setDragClip({ clip: c, x: ev.clientX, y: ev.clientY, over: folderAt(ev.clientX, ev.clientY) });
    };
    const up = (ev: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setDragClip(null);
      if (!active) return;
      const to = folderAt(ev.clientX, ev.clientY);
      if (to && to !== cur) moveClip(c, to);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // ---------- clone
  async function retrain() {
    if (!folder) return;
    const r = await askConfirm(
      "减噪重训",
      <>
        <p>
          去掉伴奏特别重的句子、对其余句子轻度降噪，再训练一次；声码器每一轮都保存下来逐轮试听比较，自动选“底噪最少、又不失音色”的那一轮。
          会占用显卡大约 <strong>5 分钟</strong>，期间朗读暂停，可以随时取消。
        </p>
        <p class="muted">训完会和现在的模型一起比较，只保留选出的那一个，其余的检查点自动删除。</p>
      </>,
      [
        { label: "取消", value: "" },
        { label: "开始重训（约 5 分钟）", kind: "primary", value: "go" },
      ],
    );
    if (r !== "go") return;
    try {
      const job = await api<Job>(`/v1/simple/folders/${folder.id}/retrain`, { method: "POST" });
      setFolders((fs) => fs.map((f) => (f.id === folder.id ? { ...f, job } : f)));
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function useModel(id: string) {
    if (!folder) return;
    try {
      await api(`/v1/simple/folders/${folder.id}/models/use`, { method: "POST", json: { id } });
      notify("已切换模型版本");
      loadAll();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  async function clone() {
    if (!folder) return;
    const secs = folder.seconds;
    let train = false;
    if (secs >= 60) {
      const r = await askConfirm(
        "开始学习这个声线",
        <>
          <p>
            “{folder.name}”共有 <strong>{mmss(secs)}</strong> 素材，够训练了。训练用 v2ProPlus 底模，会占用显卡大约 <strong>{folder.est.train} 分钟</strong>；
            训练期间语音合成暂停，可以随时取消。
          </p>
          <p class="muted">也可以只做零样本克隆（约 {folder.est.zero_shot} 分钟，几乎不占显卡），以后再训练。</p>
        </>,
        [
          { label: "取消", value: "" },
          { label: "只做零样本", value: "zs" },
          { label: `开始训练（约 ${folder.est.train} 分钟）`, kind: "primary", value: "train" },
        ],
      );
      if (!r) return;
      train = r === "train";
    }
    try {
      const job = await api<Job>(`/v1/simple/folders/${folder.id}/clone`, { method: "POST", json: { train, separate } });
      setFolders((fs) => fs.map((f) => (f.id === folder.id ? { ...f, job } : f)));
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  const job = folder?.job?.state === "running" ? folder.job : null;
  const total = folder?.seconds ?? 0;
  const status = !folder
    ? ""
    : job
      ? `学习中 ${Math.round(job.progress * 100)}%`
      : folder.trained
        ? "已就绪 · 训练版"
        : folder.ready
          ? "已就绪 · 零样本"
          : "未训练";

  return (
    <div class="page simple">
      <header class="sm-head">
        <h2>{view === "library" ? "素材库" : "训练"}</h2>
        <span class="muted">
          {view === "library" ? "一个文件夹就是一个要克隆的人或角色：录制、截取、整理声音片段" : "用文件夹里的全部片段学习声线（日语、中文、英语按文件夹设置），训练好的声音可以接入 Agent"}
        </span>
      </header>
      <div class={`sm-grid ${view} ${editing ? "editing" : ""}`}>
        {/* ---------------- folders */}
        <aside class="sm-folders card">
          <div class="list-head">
            <strong>{view === "library" ? "素材文件夹" : "声音"}</strong>
            {view === "library" && <button class="small" onClick={newFolder}>＋ 新建</button>}
          </div>
          <div class="sm-flist">
            {folders.length === 0 && <p class="muted sm-empty">一个文件夹就是一个要克隆的人或角色。先新建一个，再录制他（她）的声音。</p>}
            {folders.map((f) => {
              const fj = f.job?.state === "running" ? f.job : null;
              return (
                <div
                  key={f.id}
                  data-folder={f.id}
                  class={`sm-folder ${f.id === cur ? "on" : ""} ${dragClip?.over === f.id || dropOver === f.id ? "drop" : ""}`}
                  onClick={() => pick(f.id)}
                >
                  <div class="sf-top">
                    <span class="sf-name">{f.name}</span>
                    <span class="sf-lang" title={f.lang_auto ? "第一段片段保存后自动识别" : "素材语言"}>{f.lang_auto ? "待识别" : LANG_NAME[f.language ?? "ja"]}</span>
                    <span class={`sf-state ${fj ? "run" : f.ready ? "trained" : ""}`}>
                      {fj ? `训练中 ${Math.round(fj.progress * 100)}%` : f.ready ? "已就绪" : "未训练"}
                    </span>
                  </div>
                  <div class="sf-meta">
                    {f.clips} 段 · {mmss(f.seconds)}
                    {view === "train" && f.cloned && ` · ${f.cloned.at.slice(5, 16).replace("T", " ")} 训练`}
                    {f.dirty && <span class="sf-dirty"> · 有新素材</span>}
                  </div>
                  {view === "library" && f.id === cur && (
                    <div class="sf-actions">
                      <button class="small" onClick={(e) => { e.stopPropagation(); renameFolder(f); }}>重命名</button>
                      <button class="small" onClick={(e) => { e.stopPropagation(); changeLanguage(f); }}>语言</button>
                      <button class="small" onClick={(e) => { e.stopPropagation(); api(`/v1/simple/folders/${f.id}/open`, { method: "POST" }).catch((er) => notify(er.message, "error")); }}>打开</button>
                      <button class="small danger" onClick={(e) => { e.stopPropagation(); deleteFolder(f); }}>删除</button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {view === "library" && <p class="muted sm-tip">可以把 wav / mp3 文件直接拖到文件夹上导入；把片段拖到另一个文件夹即可移动。</p>}
        </aside>

        {/* ---------------- 1 record */}
        {view === "library" && (
        <section class="card sm-step sm-rec">
          <div class="step-no">1</div>
          <div class="sm-rec-body">
            <div class="step-title">录制电脑声音</div>
            <div class="muted">录下游戏、视频、直播里正在播放的声音</div>
            <div class="sm-hotkey">
              {hotkey ? <kbd>{hotkeyLabel(hotkey)}</kbd> : <span class="muted">{hotkey === "" ? "快捷键被其他程序占用，请用按钮" : "…"}</span>}
              <span class="muted">开始 / 停止，窗口最小化也能用</span>
              {devices.length > 1 && (
                <select
                  class="sm-dev"
                  title="录制哪个输出设备的声音"
                  value={device ?? ""}
                  onChange={(e) => {
                    const v = (e.target as HTMLSelectElement).value;
                    const d = v === "" ? null : +v;
                    setDevice(d);
                    api("/v1/settings", { method: "PATCH", json: { simple: { device: d } } }).catch(() => {});
                  }}
                >
                  <option value="">默认输出设备</option>
                  {devices.map((d) => (
                    <option value={d.index}>{d.name}</option>
                  ))}
                </select>
              )}
            </div>
          </div>
          <div class="sm-rec-ctl">
            <div class={`rec-dot ${rec.on ? "on" : ""}`} />
            <span class="rec-clock">{mmss(rec.seconds)}</span>
            <div class="meter sm-meter">
              <div class="meter-fill" style={{ width: `${Math.min(100, rec.level * 140)}%` }} />
            </div>
            <button class={rec.on ? "danger" : "primary rec-btn"} disabled={rec.on && !rec.simple} onClick={toggleRec}>
              {rec.on ? "■ 停止" : "● 开始录制"}
            </button>
          </div>
        </section>
        )}

        {/* ---------------- 2 trim / clips */}
        {view === "library" && (
        <section class="card sm-step sm-clips">
          <div class="step-head">
            <div class="step-no">2</div>
            <div class="step-title">{editing ? "截取" : `片段${folder ? ` · ${folder.name}` : ""}`}</div>
            <div class="grow" />
            {!editing && folder?.cloned && (
              <button class="small" onClick={() => setShowTr(true)} title="查看、修改切出的每句转写">
                转写{folder.doubtful ? <span class="sm-doubt-badge">{folder.doubtful} 句可能有误</span> : null}
              </button>
            )}
            {!editing && folder && (
              <label class="file-btn small">
                导入文件
                <input type="file" multiple accept="audio/*,video/*" onChange={(e) => { importFiles((e.target as HTMLInputElement).files); (e.target as HTMLInputElement).value = ""; }} />
              </label>
            )}
          </div>
          {editing ? (
            <ClipEditor
              rec={editing.rec}
              folder={cur || null}
              folders={folders}
              replace={editing.replace}
              onNewFolder={newFolder}
              onSaved={(f) => {
                pick(f);
                setEditing(null);
                loadClips(f);
              }}
              onClose={(discard) => {
                if (discard) api(`/v1/simple/recordings/${editing.rec}`, { method: "DELETE" }).catch(() => {});
                setEditing(null);
              }}
            />
          ) : !folder ? (
            <p class="muted sm-empty">新建一个素材文件夹后，录下的声音截取完就保存在这里。</p>
          ) : clips.length === 0 ? (
            <p class="muted sm-empty">
              还没有片段。播放这个人说话的视频或游戏，按 {hotkey ? <kbd>{hotkeyLabel(hotkey)}</kbd> : "录制按钮"} 开始录，再按一次停止，然后截取保存。
            </p>
          ) : (
            <div class="sm-cliplist">
              {clips.map((c) => (
                <div
                  key={c.id}
                  class={`sm-clip ${dragClip?.clip.id === c.id ? "dragging" : ""}`}
                  onPointerDown={(e) => startDrag(e as any, c)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setMenu({ x: e.clientX, y: e.clientY, clip: c });
                  }}
                >
                  <button class="play" title="试听" onClick={() => playClip(c)}>{playingId === c.id ? "■" : "▶"}</button>
                  <span class="sc-name" title={c.name} onDblClick={() => renameClip(c)}>{c.name}</span>
                  {c.tag && <span class="sc-tag" onClick={() => tagClip(c)}>{c.tag}</span>}
                  <span class="dur">{c.duration.toFixed(1)} 秒</span>
                  <button class="small" title="重新裁剪" onClick={() => setEditing({ rec: c.rec, replace: { id: c.id, folder: cur, ranges: c.ranges } })}>裁剪</button>
                  <button class="small" title="更多：移动、标签、重命名、删除" onClick={(e) => setMenu({ x: (e as MouseEvent).clientX, y: (e as MouseEvent).clientY, clip: c })}>⋯</button>
                </div>
              ))}
            </div>
          )}
          {!editing && folder && (
            <div class="sm-total">
              <div class="sm-bar">
                <div class="sm-bar-fill" style={{ width: `${Math.min(100, (total / 600) * 100)}%` }} />
                <span class="mark" style={{ left: "10%" }}><i />1 分钟</span>
                <span class="mark" style={{ left: "30%" }}><i />3 分钟</span>
                <span class="mark end"><i />10 分钟</span>
              </div>
              <div class="sm-total-line">
                合计 <strong>{mmss(total)}</strong>（{clips.length} 段）
                <span class="muted">
                  {total < 60 ? "· 不到 1 分钟会用零样本克隆；1 分钟以上可以训练，3–10 分钟效果更好" : total < 180 ? "· 已可训练；3–10 分钟效果更好" : "· 素材充足"}
                </span>
              </div>
            </div>
          )}
        </section>
        )}

        {/* ---------------- 3 train */}
        {view === "train" && (
        <section class="card sm-step sm-clone">
          <div class="step-head">
            <div class="step-title">{folder ? folder.name : "训练"}</div>
            <div class="grow" />
            {folder && <span class={`sf-state ${job ? "run" : folder.ready ? "trained" : ""}`}>{status}</span>}
          </div>
          {folder && (
            <div class="tr-facts">
              <span>素材 <strong>{mmss(folder.seconds)}</strong>（{folder.clips} 段）</span>
              {folder.cloned && <span>有效语音 <strong>{mmss(folder.cloned.seconds)}</strong></span>}
              {folder.cloned && <span>上次训练 <strong>{folder.cloned.at.slice(0, 16).replace("T", " ")}</strong></span>}
              <span>语言 <strong>{folder.lang_auto ? "待识别" : LANG_NAME[folder.language ?? "ja"]}</strong></span>
              {folder.ready && <span>方式 <strong>{folder.trained ? "训练（v2ProPlus）" : "零样本"}</strong></span>}
            </div>
          )}
          {folder && folder.seconds === 0 && <p class="muted">这个文件夹还没有片段，先到“素材库”录制、截取几段声音。</p>}
          {!folder ? (
            <p class="muted">选择一个素材文件夹。</p>
          ) : job ? (
            <div class="sm-job">
              <div class="sm-job-line">
                <span>{job.stage}</span>
                <span class="muted">{eta(job)}</span>
              </div>
              <div class="progress">
                <div style={{ width: `${Math.round(job.progress * 100)}%` }} />
              </div>
              <button class="small" onClick={() => api(`/v1/simple/folders/${folder.id}/clone/cancel`, { method: "POST" })}>取消</button>
            </div>
          ) : (
            <>
              <p class="muted sm-clone-note">
                用文件夹里全部片段学习：先分离人声、切句、转写，筛掉别人的声音；不到 1 分钟做零样本克隆，1 分钟以上可以训练 v2ProPlus。
                {folder.cloned && ` 上次：${folder.cloned.mode === "trained" ? "训练" : "零样本"}，${mmss(folder.cloned.seconds)} 有效语音。`}
              </p>
              {folder.job?.state === "error" && <p class="error">{folder.job.message}</p>}
              {(folder.models?.length ?? 0) > 1 && (
                <div class="sm-speed">
                  <span>模型版本</span>
                  <div class="seg">
                    {folder.models!.map((m) => (
                      <button class={folder.model === m.id ? "on" : ""} title={m.note} onClick={() => useModel(m.id)}>
                        {m.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {folder.texts_edited && <p class="sm-warn">转写改过了：点“重新学习”让训练用上修改后的文字（参考文字已经生效）。</p>}
              {folder.dirty && !folder.texts_edited && <p class="sm-warn">素材有变化：点“重新学习”把新片段学进去。</p>}
              <div class="btn-row">
                <button class="primary" disabled={!folder.clips} onClick={clone}>
                  {!folder.cloned ? "克隆这个声线" : folder.texts_edited ? "重新学习（转写已改）" : folder.dirty ? "重新学习（有新片段）" : "重新学习"}
                </button>
                {folder.cloned && view === "train" && (
                  <button onClick={() => setShowTr(true)} title="查看、修改每句的转写和参考文字">
                    转写{folder.doubtful ? <span class="sm-doubt-badge">{folder.doubtful} 句可能有误</span> : null}
                  </button>
                )}
                {folder.trained && (
                  <button onClick={retrain} title="去掉伴奏重的句子再训练一次，声码器少训几轮，减少底噪和机械的齿音">
                    减噪重训
                  </button>
                )}
                <label class="check slim" title="录到的声音里有背景音乐、音效时保持打开">
                  <input type="checkbox" checked={separate} onChange={(e) => { const v = (e.target as HTMLInputElement).checked; setSeparate(v); api("/v1/settings", { method: "PATCH", json: { simple: { separate: v } } }).catch(() => {}); }} />
                  去除背景音乐
                </label>
              </div>
            </>
          )}
        </section>
        )}

        {/* ---------------- 4 try it */}
        {view === "train" && <ReadCard folder={folder} busy={!!job} mini />}
      </div>

      {menu && (
        <div class="ctx-back" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null); }}>
          <div class="ctx" style={{ left: Math.min(menu.x, innerWidth - 200), top: Math.min(menu.y, innerHeight - 260) }} onClick={(e) => e.stopPropagation()}>
            <button onClick={() => { setMenu(null); setEditing({ rec: menu.clip.rec, replace: { id: menu.clip.id, folder: cur, ranges: menu.clip.ranges } }); }}>重新裁剪</button>
            <button onClick={() => { setMenu(null); renameClip(menu.clip); }}>重命名</button>
            <button onClick={() => { setMenu(null); tagClip(menu.clip); }}>标签…</button>
            {folders.filter((f) => f.id !== cur).length > 0 && <div class="ctx-sep">移动到</div>}
            {folders.filter((f) => f.id !== cur).map((f) => (
              <button onClick={() => { setMenu(null); moveClip(menu.clip, f.id); }}>　{f.name}</button>
            ))}
            <div class="ctx-sep" />
            <button class="danger-text" onClick={() => { setMenu(null); deleteClip(menu.clip); }}>删除</button>
          </div>
        </div>
      )}
      {dragClip && (
        <div class="drag-ghost" style={{ left: dragClip.x + 12, top: dragClip.y + 8 }}>
          {dragClip.clip.name} · {dragClip.clip.duration.toFixed(1)} 秒{dragClip.over && dragClip.over !== cur ? ` → ${folders.find((f) => f.id === dragClip.over)?.name}` : ""}
        </div>
      )}
      {showTr && folder && (
        <Transcripts
          folder={folder.id}
          name={folder.name}
          onClose={() => {
            setShowTr(false);
            loadAll().catch(() => {});
          }}
        />
      )}
      {consentAsk && <ConsentModal onOk={giveConsent} onCancel={() => setConsentAsk(false)} />}
      {ask && <AskModal ask={ask} onDone={(v) => { ask.resolve(v); setAsk(null); }} />}
      {confirm && (
        <div class="modal-back">
          <div class="modal" role="dialog" aria-modal="true">
            <h2>{confirm.title}</h2>
            {confirm.body}
            <div class="btn-row end">
              {confirm.actions.map((a) => (
                <button class={a.kind ?? ""} onClick={() => { confirm.resolve(a.value || null); setConfirm(null); }}>{a.label}</button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function eta(j: Job): string {
  if (j.progress < 0.05 || j.elapsed < 10) return `已用 ${mmss(j.elapsed)}`;
  const left = (j.elapsed * (1 - j.progress)) / j.progress;
  return `已用 ${mmss(j.elapsed)} · 预计还要 ${left < 60 ? "不到 1 分钟" : `约 ${Math.ceil(left / 60)} 分钟`}`;
}

function ConsentModal({ onOk, onCancel }: { onOk: () => void; onCancel: () => void }) {
  const [ok, setOk] = useState(false);
  return (
    <div class="modal-back">
      <div class="modal" role="dialog" aria-modal="true">
        <h2>使用前确认（只需一次）</h2>
        <p>简易模式会录制电脑播放的声音，并用它克隆说话人的声线。</p>
        <label class="check">
          <input type="checkbox" checked={ok} onChange={(e) => setOk((e.target as HTMLInputElement).checked)} />
          我只录制、克隆本人或已获授权的声音，不会用克隆的声音冒充他人。
        </label>
        <p class="muted">导出的音频会带上“AI 生成”标记和不可听水印。</p>
        <div class="btn-row end">
          <button onClick={onCancel}>取消</button>
          <button class="primary" disabled={!ok} onClick={onOk}>确认</button>
        </div>
      </div>
    </div>
  );
}

function AskModal({ ask, onDone }: { ask: Ask; onDone: (v: string | null) => void }) {
  const [v, setV] = useState(ask.value);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);
  return (
    <div class="modal-back">
      <div class="modal" role="dialog" aria-modal="true">
        <h2>{ask.title}</h2>
        <label class="field">
          <span>{ask.label}</span>
          <input ref={input} value={v} onInput={(e) => setV((e.target as HTMLInputElement).value)} onKeyDown={(e) => { if (e.key === "Enter") onDone(v); if (e.key === "Escape") onDone(null); }} />
        </label>
        <div class="btn-row end">
          <button onClick={() => onDone(null)}>取消</button>
          <button class="primary" onClick={() => onDone(v)}>{ask.ok}</button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ 4 read aloud
/** Split Japanese prose into sentences; very short ones join the next, very long ones split at 、. */
export function sentences(text: string): string[] {
  const raw = text.replace(/\r/g, "").split(/(?<=[。！？!?…\n；])|(?<=[.;])\s+/).map((s) => s.trim()).filter(Boolean);
  const out: string[] = [];
  let carry = "";
  for (const s of raw) {
    const t = carry + s;
    if (t.replace(/[\s。、！？!?「」『』…]/g, "").length < 6) {
      carry = t;
      continue;
    }
    carry = "";
    if (t.length <= 120) out.push(t);
    else {
      let cur = "";
      for (const p of t.split(/(?<=[、，,])/)) {
        if ((cur + p).length > 120 && cur) {
          out.push(cur);
          cur = "";
        }
        cur += p;
      }
      if (cur) out.push(cur);
    }
  }
  if (carry) out.length ? (out[out.length - 1] += carry) : out.push(carry);
  return out;
}

const TRY_SAMPLES: Record<string, string> = {
  ja: "こんにちは。今日はいい天気ですね、少し散歩に行きませんか。",
  zh: "你好，今天天气真不错，要不要一起出去走走？",
  en: "Hello there. It's a lovely day today, would you like to go for a walk?",
};
const SAMPLE = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。";

/** Read a streamed WAV response (header with unknown length, then 16-bit PCM) chunk by chunk. */
async function* pcmChunks(r: Response): AsyncGenerator<{ sr: number; pcm: Float32Array<ArrayBuffer> }> {
  const reader = r.body!.getReader();
  let head = new Uint8Array(0);
  let sr = 0;
  let carry: number | null = null; // odd byte left over from the previous chunk
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    let bytes = value;
    if (!sr) {
      const h = new Uint8Array(head.length + bytes.length);
      h.set(head);
      h.set(bytes, head.length);
      if (h.length < 44) {
        head = h;
        continue;
      }
      sr = new DataView(h.buffer).getUint32(24, true);
      bytes = h.subarray(44);
    }
    if (carry !== null) {
      const c = new Uint8Array(bytes.length + 1);
      c[0] = carry;
      c.set(bytes, 1);
      bytes = c;
      carry = null;
    }
    if (bytes.length % 2) {
      carry = bytes[bytes.length - 1];
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    if (!bytes.length) continue;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const pcm = new Float32Array(bytes.length / 2);
    for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(i * 2, true) / 32768;
    yield { sr, pcm };
  }
}

function ReadCard({ folder, busy, mini = false }: { folder: Folder | null; busy: boolean; mini?: boolean }) {
  const lang = folder?.language ?? "ja";
  const [text, setText] = useState(() => (mini ? TRY_SAMPLES[lang] : lsGet("cv.simple.text") ?? SAMPLE));
  const [heard, setHeard] = useState<{ language?: string; warn?: string | null } | null>(null);
  const [state, setState] = useState<"idle" | "playing" | "paused">("idle");
  const [idx, setIdx] = useState(-1);
  const [waiting, setWaiting] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  const [speed, setSpeed] = useState(folder?.read_speed ?? 0.9);
  const [deess, setDeess] = useState(folder?.deess ?? "medium");
  const [mode, setMode] = useState(folder?.mode ?? "trained");
  const [switching, setSwitching] = useState(false);
  const speedRef = useRef(speed);
  speedRef.current = speed;
  const saveTimer = useRef(0);
  const run = useRef<{ abort: AbortController; ctx: AudioContext } | null>(null);
  const list = useMemo(() => sentences(text), [text]);
  const eng = engine.value;
  const ready = !!folder?.ready;

  useEffect(() => {
    setSpeed(folder?.read_speed ?? 0.9);
    if (mini) setText(lsGet(`cv.try.${folder?.id}`) ?? TRY_SAMPLES[folder?.language ?? "ja"]);
    setHeard(null);
    // the push-to-talk hotkey reads back in the voice open on this page
    if (mini && folder?.ready) api("/v1/settings", { method: "PATCH", json: { ptt: { voice: folder.id } } }).catch(() => {});
    setDeess(folder?.deess ?? "medium");
    setMode(folder?.mode ?? "trained");
    return () => stop();
  }, [folder?.id]);
  // load the voice before the first read (weights + first inference), so it starts within a second
  useEffect(() => {
    if (folder?.ready) api(`/v1/simple/folders/${folder.id}/warm`, { method: "POST" }).catch(() => {});
  }, [folder?.id, folder?.ready, mode]);

  function changeSpeed(v: number) {
    setSpeed(v);
    clearTimeout(saveTimer.current);
    const id = folder?.id;
    saveTimer.current = window.setTimeout(() => id && api(`/v1/simple/folders/${id}`, { method: "PATCH", json: { read_speed: v } }).catch(() => {}), 400);
  }

  async function changeMode(v: "trained" | "omnivoice") {
    if (!folder || v === mode) return;
    stop();
    setSwitching(true);
    try {
      await api(`/v1/simple/folders/${folder.id}`, { method: "PATCH", json: { mode: v } });
      setMode(v);
      notify(v === "omnivoice" ? "已切换到音质优先（OmniVoice），首次会加载引擎，稍等十几秒" : "已切换回训练版声线");
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setSwitching(false);
    }
  }

  function changeDeess(v: "off" | "light" | "medium" | "strong") {
    setDeess(v);
    if (folder) api(`/v1/simple/folders/${folder.id}`, { method: "PATCH", json: { deess: v } }).catch((e) => notify(e.message, "error"));
  }

  async function start() {
    if (!folder || !list.length || !conn.value) return;
    stop();
    const abort = new AbortController();
    const ctx = new AudioContext();
    const r = { abort, ctx };
    run.current = r;
    setState("playing");
    setIdx(0);
    // Each sentence is streamed (first sound after its first clause) and scheduled back to back on
    // one AudioContext; the next sentence is requested as soon as the previous one has arrived,
    // so synthesis runs ahead of playback. Pause/resume suspend the context.
    let t = ctx.currentTime + 0.05;
    const starts: number[] = [];
    let raf = 0;
    const follow = () => {
      if (run.current !== r) return;
      const now = ctx.currentTime;
      let k = 0;
      while (k + 1 < starts.length && starts[k + 1] <= now) k++;
      setIdx(k);
      raf = requestAnimationFrame(follow);
    };
    raf = requestAnimationFrame(follow);
    try {
      for (let i = 0; i < list.length; i++) {
        // stay at most ~20 s ahead of what is playing
        while (t - ctx.currentTime > 20) {
          await new Promise((ok) => setTimeout(ok, 300));
          if (run.current !== r) return;
        }
        setWaiting(true);
        const res = await fetch(url("/v1/audio/speech"), {
          method: "POST",
          signal: abort.signal,
          headers: { Authorization: `Bearer ${conn.value!.token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ input: list[i], voice: folder.id, play: false, stream: true, response_format: "wav", speed: speedRef.current }),
        });
        if (!res.ok) {
          let msg = `${res.status}`;
          try {
            const j = await res.json();
            msg = j?.error?.message ?? msg;
          } catch {}
          throw new Error(msg);
        }
        let first = true;
        for await (const { sr, pcm } of pcmChunks(res)) {
          if (run.current !== r) return;
          const buf = ctx.createBuffer(1, pcm.length, sr);
          buf.copyToChannel(pcm, 0);
          const src = ctx.createBufferSource();
          src.buffer = buf;
          src.connect(ctx.destination);
          t = Math.max(t, ctx.currentTime + 0.03);
          if (first) {
            starts[i] = t;
            first = false;
            setWaiting(false);
          }
          src.start(t);
          t += buf.duration;
        }
        t += 0.35 / speedRef.current; // a slightly longer rest between sentences when reading
      }
      setWaiting(false);
      // let the last sentence finish
      while (ctx.currentTime < t - 0.3) {
        await new Promise((ok) => setTimeout(ok, 200));
        if (run.current !== r) return;
      }
      stop();
    } catch (e: any) {
      if (run.current === r && e?.name !== "AbortError") notify(e.message ?? String(e), "error");
      if (run.current === r) stop();
    } finally {
      cancelAnimationFrame(raf);
    }
  }
  function pause() {
    const r = run.current;
    if (!r) return;
    if (state === "playing") {
      r.ctx.suspend();
      setState("paused");
    } else {
      r.ctx.resume();
      setState("playing");
    }
  }
  function stop() {
    const r = run.current;
    run.current = null;
    if (r) {
      r.abort.abort();
      r.ctx.close().catch(() => {});
    }
    setState("idle");
    setIdx(-1);
    setWaiting(false);
  }
  async function exportAs(fmt: "wav" | "mp3") {
    if (!folder) return;
    setExporting(fmt);
    try {
      const r = await api<{ path: string; seconds: number }>(`/v1/simple/folders/${folder.id}/export`, { method: "POST", json: { text, format: fmt, speed } });
      notify(`已导出 ${mmss(r.seconds)} 的 ${fmt.toUpperCase()}`);
      revealItemInDir(r.path).catch(() => {});
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setExporting(null);
    }
  }

  return (
    <section class={`card sm-step sm-read ${mini ? "mini" : ""}`}>
      <div class="step-head">
        {!mini && <div class="step-no">4</div>}
        <div class="step-title">{mini ? "试听" : "朗读"}</div>
        <div class="grow" />
        <span class="muted">{LANG_NAME[lang]}声音 · {list.length} 句</span>
      </div>
      {mini && state === "idle" ? (
        <input
          class="sm-try"
          lang={lang}
          value={text}
          placeholder="输入一句话，或按住麦克风说一句"
          onInput={(e) => {
            const v = (e.target as HTMLInputElement).value;
            setText(v);
            setHeard(null);
            try {
              localStorage.setItem(`cv.try.${folder?.id}`, v);
            } catch {}
          }}
          onKeyDown={(e) => e.key === "Enter" && ready && !busy && start()}
        />
      ) : state === "idle" ? (
        <textarea
          class="sm-text"
          lang={lang}
          value={text}
          placeholder="粘贴一段文章…"
          onInput={(e) => {
            const v = (e.target as HTMLTextAreaElement).value;
            setText(v);
            try {
              localStorage.setItem(mini ? "cv.try.text" : "cv.simple.text", v);
            } catch {}
          }}
        />
      ) : (
        <div class="sm-reading" lang={lang}>
          {list.map((s, i) => (
            <span class={i === idx ? "now" : i < idx ? "done" : ""}>{s}</span>
          ))}
        </div>
      )}
      <details class="sm-more" open={!mini}>
        <summary>{mini ? "朗读设置（语速、音质、去齿音，对 Agent 调用同样生效）" : "更多设置"}</summary>
        <div class="sm-speed">
          <span>音质</span>
          <div class="seg">
            <button class={mode === "trained" ? "on" : ""} disabled={switching} onClick={() => changeMode("trained")} title="用这个文件夹训练 / 克隆出的声线，速度最快">
              标准
            </button>
            <button class={mode === "omnivoice" ? "on" : ""} disabled={switching} onClick={() => changeMode("omnivoice")} title="用 OmniVoice 从文件夹里最干净的几句零样本克隆：s、sh 更自然，音色稍微没那么像">
              音质优先
            </button>
          </div>
          {mode === "omnivoice" && <span class="muted">OmniVoice · 合成会慢一些</span>}
        </div>
        <label class="sm-speed">
          <span>语速</span>
          <input type="range" min="0.7" max="1.2" step="0.05" value={speed} onInput={(e) => changeSpeed(+(e.target as HTMLInputElement).value)} />
          <span class="sm-speed-val">{speed.toFixed(2)}×</span>
          {speed !== 0.9 && <button class="small" onClick={() => changeSpeed(0.9)}>默认</button>}
        </label>
        <div class="sm-speed" title="压低过重的 s、sh 齿音。轻 / 中只压超出这个声线平常齿音水平的部分；强会连正常的齿音也压一些">
          <span>去齿音</span>
          <div class="seg">
            {(
              [
                ["off", "关"],
                ["light", "轻"],
                ["medium", "中"],
                ["strong", "强"],
              ] as const
            ).map(([k, label]) => (
              <button class={deess === k ? "on" : ""} onClick={() => changeDeess(k)}>
                {label}
              </button>
            ))}
          </div>
        </div>
      </details>
      {!ready && folder && <p class="muted">{mini ? "训练完成后就能在这里试听。" : "先完成第 3 步克隆，才能用这个声线朗读。"}</p>}
      {mini && heard?.warn && <p class="sm-warn">{heard.warn}</p>}
      {ready && mode === "trained" && eng?.state !== "ready" && (
        <p class="muted">
          语音引擎{eng?.state === "starting" ? "正在启动…" : "未启动。"}
          {eng?.state !== "starting" && <button class="small" onClick={() => api("/v1/engine/start", { method: "POST" }).catch((e) => notify(e.message, "error"))}>启动引擎</button>}
        </p>
      )}
      <div class="btn-row">
        {state === "idle" ? (
          <>
            <button class="primary" disabled={!ready || busy || !list.length} onClick={start}>▶ {mini ? "试听" : "朗读"}</button>
            {mini && <MicButton folder={folder} disabled={!ready || busy} onText={(t, l, w) => { setText(t); setHeard({ language: l, warn: w }); }} />}
          </>
        ) : (
          <>
            <button class="primary" onClick={pause}>{state === "playing" ? "❚❚ 暂停" : "▶ 继续"}</button>
            <button onClick={stop}>■ 停止</button>
            {waiting && <span class="muted">正在合成第 {idx + 1} 句…</span>}
          </>
        )}
        <div class="grow" />
        {!mini && <>
        <button class="small" disabled={!ready || busy || !!exporting || !text.trim()} onClick={() => exportAs("wav")}>{exporting === "wav" ? "导出中…" : "导出 WAV"}</button>
        <button class="small" disabled={!ready || busy || !!exporting || !text.trim()} onClick={() => exportAs("mp3")}>{exporting === "mp3" ? "导出中…" : "导出 MP3"}</button>
        </>}
      </div>
    </section>
  );
}


/** Hold to talk: speech -> text (SenseVoice, language detected) -> read back in this voice. */
function MicButton({ folder, disabled, onText }: { folder: Folder | null; disabled: boolean; onText: (text: string, lang: string, warn: string | null) => void }) {
  const [state, setState] = useState<"idle" | "recording" | "working">("idle");
  const [auto, setAuto] = useState(true);
  const [key, setKey] = useState("");
  useEffect(() => {
    invoke<string>("ptt_hotkey").then(setKey, () => {});
    api("/v1/settings").then((s: any) => setAuto(s.ptt?.auto_speak !== false)).catch(() => {});
    // the global hotkey goes through the backend too: show what it heard here
    return onEvent((ev) => {
      if (ev.type !== "ptt") return;
      if (ev.state === "recording") setState("recording");
      else if (ev.state === "recognizing") setState("working");
      else if (ev.state === "text") onText(ev.text, ev.language, null);
      else if (ev.state === "speaking") {
        setState("idle");
        if (ev.accent_warning) notify(ev.accent_warning);
      } else if (ev.state === "empty" || ev.state === "error") {
        setState("idle");
        notify(ev.state === "empty" ? "没听到说话" : ev.message, ev.state === "error" ? "error" : "info");
      }
    });
  }, []);
  async function down() {
    if (disabled || state !== "idle") return;
    setState("recording");
    await api("/v1/ptt/start", { method: "POST" }).catch((e) => {
      notify(e.message, "error");
      setState("idle");
    });
  }
  async function up() {
    if (state !== "recording") return;
    setState("working");
    try {
      const r = await api<any>("/v1/ptt/stop", { method: "POST", json: { speak: auto, voice: folder?.id } });
      if (r.text) onText(r.text, r.language, r.accent_warning ?? null);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setState("idle");
    }
  }
  return (
    <>
      <button
        class={`mic-btn ${state}`}
        disabled={disabled}
        onPointerDown={down}
        onPointerUp={up}
        onPointerLeave={up}
        title={`按住说话，松开后识别并${auto ? "自动念出来" : "填进输入框"}${key ? `；全局快捷键：按住 ${hotkeyLabel(key)}` : ""}`}
      >
        {state === "recording" ? "● 正在听…" : state === "working" ? "识别中…" : "🎙 按住说话"}
      </button>
      <label class="check slim" title="识别后马上用这个声音念出来">
        <input
          type="checkbox"
          checked={auto}
          onChange={(e) => {
            const v = (e.target as HTMLInputElement).checked;
            setAuto(v);
            api("/v1/settings", { method: "PATCH", json: { ptt: { auto_speak: v } } }).catch(() => {});
          }}
        />
        自动念
      </label>
      {key && <span class="muted">或按住 <kbd>{hotkeyLabel(key)}</kbd></span>}
    </>
  );
}
