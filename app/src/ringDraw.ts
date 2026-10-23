// 2D drawing of the presence ring: one fine glowing gradient line plus a faint companion line.
// The palette slowly rotates around the ring; the line swells smoothly with the live level;
// while thinking a soft comet of light runs around it.
import type { FxState, RGB } from "./fx";

export const RING_RADIUS = 0.34; // fraction of the smaller side

const rgba = (c: RGB, a: number) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

function contour(ctx: CanvasRenderingContext2D, cx: number, cy: number, R: number, amp: number, t: number, phase: number) {
  const N = 240;
  ctx.beginPath();
  for (let n = 0; n <= N; n++) {
    const a = (n / N) * Math.PI * 2;
    const r = R + amp * (0.6 * Math.sin(2 * a + t * 1.1 + phase) + 0.4 * Math.sin(3 * a - t * 0.8 + phase * 1.7));
    const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
    n === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.closePath();
}

export function drawRing(ctx: CanvasRenderingContext2D, w: number, h: number, f: FxState) {
  const { pal, level, energy, sweep, breathe, t, still } = f;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) * RING_RADIUS * (1 + 0.012 * breathe);
  const amp = still ? 0 : R * (0.003 + 0.045 * level + 0.006 * sweep);
  const rotA = still ? 0 : t * 0.25;

  // halo
  const halo = ctx.createRadialGradient(cx, cy, R * 0.85, cx, cy, R * 1.35);
  const ha = 0.05 + energy * 0.12 + level * 0.08;
  halo.addColorStop(0, rgba(pal[0], 0));
  halo.addColorStop(0.3, rgba(pal[0], ha));
  halo.addColorStop(1, rgba(pal[2], 0));
  ctx.fillStyle = halo;
  ctx.fillRect(0, 0, w, h);

  const grad = ctx.createConicGradient(rotA, cx, cy);
  grad.addColorStop(0, rgba(pal[0], 0.95));
  grad.addColorStop(0.33, rgba(pal[1], 0.95));
  grad.addColorStop(0.66, rgba(pal[2], 0.95));
  grad.addColorStop(1, rgba(pal[0], 0.95));

  // companion line (faint, slightly outside, out of phase)
  ctx.globalAlpha = 0.28 + 0.2 * energy;
  ctx.lineWidth = 1;
  ctx.strokeStyle = grad;
  ctx.shadowBlur = 0;
  contour(ctx, cx, cy, R * 1.04, amp * 0.8, t, 2.4);
  ctx.stroke();

  // main line with glow
  ctx.globalAlpha = 0.9;
  ctx.lineWidth = 1.5 + energy * 1.1 + level * 0.8;
  ctx.shadowColor = rgba(pal[1], 0.85);
  ctx.shadowBlur = 10 + energy * 14 + level * 10;
  contour(ctx, cx, cy, R, amp, t, 0);
  ctx.stroke();

  // thinking comet
  if (sweep > 0.01) {
    const head = still ? -Math.PI / 2 : t * 2.4;
    const cg = ctx.createConicGradient(head - 1.4, cx, cy);
    cg.addColorStop(0, rgba(pal[1], 0));
    cg.addColorStop(0.22, rgba(pal[0], 0.95 * sweep));
    cg.addColorStop(0.225, rgba(pal[0], 0));
    cg.addColorStop(1, rgba(pal[0], 0));
    ctx.globalAlpha = 1;
    ctx.lineWidth = 3;
    ctx.shadowBlur = 18;
    ctx.shadowColor = rgba(pal[0], 1);
    ctx.strokeStyle = cg;
    contour(ctx, cx, cy, R, amp, t, 0);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}
