// Training material: add sources (files / loopback recordings), trim, process, review, export.
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, EMOTION_LABEL, LANG_LABEL, onEvent, url } from "../api";
import { RecordPanel } from "../components/RecordPanel";
import { WaveEditor } from "../components/WaveEditor";
import { go, notify, select, selected, voices } from "../store";
import { FileButton } from "./Voices";

interface Item {
  id: string;
  source: string;
  start: number;
  duration: number;
  text: string;
  asr_text: string;
  lang: string;
  emotion: string;
  quality: { clipping: number; snr_db: number; peak_db: number; bgm_db?: number; f0_st?: number | null };
  score: number;
  similarity: number | null;
  deleted: boolean;
}
interface Source {
  id: string;
  name: string;
  origin: "file" | "loopback";
  duration: number;
  status: "pending" | "processed";
  kept: [number, number][] | null;
  separated: boolean | null;
}
interface Summary {
  clips: number;
  kept: number;
  usable: number;
  seconds: number;
  min_seconds: number;
  recommended_seconds: number;
  level: string;
  languages: Record<string, number>;
  has_target: boolean;
}
interface Job {
  state: string;
  stage: string;
  progress: number;
  message: string;
}
interface DatasetResp {
  items: Item[];
  summary: Summary;
  target: { source: string; items?: string[] } | null;
  sources: Source[];
  job: Job | null;
}

const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

export function Material() {
  const v = selected.value;
  const [d, setD] = useState<DatasetResp | null>(null);
  const [editing, setEditing] = useState<Source | null>(null);
  const [recording, setRecording] = useState(false);
  const [minScore, setMinScore] = useState(50);
  const [minSim, setMinSim] = useState(0.5);
  const [showDeleted, setShowDeleted] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [uploading, setUploading] = useState(false);
  const player = useRef<HTMLAudioElement>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);

  const load = () => v && api<DatasetResp>(`/v1/voices/${v.id}/dataset`).then(setD, (e) => notify(e.message, "error"));
  useEffect(() => {
    setD(null);
    load();
  }, [v?.id]);
  useEffect(
    () =>
      onEvent((ev) => {
        if (!v) return;
        if (ev.type === "dataset_changed" && ev.voice === v.id) load();
        if (ev.type === "dataset_job" && ev.voice === v.id) setD((x) => (x ? { ...x, job: ev } : x));
      }),
    [v?.id],
  );

  const items = useMemo(() => (d?.items ?? []).filter((i) => showDeleted || !i.deleted), [d, showDeleted]);
  const wouldDrop = useMemo(
    () => (d?.items ?? []).filter((i) => !i.deleted && (i.score < minScore || (i.similarity !== null && i.similarity < minSim))).length,
    [d, minScore, minSim],
  );

  if (!v) {
    return (
      <div class="page doc">
        <h2>素材</h2>
        <p class="muted">先在声音库里新建一个声音。</p>
        <button class="primary" onClick={() => go("voices")}>去新建</button>
      </div>
    );
  }

  async function upload(f: File) {
    setUploading(true);
    const fd = new FormData();
    fd.append("file", f);
    try {
      const src = await api<Source>(`/v1/voices/${v!.id}/dataset/sources`, { method: "POST", body: fd });
      await load();
      setEditing(src);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setUploading(false);
    }
  }
  async function patchItem(id: string, body: Partial<Item>) {
    try {
      await api(`/v1/voices/${v!.id}/dataset/items/${id}`, { method: "PATCH", json: body });
      setD((x) => (x ? { ...x, items: x.items.map((i) => (i.id === id ? { ...i, ...body } : i)) } : x));
      if ("deleted" in body) load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function bulk(body: any, msg: string) {
    try {
      const r = await api(`/v1/voices/${v!.id}/dataset/bulk`, { method: "POST", json: body });
      notify(`${msg}：${r.changed} 段`);
      setChecked(new Set());
      load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function target(body: any) {
    try {
      await api(`/v1/voices/${v!.id}/dataset/target`, { method: "POST", json: body });
      notify("已按新的目标说话人重新计算声纹相似度");
      load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function exportList() {
    try {
      const r = await api(`/v1/voices/${v!.id}/dataset/export`, { method: "POST" });
      notify(`已导出 ${r.lines} 段（${mmss(r.seconds)}）到训练列表`);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  function playItem(i: Item) {
    const el = player.current!;
    if (playingId === i.id) {
      el.pause();
      setPlayingId(null);
      return;
    }
    el.src = url(`/v1/voices/${v!.id}/dataset/items/${i.id}/audio`, true);
    el.play();
    setPlayingId(i.id);
  }

  const s = d?.summary;
  const job = d?.job;
  const pct = s ? Math.min(100, (s.seconds / s.recommended_seconds) * 100) : 0;

  return (
    <div class="page material">
      <header class="mat-head">
        <h2>素材</h2>
        <select value={v.id} onChange={(e) => select((e.target as HTMLSelectElement).value)} aria-label="声音">
          {voices.value.map((x) => (
            <option value={x.id}>{x.name}</option>
          ))}
        </select>
        <div class="grow" />
        <FileButton accept="audio/*,video/*,.mkv,.flv" label={uploading ? "正在读取…" : "导入音视频"} onFile={upload} />
        <button onClick={() => setRecording(true)}>● 录制电脑声音</button>
      </header>

      {s && (
        <div class="card total">
          <div class="total-line">
            <span>
              可用素材 <strong>{mmss(s.seconds)}</strong>（{s.usable} 段）
            </span>
            <span class={`level ${s.level}`}>{s.level === "recommended" ? "已达到建议时长" : s.level === "minimum" ? "已达到最低时长，可以训练" : "不足 1 分钟，还不能训练"}</span>
            <span class="muted">{Object.entries(s.languages).map(([k, x]) => `${LANG_LABEL[k] ?? k} ${mmss(x)}`).join(" · ")}</span>
          </div>
          <div class="duration-bar">
            <div class="fill" style={{ width: `${pct}%` }} />
            <div class="mark" style={{ left: `${(s.min_seconds / s.recommended_seconds) * 100}%` }}>
              <span>1 分钟下限</span>
            </div>
            <div class="mark end">
              <span>10 分钟建议</span>
            </div>
          </div>
        </div>
      )}

      {job && job.state === "running" && (
        <div class="card job">
          <span>{job.stage}</span>
          <div class="progress">
            <div style={{ width: `${job.progress * 100}%` }} />
          </div>
          <button class="small" onClick={() => api(`/v1/voices/${v.id}/dataset/cancel`, { method: "POST" })}>取消</button>
        </div>
      )}
      {job && job.state === "error" && <div class="card job error">处理失败：{job.message}</div>}

      {d && d.sources.length > 0 && (
        <>
          <h3>素材来源</h3>
          <div class="sources">
            {d.sources.map((src) => (
              <div class="source">
                <span class="src-kind">{src.origin === "loopback" ? "录音" : "文件"}</span>
                <span class="src-name" title={src.name}>{src.name}</span>
                <span class="muted">{mmss(src.duration)}</span>
                <span class={`tag ${src.status}`}>
                  {src.status === "pending" ? "待裁剪" : `已处理${src.kept ? ` · 保留 ${mmss(src.kept.reduce((a, [x, y]) => a + y - x, 0))}` : ""}`}
                </span>
                <div class="grow" />
                <button class="small" disabled={job?.state === "running"} onClick={() => setEditing(src)}>
                  {src.status === "pending" ? "裁剪并处理" : "重新裁剪"}
                </button>
                <button class="small danger" onClick={() => confirm(`移除素材「${src.name}」及其切出的片段？`) && api(`/v1/voices/${v.id}/dataset/sources/${src.id}`, { method: "DELETE" }).then(load)}>
                  移除
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      {d && d.items.length > 0 && (
        <>
          <h3>审阅片段</h3>
          <div class="card review-tools">
            <div class="tool-group">
              <strong>目标说话人</strong>
              <span class="muted">{d.target ? (d.target.source === "ref" ? "使用参考音频的声纹" : `使用选中的 ${d.target.items?.length ?? 0} 段`) : "未指定"}</span>
              <button class="small" disabled={!v.ref} onClick={() => target({ use_ref: true })}>用参考音频</button>
              <button class="small" disabled={!checked.size} onClick={() => target({ item_ids: [...checked] })}>用勾选的片段（{checked.size}）</button>
            </div>
            <div class="tool-group">
              <label>
                质量分 ≥ <strong>{minScore}</strong>
                <input type="range" min={0} max={100} value={minScore} onInput={(e) => setMinScore(+(e.target as HTMLInputElement).value)} />
              </label>
              <label>
                声纹相似度 ≥ <strong>{minSim.toFixed(2)}</strong>
                <input type="range" min={0} max={1} step={0.01} value={minSim} onInput={(e) => setMinSim(+(e.target as HTMLInputElement).value)} />
              </label>
              <button class="small danger" disabled={!wouldDrop} onClick={() => bulk({ action: "filter", min_score: minScore, min_similarity: minSim }, "已按阈值删除")}>
                删除低于阈值的 {wouldDrop} 段
              </button>
            </div>
            <div class="tool-group">
              <label class="check">
                <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted((e.target as HTMLInputElement).checked)} />
                显示已删除
              </label>
              <button class="small" disabled={!checked.size} onClick={() => bulk({ action: "delete", ids: [...checked] }, "已删除")}>删除勾选</button>
              <button class="small" disabled={!checked.size} onClick={() => bulk({ action: "restore", ids: [...checked] }, "已恢复")}>恢复勾选</button>
              <div class="grow" />
              <button class="primary small" disabled={!s || s.usable === 0} onClick={exportList}>导出训练列表</button>
            </div>
          </div>
          <div class="items">
            {items.map((i) => {
              const low = i.score < minScore || (i.similarity !== null && i.similarity < minSim);
              return (
                <div key={i.id} class={`item ${i.deleted ? "deleted" : ""} ${low && !i.deleted ? "low" : ""}`}>
                  <input
                    type="checkbox"
                    checked={checked.has(i.id)}
                    onChange={(e) => {
                      const n = new Set(checked);
                      (e.target as HTMLInputElement).checked ? n.add(i.id) : n.delete(i.id);
                      setChecked(n);
                    }}
                  />
                  <button class={`play ${playingId === i.id ? "on" : ""}`} onClick={() => playItem(i)} title="试听">
                    {playingId === i.id ? "■" : "▶"}
                  </button>
                  <input
                    class="item-text"
                    value={i.text}
                    placeholder="（没有识别出文字，请手动填写或删除）"
                    onChange={(e) => patchItem(i.id, { text: (e.target as HTMLInputElement).value })}
                  />
                  <select value={i.lang} onChange={(e) => patchItem(i.id, { lang: (e.target as HTMLSelectElement).value })}>
                    {["zh", "en", "ja", "ko", "yue"].map((l) => (
                      <option value={l}>{LANG_LABEL[l] ?? l}</option>
                    ))}
                  </select>
                  <select class="emo-sel" value={EMOTION_LABEL[i.emotion] ? i.emotion : "neutral"} onChange={(e) => patchItem(i.id, { emotion: (e.target as HTMLSelectElement).value })} title="情绪（SenseVoice 自动识别，可改）">
                    {Object.entries(EMOTION_LABEL).map(([k, l]) => (
                      <option value={k}>{l}</option>
                    ))}
                  </select>
                  <span class="dur">{i.duration.toFixed(1)}s</span>
                  <span
                    class={`badge ${i.score >= 70 ? "good" : i.score >= 40 ? "mid" : "bad"}`}
                    title={`信噪比 ${i.quality.snr_db} dB · 峰值 ${i.quality.peak_db} dB · 削波 ${i.quality.clipping}%${i.quality.bgm_db !== undefined ? ` · 伴奏残留 ${i.quality.bgm_db} dB` : ""}`}
                  >
                    质量 {i.score}
                  </span>
                  <span class={`badge ${i.similarity === null ? "" : i.similarity >= 0.6 ? "good" : i.similarity >= 0.4 ? "mid" : "bad"}`} title="与目标说话人的声纹相似度">
                    声纹 {i.similarity === null ? "—" : i.similarity.toFixed(2)}
                  </span>
                  {i.quality.f0_st != null && (
                    <span
                      class={`badge ${i.quality.f0_st >= 3.5 ? "good" : i.quality.f0_st >= 2.5 ? "mid" : ""}`}
                      title="语调起伏（基频标准差，半音）。起伏大、又清晰的片段做参考音频，合成出来更自然；平板的朗读片段容易让合成也平板。"
                    >
                      起伏 {i.quality.f0_st.toFixed(1)}
                      {i.quality.f0_st >= 3.5 && i.score >= 70 && i.duration >= 3 && i.duration <= 10 ? " · 推荐作参考" : ""}
                    </span>
                  )}
                  <button
                    class="small"
                    disabled={i.duration < 3}
                    title={i.duration < 3 ? "参考音频需要 3 秒以上" : "把这段设为该情绪的参考音频"}
                    onClick={() =>
                      api(`/v1/voices/${v.id}/refs/from-clip`, { method: "POST", json: { item_id: i.id, emotion: EMOTION_LABEL[i.emotion] ? i.emotion : "neutral" } }).then(
                        () => notify(`已设为「${EMOTION_LABEL[i.emotion] ?? "平静"}」参考`),
                        (e) => notify(e.message, "error"),
                      )
                    }
                  >
                    设为参考
                  </button>
                  <button class="small" onClick={() => patchItem(i.id, { deleted: !i.deleted })}>
                    {i.deleted ? "恢复" : "删除"}
                  </button>
                </div>
              );
            })}
          </div>
        </>
      )}

      {d && d.sources.length === 0 && (
        <div class="empty-card left">
          <p>还没有素材。导入包含「{v.name}」声音的音频或视频，或者直接录制电脑正在播放的声音。建议至少 1 分钟，10 分钟以上效果更好；尽量选只有一个人说话、没有背景音乐的段落。</p>
        </div>
      )}

      <audio ref={player} onEnded={() => setPlayingId(null)} />
      {editing && <WaveEditor voice={v.id} source={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); load(); }} />}
      {recording && (
        <RecordPanel
          voice={v}
          onClose={() => setRecording(false)}
          onRecorded={(src) => {
            setRecording(false);
            load();
            setEditing(src);
          }}
        />
      )}
    </div>
  );
}
