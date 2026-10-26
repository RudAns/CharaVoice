// Training wizard: pre-flight check -> parameters -> staged progress with live log -> samples.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, LANG_LABEL, onEvent, url } from "../api";
import { go, notify, select, selected, voices } from "../store";

interface Check {
  key: string;
  ok: boolean;
  label: string;
  detail: string;
  blocking: boolean;
}
interface Precheck {
  ok: boolean;
  items: Check[];
  recommend: { s2_batch: number; s1_batch: number; s2_epochs: number; s1_epochs: number } | null;
  resumable: boolean;
  state: { status?: string; stage?: string; message?: string };
}
interface Job {
  status: string;
  stage: string;
  stage_label: string;
  progress: number;
  stage_progress: number;
  detail: string;
  message: string;
  elapsed: number;
  samples: Record<string, string>;
}

const hms = (s: number) => `${Math.floor(s / 3600) ? Math.floor(s / 3600) + ":" : ""}${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

export function Train() {
  const v = selected.value;
  const [pre, setPre] = useState<Precheck | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [stages, setStages] = useState<{ key: string; label: string }[]>([]);
  const [log, setLog] = useState<string[]>([]);
  const [samples, setSamples] = useState<string[]>([]);
  const [params, setParams] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);

  const refresh = async () => {
    if (!v) return;
    const [p, s] = await Promise.all([api<Precheck>(`/v1/voices/${v.id}/train/precheck`), api(`/v1/voices/${v.id}/train?log_lines=400`)]);
    setPre(p);
    if (p.recommend) setParams((x) => (Object.keys(x).length ? x : { ...p.recommend! }));
    setJob(s.job);
    setStages(s.stages);
    setLog(s.log);
    setSamples(s.samples);
  };
  useEffect(() => {
    setPre(null);
    setParams({});
    refresh().catch((e) => notify(e.message, "error"));
  }, [v?.id]);
  useEffect(
    () =>
      onEvent((ev) => {
        if (!v || ev.voice !== v.id) return;
        if (ev.type === "train_job") {
          setJob(ev);
          if (ev.status !== "running") refresh();
        } else if (ev.type === "train_log") setLog((l) => [...l.slice(-800), ev.line]);
      }),
    [v?.id],
  );
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  if (!v) {
    return (
      <div class="page doc">
        <h2>训练</h2>
        <p class="muted">先在声音库里新建一个声音，并在素材页准备至少 1 分钟素材。</p>
        <button class="primary" onClick={() => go("voices")}>去新建</button>
      </div>
    );
  }

  async function start(fresh = false) {
    setBusy(true);
    try {
      await api(`/v1/voices/${v!.id}/train/start`, { method: "POST", json: { ...params, fresh } });
      setLog([]);
      notify(fresh ? "从头开始训练" : "开始训练");
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const running = job?.status === "running";
  const curIdx = stages.findIndex((s) => s.key === (job?.stage ?? pre?.state.stage));
  const num = (k: string, label: string, min: number, max: number) => (
    <label class="param">
      <span>{label}</span>
      <input type="number" min={min} max={max} value={params[k] ?? ""} disabled={running} onInput={(e) => setParams({ ...params, [k]: +(e.target as HTMLInputElement).value })} />
    </label>
  );

  return (
    <div class="page train">
      <header class="mat-head">
        <h2>训练</h2>
        <select value={v.id} disabled={running} onChange={(e) => select((e.target as HTMLSelectElement).value)} aria-label="声音">
          {voices.value.map((x) => (
            <option value={x.id}>{x.name}</option>
          ))}
        </select>
        <span class="tag">{v.trained ? "已有训练权重" : "零样本"}</span>
      </header>

      {!running && !v.trained && (
        <div class="card advice">
          <strong>需要训练吗？</strong>
          <p>
            实测（10 分钟素材、RTX 3060）：对日语角色声音，训练后与真人录音的声纹相似度 0.74，零样本 V5 turbo 0.73、OmniVoice 0.72，差距很小；
            训练的好处是开口更快（低延迟首包约 0.19 秒，OmniVoice 约 0.55 秒）、说其他语言更稳（日语声音说中文，识别准确率 训练 0.92 / OmniVoice 0.30）。
          </p>
          <p class="muted">素材少或想快速用时推荐 OmniVoice（在声音库把基础模型选成 OmniVoice）；训练适合需要最像、最快响应的长期角色。训练默认使用 v2ProPlus 底模，约 6 分钟。</p>
        </div>
      )}

      {!running && pre && (
        <div class="card">
          <h3 class="first">开训前检查</h3>
          <ul class="checks">
            {pre.items.map((i) => (
              <li class={i.ok ? "ok" : i.blocking ? "bad" : "warn"}>
                <span class="mark">{i.ok ? "✓" : i.blocking ? "✕" : "!"}</span>
                <strong>{i.label}</strong>
                <span class="muted">{i.detail}</span>
              </li>
            ))}
          </ul>
          {pre.recommend && (
            <div class="params">
              {num("s2_epochs", "SoVITS 轮数", 1, 25)}
              {num("s1_epochs", "GPT 轮数", 1, 50)}
              {num("s2_batch", "SoVITS 批量", 1, 32)}
              {num("s1_batch", "GPT 批量", 1, 32)}
              <span class="muted">已按可用显存自动选择，一般不用改。轮数太多容易过拟合。</span>
            </div>
          )}
          <div class="btn-row">
            {pre.resumable ? (
              <>
                <button class="primary" disabled={!pre.ok || busy} onClick={() => start(false)}>继续上次训练</button>
                <button disabled={!pre.ok || busy} onClick={() => confirm("丢弃上次的训练进度，从头开始？") && start(true)}>从头训练</button>
              </>
            ) : (
              <button class="primary" disabled={!pre.ok || busy} onClick={() => start(false)}>开始训练</button>
            )}
            {!pre.ok && <span class="muted">有未通过的检查项，处理后再开始。</span>}
            {pre.items.find((i) => i.key === "data" && !i.ok) && <button onClick={() => go("material")}>去准备素材</button>}
          </div>
        </div>
      )}

      {(running || job) && (
        <div class="card">
          <div class="stepper">
            {stages.map((s, i) => (
              <div class={`step ${i < curIdx || job?.status === "done" ? "done" : i === curIdx ? (job?.status === "error" ? "err" : "cur") : ""}`}>
                <i>{i < curIdx || job?.status === "done" ? "✓" : i + 1}</i>
                <span>{s.label}</span>
              </div>
            ))}
          </div>
          <div class="progress big">
            <div style={{ width: `${(job?.progress ?? 0) * 100}%` }} />
          </div>
          <div class="train-line">
            <strong>{job?.status === "running" ? job.stage_label : job?.status === "done" ? "训练完成" : job?.status === "cancelled" ? "已中断" : job?.status === "error" ? "训练出错" : ""}</strong>
            <span class="muted">{job?.detail}</span>
            <span class="muted">{Math.round((job?.progress ?? 0) * 100)}% · 已用 {hms(job?.elapsed ?? 0)}</span>
            <div class="grow" />
            {running && (
              <button class="danger" onClick={() => api(`/v1/voices/${v.id}/train/cancel`, { method: "POST" })}>
                中断（可继续）
              </button>
            )}
          </div>
          {job?.message && <p class={job.status === "error" ? "error" : "muted"}>{job.message}</p>}
        </div>
      )}

      {samples.length > 0 && !running && (
        <div class="card">
          <h3 class="first">训练后试听</h3>
          <div class="samples">
            {samples.map((s) => (
              <div class="sample">
                <span>{LANG_LABEL[s.replace(".wav", "")] ?? s}</span>
                <audio controls src={url(`/v1/voices/${v.id}/train/samples/${s}`, true) + `&t=${job?.elapsed ?? 0}`} />
              </div>
            ))}
          </div>
          <p class="muted">已自动切换到新权重。可以回到舞台页随便说几句对比效果。</p>
        </div>
      )}

      {log.length > 0 && (
        <>
          <h3>训练日志</h3>
          <pre class="logs train-log" ref={logRef}>{log.join("\n")}</pre>
        </>
      )}
    </div>
  );
}
