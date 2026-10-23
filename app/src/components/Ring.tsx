// Presence ring: layered glowing contours whose motion follows the live level.
//   idle      slow breathing, cool white-blue
//   listening teal/green, driven by microphone level
//   speaking  violet/pink, driven by the level of the audio actually playing
//   thinking  rotating light sweep while the engine is synthesizing
// With prefers-reduced-motion the contour stays still and only colour/opacity change.
import { useEffect, useRef } from "preact/hooks";
import { tick, type RGB } from "../fx";

const rgba = (c: RGB, a: number) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

export const RING_RADIUS = 0.34; // fraction of the smaller side

export function Ring({ class: cls }: { class?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = canvas.current!;
    const ctx = cv.getContext("2d")!;
    let w = 0, h = 0, dpr = 1, raf = 0;
    const ro = new ResizeObserver(() => {
      dpr = window.devicePixelRatio || 1;
      w = cv.clientWidth;
      h = cv.clientHeight;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    });
    ro.observe(cv);

    const frame = (now: number) => {
      const { pal, level, energy, sweep, breathe, t, still, state: st } = tick(now);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const R = Math.min(w, h) * RING_RADIUS * (1 + 0.018 * breathe);

      // soft halo
      ctx.globalCompositeOperation = "lighter";
      const halo = ctx.createRadialGradient(cx, cy, R * 0.7, cx, cy, R * 1.45);
      const haloA = 0.1 + energy * 0.22 + (still ? level * 0.2 : 0);
      halo.addColorStop(0, rgba(pal[0], 0));
      halo.addColorStop(0.35, rgba(pal[0], haloA));
      halo.addColorStop(0.7, rgba(pal[1], haloA * 0.35));
      halo.addColorStop(1, rgba(pal[2], 0));
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, w, h);

      // contours
      const amp = still ? 0 : R * (0.012 + 0.012 * (breathe + 1) * (st === "idle" ? 1 : 0) + level * 0.16 + (st === "thinking" ? 0.02 : 0));
      const N = 220;
      for (let i = 0; i < 3; i++) {
        ctx.beginPath();
        for (let n = 0; n <= N; n++) {
          const a = (n / N) * Math.PI * 2;
          const wob =
            0.55 * Math.sin(3 * a + t * (1.1 + i * 0.45) + i * 2.1) +
            0.3 * Math.sin(5 * a - t * (0.8 + i * 0.3) + i) +
            0.15 * Math.sin(8 * a + t * (2.2 - i * 0.4));
          const r = R + amp * wob * (1 + i * 0.25);
          const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
          n === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        const g = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
        g.addColorStop(0, rgba(pal[i % 3], 0.9));
        g.addColorStop(0.5, rgba(pal[(i + 1) % 3], 0.75));
        g.addColorStop(1, rgba(pal[(i + 2) % 3], 0.9));
        ctx.strokeStyle = g;
        ctx.lineWidth = 1.6 + energy * 2.4 - i * 0.3;
        ctx.shadowColor = rgba(pal[i], 0.9);
        ctx.shadowBlur = 14 + energy * 26;
        ctx.globalAlpha = (still ? 0.55 + level * 0.45 : 0.7) * (1 - i * 0.18);
        ctx.stroke();
      }

      // thinking sweep: two comets travelling round the ring
      if (sweep > 0.01) {
        const rot = still ? 0 : t * 2.6;
        for (let j = 0; j < 2; j++) {
          const cg = ctx.createConicGradient(rot + j * Math.PI - 1.6, cx, cy);
          cg.addColorStop(0, rgba(pal[j], 0));
          cg.addColorStop(0.25, rgba(pal[j], 0.95 * sweep));
          cg.addColorStop(0.26, rgba(pal[j], 0));
          cg.addColorStop(1, rgba(pal[j], 0));
          ctx.beginPath();
          ctx.arc(cx, cy, R + 1, 0, Math.PI * 2);
          ctx.strokeStyle = cg;
          ctx.lineWidth = 4;
          ctx.shadowBlur = 24;
          ctx.shadowColor = rgba(pal[j], 1);
          ctx.globalAlpha = 1;
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      ctx.shadowBlur = 0;
      ctx.globalCompositeOperation = "source-over";
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return <canvas ref={canvas} class={cls} aria-hidden="true" />;
}
