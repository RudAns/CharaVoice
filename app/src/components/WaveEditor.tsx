// Waveform trim editor for one source (imported file or loopback recording).
//   - drag the two trim handles to set the overall start/end
//   - drag on the waveform to select, then mark the selection as 保留 (keep) or 删除 (cut)
//   - final result = (union of keep regions, or the trim range if none) ∩ trim − cut regions
//   - wheel zooms around the cursor, Shift+wheel pans; play the selection or the result
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, url } from "../api";
import { notify } from "../store";

interface Peaks {
  rate: number;
  duration: number;
  min: number[];
  max: number[];
}
type Region = { id: number; type: "keep" | "cut"; a: number; b: number };
type Range = [number, number];

let rid = 1;
const fmt = (t: number) => {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
};

function union(rs: Range[]): Range[] {
  const s = [...rs].sort((x, y) => x[0] - y[0]);
  const out: Range[] = [];
  for (const [a, b] of s) {
    if (out.length && a <= out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
    else out.push([a, b]);
  }
  return out;
}
function subtract(base: Range[], cuts: Range[]): Range[] {
  let cur = base;
  for (const [ca, cb] of cuts) {
    const next: Range[] = [];
    for (const [a, b] of cur) {
      if (cb <= a || ca >= b) next.push([a, b]);
      else {
        if (ca > a) next.push([a, ca]);
        if (cb < b) next.push([cb, b]);
      }
    }
    cur = next;
  }
  return cur.filter(([a, b]) => b - a > 0.05);
}
export function resultRanges(trim: Range, regions: Region[]): Range[] {
  const keeps = regions.filter((r) => r.type === "keep").map((r) => [r.a, r.b] as Range);
  const base = (keeps.length ? union(keeps) : [trim]).map(([a, b]) => [Math.max(a, trim[0]), Math.min(b, trim[1])] as Range).filter(([a, b]) => b > a);
  return subtract(base, regions.filter((r) => r.type === "cut").map((r) => [r.a, r.b] as Range));
}

export function WaveEditor({ voice, source, onClose, onDone }: { voice: string; source: { id: string; name: string; duration: number; kept?: Range[] | null }; onClose: () => void; onDone: () => void }) {
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const D = source.duration;
  const [view, setView] = useState<Range>([0, D]);
  const [trim, setTrim] = useState<Range>([0, D]);
  const [regions, setRegions] = useState<Region[]>(() => (source.kept && source.kept.length ? source.kept.map(([a, b]) => ({ id: rid++, type: "keep" as const, a, b })) : []));
  const [sel, setSel] = useState<Range | null>(null);
  const [head, setHead] = useState(0);
  const [playing, setPlaying] = useState<Range | null>(null);
  const [separate, setSeparate] = useState(true);
  const [busy, setBusy] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const drag = useRef<{ kind: string; id?: number; x0: number; t0: number; moved: boolean } | null>(null);
  const [size, setSize] = useState(0);
  useEffect(() => {
    const ro = new ResizeObserver(() => setSize((n) => n + 1));
    ro.observe(canvas.current!);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    api<Peaks>(`/v1/voices/${voice}/dataset/sources/${source.id}/peaks?per_second=${D > 1800 ? 50 : 100}`).then(setPeaks, (e) => notify(e.message, "error"));
  }, [source.id]);

  const result = useMemo(() => resultRanges(trim, regions), [trim, regions]);
  const keptSeconds = result.reduce((s, [a, b]) => s + b - a, 0);

  // ---------- drawing
  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    cv.width = W * dpr;
    cv.height = H * dpr;
    const ctx = cv.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const [v0, v1] = view;
    const X = (t: number) => ((t - v0) / (v1 - v0)) * W;
    const ruler = 22;
    // ruler
    ctx.fillStyle = "#121722";
    ctx.fillRect(0, 0, W, ruler);
    const span = v1 - v0;
    const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((s) => span / s <= 12) ?? 1200;
    ctx.fillStyle = "#7d859c";
    ctx.font = "11px Segoe UI, Microsoft YaHei UI";
    for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
      const x = X(t);
      ctx.fillRect(x, ruler - 6, 1, 6);
      ctx.fillText(fmt(t), x + 3, 13);
    }
    const top = ruler, h = H - ruler, mid = top + h / 2;
    // kept result background
    ctx.fillStyle = "rgba(52, 211, 153, 0.08)";
    for (const [a, b] of result) ctx.fillRect(X(a), top, X(b) - X(a), h);
    // regions
    for (const r of regions) {
      ctx.fillStyle = r.type === "keep" ? "rgba(52, 211, 153, 0.16)" : "rgba(248, 113, 113, 0.22)";
      ctx.fillRect(X(r.a), top, X(r.b) - X(r.a), h);
      ctx.fillStyle = r.type === "keep" ? "#34d399" : "#f87171";
      ctx.fillRect(X(r.a), top, 2, h);
      ctx.fillRect(X(r.b) - 2, top, 2, h);
    }
    // waveform
    if (peaks) {
      // display gain: normalise to the loudest peak so quiet recordings are still readable
      let pk = 0.05;
      for (let i = 0; i < peaks.max.length; i++) pk = Math.max(pk, peaks.max[i], -peaks.min[i]);
      const g = 0.95 / pk;
      const inKept = (t: number) => result.some(([a, b]) => t >= a && t <= b);
      for (let x = 0; x < W; x++) {
        const ta = v0 + (x / W) * span, tb = v0 + ((x + 1) / W) * span;
        const ia = Math.max(0, Math.floor(ta * peaks.rate)), ib = Math.min(peaks.min.length, Math.max(ia + 1, Math.ceil(tb * peaks.rate)));
        let mn = 0, mx = 0;
        for (let i = ia; i < ib; i++) {
          if (peaks.min[i] < mn) mn = peaks.min[i];
          if (peaks.max[i] > mx) mx = peaks.max[i];
        }
        ctx.fillStyle = inKept((ta + tb) / 2) ? "#a5b4fc" : "#3a4058";
        ctx.fillRect(x, mid - mx * g * (h / 2), 1, Math.max(1, (mx - mn) * g * (h / 2)));
      }
    }
    // outside trim
    ctx.fillStyle = "rgba(6, 8, 14, 0.62)";
    ctx.fillRect(0, top, Math.max(0, X(trim[0])), h);
    ctx.fillRect(X(trim[1]), top, W - X(trim[1]), h);
    // trim handles
    for (const t of trim) {
      const x = X(t);
      ctx.fillStyle = "#fbbf24";
      ctx.fillRect(x - 1.5, top, 3, h);
      ctx.beginPath();
      ctx.roundRect(x - 6, top + h / 2 - 16, 12, 32, 4);
      ctx.fill();
    }
    // selection
    if (sel) {
      ctx.fillStyle = "rgba(96, 165, 250, 0.25)";
      ctx.fillRect(X(sel[0]), top, X(sel[1]) - X(sel[0]), h);
      ctx.strokeStyle = "#60a5fa";
      ctx.strokeRect(X(sel[0]) + 0.5, top + 0.5, X(sel[1]) - X(sel[0]) - 1, h - 1);
    }
    // playhead
    ctx.fillStyle = "#fff";
    ctx.fillRect(X(head), 0, 1, H);
  }, [peaks, view, trim, regions, sel, head, result, size]);

  // ---------- pointer interaction
  const tAt = (clientX: number) => {
    const r = canvas.current!.getBoundingClientRect();
    return Math.max(0, Math.min(D, view[0] + ((clientX - r.left) / r.width) * (view[1] - view[0])));
  };
  const pxPerSec = () => canvas.current!.clientWidth / (view[1] - view[0]);
  function onDown(e: PointerEvent) {
    (e.target as Element).setPointerCapture(e.pointerId);
    const t = tAt(e.clientX);
    const tol = 7 / pxPerSec();
    let kind = "select";
    let id: number | undefined;
    if (Math.abs(t - trim[0]) < tol) kind = "trim0";
    else if (Math.abs(t - trim[1]) < tol) kind = "trim1";
    else
      for (const r of regions) {
        if (Math.abs(t - r.a) < tol) {
          kind = "ra";
          id = r.id;
          break;
        }
        if (Math.abs(t - r.b) < tol) {
          kind = "rb";
          id = r.id;
          break;
        }
      }
    drag.current = { kind, id, x0: e.clientX, t0: t, moved: false };
  }
  function onMove(e: PointerEvent) {
    const d = drag.current;
    const t = tAt(e.clientX);
    const cv = canvas.current!;
    if (!d) {
      const tol = 7 / pxPerSec();
      const near = Math.abs(t - trim[0]) < tol || Math.abs(t - trim[1]) < tol || regions.some((r) => Math.abs(t - r.a) < tol || Math.abs(t - r.b) < tol);
      cv.style.cursor = near ? "ew-resize" : "crosshair";
      return;
    }
    if (Math.abs(e.clientX - d.x0) > 3) d.moved = true;
    if (!d.moved) return;
    if (d.kind === "trim0") setTrim(([, b]) => [Math.min(t, b - 0.1), b]);
    else if (d.kind === "trim1") setTrim(([a]) => [a, Math.max(t, a + 0.1)]);
    else if (d.kind === "ra") setRegions((rs) => rs.map((r) => (r.id === d.id ? { ...r, a: Math.min(t, r.b - 0.05) } : r)));
    else if (d.kind === "rb") setRegions((rs) => rs.map((r) => (r.id === d.id ? { ...r, b: Math.max(t, r.a + 0.05) } : r)));
    else setSel([Math.min(d.t0, t), Math.max(d.t0, t)]);
  }
  function onUp(e: PointerEvent) {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved) {
      const t = tAt(e.clientX);
      setHead(t);
      if (audio.current) audio.current.currentTime = t;
      if (d.kind === "select") setSel(null);
    }
  }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const [v0, v1] = view;
    const span = v1 - v0;
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const dt = ((e.shiftKey ? e.deltaY : e.deltaX) / canvas.current!.clientWidth) * span;
      const a = Math.max(0, Math.min(D - span, v0 + dt));
      setView([a, a + span]);
      return;
    }
    const t = tAt(e.clientX);
    const k = e.deltaY > 0 ? 1.25 : 0.8;
    const ns = Math.max(0.5, Math.min(D, span * k));
    let a = t - ((t - v0) / span) * ns;
    a = Math.max(0, Math.min(D - ns, a));
    setView([a, a + ns]);
  }
  const zoom = (k: number) => {
    const [v0, v1] = view;
    const c = (v0 + v1) / 2, ns = Math.max(0.5, Math.min(D, (v1 - v0) * k));
    const a = Math.max(0, Math.min(D - ns, c - ns / 2));
    setView([a, a + ns]);
  };

  // ---------- playback
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      const el = audio.current;
      if (el && !el.paused) {
        setHead(el.currentTime);
        if (playing) {
          const seg = result.find(([a, b]) => el.currentTime >= a - 0.02 && el.currentTime < b);
          if (playing[0] === -1) {
            // playing the result: jump over removed parts, stop after the last kept range
            if (!seg) {
              const next = result.find(([a]) => a > el.currentTime);
              if (next) el.currentTime = next[0];
              else {
                el.pause();
                setPlaying(null);
              }
            }
          } else if (el.currentTime >= playing[1]) {
            el.pause();
            setPlaying(null);
          }
        }
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [playing, result]);

  function play(r: Range | "result") {
    const el = audio.current!;
    if (r === "result") {
      if (!result.length) return;
      el.currentTime = result[0][0];
      setPlaying([-1, -1]);
    } else {
      el.currentTime = r[0];
      setPlaying(r);
    }
    el.play();
  }
  function stop() {
    audio.current?.pause();
    setPlaying(null);
  }
  function mark(type: "keep" | "cut") {
    if (!sel || sel[1] - sel[0] < 0.1) return notify("先在波形上拖动选出一段", "error");
    setRegions((rs) => [...rs, { id: rid++, type, a: sel[0], b: sel[1] }]);
    setSel(null);
  }

  async function submit() {
    if (!result.length) return notify("没有保留任何片段", "error");
    setBusy(true);
    try {
      await api(`/v1/voices/${voice}/dataset/sources/${source.id}/process`, { method: "POST", json: { ranges: result, separate } });
      notify("已开始处理：分离人声、切句、转写、打分");
      onDone();
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="modal-back">
      <div class="modal editor" role="dialog" aria-modal="true" aria-label="裁剪素材">
        <div class="editor-head">
          <h2>裁剪素材</h2>
          <span class="muted">{source.name} · 共 {fmt(D)}</span>
          <div class="grow" />
          <button onClick={onClose}>关闭</button>
        </div>
        <p class="notice">只保留目标说话人的声音：拖动黄色手柄确定首尾；在波形上拖出选区后标记“保留”或“删除”，把别人说话、音乐和杂音剪掉。</p>
        <canvas
          ref={canvas}
          class="wave"
          onPointerDown={onDown as any}
          onPointerMove={onMove as any}
          onPointerUp={onUp as any}
          onWheel={onWheel as any}
        />
        <input
          type="range"
          class="wave-scroll"
          min={0}
          max={Math.max(0, D - (view[1] - view[0]))}
          step={0.01}
          value={view[0]}
          disabled={view[1] - view[0] >= D - 0.01}
          onInput={(e) => {
            const a = +(e.target as HTMLInputElement).value;
            setView(([v0, v1]) => [a, a + (v1 - v0)]);
          }}
        />
        <div class="editor-bar">
          <div class="btn-row">
            <button class="small" onClick={() => zoom(0.5)} title="放大（也可以用滚轮）">放大</button>
            <button class="small" onClick={() => zoom(2)}>缩小</button>
            <button class="small" onClick={() => setView([0, D])}>全部</button>
            {sel && (
              <button class="small" onClick={() => setView([Math.max(0, sel[0] - 0.5), Math.min(D, sel[1] + 0.5)])}>缩放到选区</button>
            )}
          </div>
          <div class="btn-row">
            {playing ? (
              <button class="small" onClick={stop}>■ 停止</button>
            ) : (
              <>
                <button class="small" disabled={!sel} onClick={() => sel && play(sel)}>▶ 播放选区</button>
                <button class="small" onClick={() => play("result")}>▶ 试听保留结果</button>
              </>
            )}
            <span class="time">{fmt(head)}</span>
          </div>
          <div class="btn-row">
            <span class="muted">{sel ? `选区 ${fmt(sel[0])} – ${fmt(sel[1])}（${(sel[1] - sel[0]).toFixed(1)} 秒）` : "拖动选择一段"}</span>
            <button class="small keep" disabled={!sel} onClick={() => mark("keep")}>保留选区</button>
            <button class="small cut" disabled={!sel} onClick={() => mark("cut")}>删除选区</button>
          </div>
        </div>
        {regions.length > 0 && (
          <div class="regions">
            {regions
              .slice()
              .sort((x, y) => x.a - y.a)
              .map((r) => (
                <span class={`chip ${r.type}`}>
                  {r.type === "keep" ? "保留" : "删除"} {fmt(r.a)}–{fmt(r.b)}
                  <button class="x" title="移除" onClick={() => setRegions((rs) => rs.filter((x) => x.id !== r.id))}>
                    ✕
                  </button>
                </span>
              ))}
            <button class="small" onClick={() => setRegions([])}>清空标记</button>
          </div>
        )}
        <div class="editor-foot">
          <span>
            最终保留 <strong>{keptSeconds.toFixed(1)} 秒</strong>（{result.length} 段）
          </span>
          <label class="check">
            <input type="checkbox" checked={separate} onChange={(e) => setSeparate((e.target as HTMLInputElement).checked)} />
            去除背景音乐和杂音（人声分离）
          </label>
          <div class="grow" />
          <button onClick={() => { setTrim([0, D]); setRegions([]); setSel(null); }}>重置</button>
          <button class="primary" disabled={busy || !result.length} onClick={submit}>
            {busy ? "提交中…" : "确认并处理"}
          </button>
        </div>
        <audio ref={audio} src={url(`/v1/voices/${voice}/dataset/sources/${source.id}/audio`, true)} preload="auto" onEnded={() => setPlaying(null)} />
      </div>
    </div>
  );
}
