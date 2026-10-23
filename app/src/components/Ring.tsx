// Presence ring canvas: draws ringDraw.ts every frame from the shared fx state.
import { useEffect, useRef } from "preact/hooks";
import { tick } from "../fx";
import { drawRing } from "../ringDraw";

export { RING_RADIUS } from "../ringDraw";

export function Ring({ class: cls }: { class?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = canvas.current!;
    const ctx = cv.getContext("2d")!;
    let raf = 0;
    const frame = (now: number) => {
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth, h = cv.clientHeight;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawRing(ctx, w, h, tick(now));
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={canvas} class={cls} aria-hidden="true" />;
}
