// Default showcase when a voice has no avatar: the glass orb (orbGL.ts), driven by the
// same presence state and live level as the ring (fx.ts).
import { useEffect, useRef, useState } from "preact/hooks";
import { tick } from "../fx";
import { createOrbRenderer } from "../orbGL";
import { RING_RADIUS } from "../ringDraw";

export const ORB_RADIUS = RING_RADIUS * 0.8;
export const ORB_STYLE = 4; // "E" glowing nebula, chosen in dev/orb-lab.html

export function Orb({ class: cls }: { class?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const r = createOrbRenderer(cv);
    if (!r) {
      setFallback(true);
      return;
    }
    let raf = 0;
    const frame = (now: number) => {
      const f = tick(now);
      r.draw({
        t: f.t,
        level: f.still ? 0 : f.level,
        energy: f.energy + (f.still ? f.level * 0.4 : 0),
        sweep: f.sweep,
        breathe: f.breathe,
        pal: f.pal,
        radius: ORB_RADIUS,
        style: ORB_STYLE,
      });
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      r.dispose();
    };
  }, []);

  if (fallback) return <div class={`${cls ?? ""} orb-fallback`} aria-hidden="true" />;
  return <canvas ref={canvas} class={cls} aria-hidden="true" />;
}
