// Inline trim editor for a hotkey recording (simple mode).
//   - the two yellow handles start at the suggested start/end (first and last speech, Silero VAD)
//   - the strip under the waveform shows speech segments; segments whose voice differs from the
//     reference (the folder's saved clips, or a segment the user picks) are marked and left out
//   - click a segment to keep/leave it out, double-click it to make it the reference voice
//   - drag on the waveform to select and play a part; wheel zooms, Shift+wheel pans
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api, url } from "../api";
import { notify } from "../store";

interface Peaks {
  rate: number;
  duration: number;
  min: number[];
  max: number[];
}
interface Analysis {
  duration: number;
  suggest: [number, number];
  segments: { a: number; b: number; folder_sim: number | null }[];
  sims: (number | null)[][];
  speaker_check: boolean;
  has_folder_ref: boolean;
  threshold: number;
}
export interface FolderLite {
  id: string;
  name: string;
}
type Range = [number, number];

const fmt = (t: number) => {
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, "0")}`;
};
const STRIP = 20; // segment strip height at the bottom of the canvas

function subtract(base: Range, cuts: Range[]): Range[] {
  let cur: Range[] = [base];
  for (const [ca, cb] of [...cuts].sort((x, y) => x[0] - y[0])) {
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
  return cur.filter(([a, b]) => b - a > 0.15);
}

export function ClipEditor({
  rec,
  folder,
  folders,
  replace,
  onSaved,
  onClose,
  onNewFolder,
}: {
  rec: string;
  folder: string | null;
  folders: FolderLite[];
  replace?: { id: string; folder: string; ranges: Range[] } | null;
  onSaved: (folder: string) => void;
  onClose: (discard: boolean) => void;
  onNewFolder: () => Promise<string | null>;
}) {
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [an, setAn] = useState<Analysis | null>(null);
  const [D, setD] = useState(1);
  const [view, setView] = useState<Range>([0, 1]);
  const [trim, setTrim] = useState<Range>([0, 1]);
  const [ref, setRef] = useState<number | "folder">("folder");
  const [autoOut, setAutoOut] = useState(true);
  const [manual, setManual] = useState<Record<number, boolean>>({}); // segment index -> keep?
  const [sel, setSel] = useState<Range | null>(null);
  const [head, setHead] = useState(0);
  const [playing, setPlaying] = useState<Range | "result" | null>(null);
  const [target, setTarget] = useState(replace?.folder ?? folder ?? "");
  const [name, setName] = useState("");
  const [tag, setTag] = useState("");
  const [busy, setBusy] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const drag = useRef<{ kind: string; x0: number; t0: number; moved: boolean } | null>(null);
  const [size, setSize] = useState(0);
  const inited = useRef("");

  useEffect(() => {
    const ro = new ResizeObserver(() => setSize((n) => n + 1));
    ro.observe(canvas.current!);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    if (!replace && folder) setTarget(folder);
  }, [folder]);

  useEffect(() => {
    setPeaks(null);
    setAn(null);
    api<Peaks>(`/v1/simple/recordings/${rec}/peaks?per_second=100`).then((p) => {
      setPeaks(p);
      setD(p.duration);
      setView([0, p.duration]);
      if (inited.current !== rec) setTrim([0, p.duration]); // the analysis may already have set it
    }, (e) => notify(e.message, "error"));
  }, [rec]);
  useEffect(() => {
    const f = replace?.folder ?? target;
    api<Analysis>(`/v1/simple/recordings/${rec}/analyze${f ? `?folder=${f}` : ""}`).then((a) => {
      setAn(a);
      setRef(a.has_folder_ref ? "folder" : Math.max(0, a.sims.findIndex((row, i) => row[i] !== null)));
      if (inited.current === rec) return; // another target folder: new reference, keep the user's trim
      inited.current = rec;
      if (replace) {
        const lo = Math.min(...replace.ranges.map((r) => r[0])), hi = Math.max(...replace.ranges.map((r) => r[1]));
        setTrim([lo, hi]);
        // segments inside the trim range that the previous cut left out stay out
        const m: Record<number, boolean> = {};
        a.segments.forEach((s, i) => {
          if (s.b > lo && s.a < hi) m[i] = replace.ranges.some(([ra, rb]) => s.a < rb && s.b > ra);
        });
        setManual(m);
        setAutoOut(false);
      } else setTrim(a.suggest);
    }, (e) => notify(e.message, "error"));
  }, [rec, replace?.folder ?? target]);

  // similarity of each segment to the chosen reference voice
  const sim = (i: number): number | null => {
    if (!an) return null;
    if (ref === "folder") return an.segments[i].folder_sim;
    return an.sims[i]?.[ref] ?? null;
  };
  const other = (i: number) => {
    const s = sim(i);
    return s !== null && an !== null && s < an.threshold;
  };
  const keeps = (i: number) => (i in manual ? manual[i] : !(autoOut && other(i)));
  const result = useMemo<Range[]>(() => {
    if (!an) return [trim];
    const cuts = an.segments.map((s, i) => (keeps(i) ? null : ([s.a - 0.05, s.b + 0.05] as Range))).filter(Boolean) as Range[];
    return subtract(trim, cuts);
  }, [an, trim, manual, autoOut, ref]);
  const kept = result.reduce((s, [a, b]) => s + b - a, 0);
  const flagged = an ? an.segments.filter((_, i) => other(i) && an.segments[i].b > trim[0] && an.segments[i].a < trim[1]).length : 0;

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
    const span = v1 - v0;
    const X = (t: number) => ((t - v0) / span) * W;
    const ruler = 20;
    ctx.fillStyle = "#121722";
    ctx.fillRect(0, 0, W, ruler);
    const step = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((s) => span / s <= 10) ?? 1200;
    ctx.fillStyle = "#7d859c";
    ctx.font = "11px Segoe UI, Microsoft YaHei UI";
    for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
      ctx.fillRect(X(t), ruler - 6, 1, 6);
      ctx.fillText(fmt(t), X(t) + 3, 13);
    }
    const top = ruler, h = H - ruler - STRIP - 4, mid = top + h / 2;
    ctx.fillStyle = "rgba(52, 211, 153, 0.08)";
    for (const [a, b] of result) ctx.fillRect(X(a), top, X(b) - X(a), h);
    if (peaks) {
      let pk = 0.05;
      for (let i = 0; i < peaks.max.length; i++) pk = Math.max(pk, peaks.max[i], -peaks.min[i]);
      const g = 0.95 / pk;
      for (let x = 0; x < W; x++) {
        const ta = v0 + (x / W) * span, tb = v0 + ((x + 1) / W) * span;
        const ia = Math.max(0, Math.floor(ta * peaks.rate)), ib = Math.min(peaks.min.length, Math.max(ia + 1, Math.ceil(tb * peaks.rate)));
        let mn = 0, mx = 0;
        for (let i = ia; i < ib; i++) {
          if (peaks.min[i] < mn) mn = peaks.min[i];
          if (peaks.max[i] > mx) mx = peaks.max[i];
        }
        const tm = (ta + tb) / 2;
        ctx.fillStyle = result.some(([a, b]) => tm >= a && tm <= b) ? "#a5b4fc" : "#3a4058";
        ctx.fillRect(x, mid - mx * g * (h / 2), 1, Math.max(1, (mx - mn) * g * (h / 2)));
      }
    }
    // segment strip: green = kept, red = left out (likely someone else), outlined = reference
    const sy = H - STRIP;
    ctx.fillStyle = "#11151f";
    ctx.fillRect(0, sy, W, STRIP);
    an?.segments.forEach((s, i) => {
      const k = keeps(i);
      ctx.fillStyle = k ? (other(i) ? "#b45309" : "#059669") : "#b91c1c";
      ctx.globalAlpha = k ? 0.85 : 0.75;
      ctx.fillRect(X(s.a) + 1, sy + 3, Math.max(2, X(s.b) - X(s.a) - 2), STRIP - 6);
      ctx.globalAlpha = 1;
      if (ref === i) {
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.strokeRect(X(s.a) + 1, sy + 2, Math.max(2, X(s.b) - X(s.a) - 2), STRIP - 4);
        ctx.lineWidth = 1;
      }
    });
    ctx.fillStyle = "rgba(6, 8, 14, 0.62)";
    ctx.fillRect(0, top, Math.max(0, X(trim[0])), h);
    ctx.fillRect(X(trim[1]), top, W - X(trim[1]), h);
    for (const t of trim) {
      const x = X(t);
      ctx.fillStyle = "#fbbf24";
      ctx.fillRect(x - 1.5, top, 3, h);
      ctx.beginPath();
      ctx.roundRect(x - 6, top + h / 2 - 16, 12, 32, 4);
      ctx.fill();
    }
    if (sel) {
      ctx.fillStyle = "rgba(96, 165, 250, 0.25)";
      ctx.fillRect(X(sel[0]), top, X(sel[1]) - X(sel[0]), h);
    }
    ctx.fillStyle = "#fff";
    ctx.fillRect(X(head), 0, 1, H);
  }, [peaks, an, view, trim, result, sel, head, size, ref, manual, autoOut]);

  // ---------- pointer interaction
  const tAt = (clientX: number) => {
    const r = canvas.current!.getBoundingClientRect();
    return Math.max(0, Math.min(D, view[0] + ((clientX - r.left) / r.width) * (view[1] - view[0])));
  };
  const inStrip = (clientY: number) => {
    const r = canvas.current!.getBoundingClientRect();
    return clientY - r.top >= r.height - STRIP;
  };
  const segAt = (t: number) => an?.segments.findIndex((s) => t >= s.a && t <= s.b) ?? -1;
  const tol = () => 7 / (canvas.current!.clientWidth / (view[1] - view[0]));
  function onDown(e: PointerEvent) {
    (e.target as Element).setPointerCapture(e.pointerId);
    const t = tAt(e.clientX);
    let kind = "select";
    if (inStrip(e.clientY)) kind = "strip";
    else if (Math.abs(t - trim[0]) < tol()) kind = "trim0";
    else if (Math.abs(t - trim[1]) < tol()) kind = "trim1";
    drag.current = { kind, x0: e.clientX, t0: t, moved: false };
  }
  function onMove(e: PointerEvent) {
    const d = drag.current;
    const t = tAt(e.clientX);
    if (!d) {
      const near = Math.abs(t - trim[0]) < tol() || Math.abs(t - trim[1]) < tol();
      canvas.current!.style.cursor = inStrip(e.clientY) ? (segAt(t) >= 0 ? "pointer" : "default") : near ? "ew-resize" : "crosshair";
      return;
    }
    if (Math.abs(e.clientX - d.x0) > 3) d.moved = true;
    if (!d.moved || d.kind === "strip") return;
    if (d.kind === "trim0") setTrim(([, b]) => [Math.min(t, b - 0.2), b]);
    else if (d.kind === "trim1") setTrim(([a]) => [a, Math.max(t, a + 0.2)]);
    else setSel([Math.min(d.t0, t), Math.max(d.t0, t)]);
  }
  function onUp(e: PointerEvent) {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved) return;
    const t = tAt(e.clientX);
    if (d.kind === "strip") {
      const i = segAt(t);
      if (i >= 0) setManual((m) => ({ ...m, [i]: !keeps(i) }));
      return;
    }
    setHead(t);
    if (audio.current) audio.current.currentTime = t;
    if (d.kind === "select") setSel(null);
  }
  function onDbl(e: MouseEvent) {
    if (!inStrip(e.clientY) || !an?.speaker_check) return;
    const i = segAt(tAt(e.clientX));
    if (i >= 0 && an.sims[i][i] !== null) {
      setRef(i);
      setManual({});
      setAutoOut(true);
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
    const ns = Math.max(0.5, Math.min(D, span * (e.deltaY > 0 ? 1.25 : 0.8)));
    const a = Math.max(0, Math.min(D - ns, t - ((t - v0) / span) * ns));
    setView([a, a + ns]);
  }

  // ---------- playback
  useEffect(() => {
    let raf = 0;
    const loop = () => {
      const el = audio.current;
      if (el && !el.paused) {
        setHead(el.currentTime);
        if (playing === "result") {
          if (!result.some(([a, b]) => el.currentTime >= a - 0.02 && el.currentTime < b)) {
            const next = result.find(([a]) => a > el.currentTime);
            if (next) el.currentTime = next[0];
            else {
              el.pause();
              setPlaying(null);
            }
          }
        } else if (playing && el.currentTime >= playing[1]) {
          el.pause();
          setPlaying(null);
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
    } else el.currentTime = r[0];
    setPlaying(r);
    el.play();
  }
  function stop() {
    audio.current?.pause();
    setPlaying(null);
  }

  async function save() {
    let f = target;
    if (!f) {
      const made = await onNewFolder();
      if (!made) return;
      f = made;
      setTarget(made);
    }
    if (!result.length) return notify("没有保留任何声音", "error");
    setBusy(true);
    try {
      await api(`/v1/simple/folders/${f}/clips`, {
        method: "POST",
        json: { rec, ranges: result, name: name.trim() || null, tag: tag.trim() || null, replace: replace?.folder === f ? replace.id : null },
      });
      notify(replace ? "已重新裁剪" : "已保存片段");
      onSaved(f);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="clip-editor">
      <div class="ce-head">
        <strong>{replace ? "重新裁剪" : "截取刚录的声音"}</strong>
        <span class="muted">共 {fmt(D)}</span>
        <div class="grow" />
        {an && !an.speaker_check && <span class="muted">（缺少声纹模型，不能自动区分说话人）</span>}
        {an && an.speaker_check && (
          <label class="check slim">
            <input type="checkbox" checked={autoOut} onChange={(e) => { setAutoOut((e.target as HTMLInputElement).checked); setManual({}); }} />
            自动去掉别人的声音{flagged ? `（${flagged} 段）` : ""}
          </label>
        )}
      </div>
      <canvas ref={canvas} class="wave ce-wave" onPointerDown={onDown as any} onPointerMove={onMove as any} onPointerUp={onUp as any} onDblClick={onDbl as any} onWheel={onWheel as any} />
      <div class="ce-legend muted">
        <span><i class="sw keep" />保留</span>
        <span><i class="sw out" />去掉（像是别人）</span>
        <span><i class="sw warn" />不太像但保留</span>
        <span class="ce-help">黄色手柄是首尾，已自动对准第一句和最后一句；点色条切换保留或去掉，双击设为基准{ref === "folder" && an?.has_folder_ref ? "（当前基准：文件夹里已有的片段）" : ""}</span>
      </div>
      <div class="ce-bar">
        {playing ? (
          <button class="small" onClick={stop}>■ 停止</button>
        ) : (
          <>
            <button class="small primary" onClick={() => play("result")}>▶ 试听保留部分</button>
            <button class="small" disabled={!sel} onClick={() => sel && play(sel)}>▶ 播放选区</button>
          </>
        )}
        <button class="small" onClick={() => setView([0, D])}>看全部</button>
        {sel && <button class="small" onClick={() => { setTrim([sel[0], sel[1]]); setSel(null); }}>只要选区</button>}
        <span class="time">{fmt(head)}</span>
        <div class="grow" />
        <span>保留 <strong>{kept.toFixed(1)} 秒</strong></span>
      </div>
      <div class="ce-save">
        <label class="field inline">
          <span>存到</span>
          <select
            value={target}
            disabled={!!replace}
            onChange={async (e) => {
              const v = (e.target as HTMLSelectElement).value;
              if (v === "__new") {
                const made = await onNewFolder();
                setTarget(made ?? target);
              } else setTarget(v);
            }}
          >
            {!target && <option value="">（先新建一个文件夹）</option>}
            {folders.map((f) => (
              <option value={f.id}>{f.name}</option>
            ))}
            <option value="__new">＋ 新建文件夹…</option>
          </select>
        </label>
        {!replace && (
          <>
            <input class="ce-name" placeholder="片段名（可不填）" value={name} onInput={(e) => setName((e.target as HTMLInputElement).value)} />
            <input class="ce-tag" placeholder="标签，如 开心" value={tag} onInput={(e) => setTag((e.target as HTMLInputElement).value)} />
          </>
        )}
        <div class="grow" />
        <button onClick={() => onClose(!replace)}>{replace ? "取消" : "放弃这段录音"}</button>
        <button class="primary" disabled={busy || !result.length || !an} onClick={save}>
          {busy ? "保存中…" : replace ? "保存修改" : "保存片段"}
        </button>
      </div>
      <audio ref={audio} src={url(`/v1/simple/recordings/${rec}/audio`, true)} preload="auto" onEnded={() => setPlaying(null)} />
    </div>
  );
}
