// Shared presence animation state for the ring and the orb: one subscription to the
// level events, one smoothing step per animation frame, so every visual moves together.
import { onEvent, presence, type Presence } from "./api";

export type RGB = [number, number, number];
export const PALETTES: Record<Presence, RGB[]> = {
  idle: [[150, 178, 255], [196, 208, 255], [124, 146, 230]],
  listening: [[45, 212, 191], [74, 222, 128], [56, 189, 248]],
  speaking: [[167, 139, 250], [244, 114, 182], [129, 140, 248]],
  thinking: [[251, 191, 36], [96, 165, 250], [244, 114, 182]],
};

export interface FxState {
  state: Presence;
  pal: RGB[];
  level: number; // 0..1 smoothed live level of the active source
  energy: number; // 0..1 overall intensity of the current state
  sweep: number; // 0..1 thinking sweep visibility
  breathe: number; // -1..1 idle breathing phase
  t: number; // animation clock (frozen with reduced motion)
  still: boolean;
}

const mix = (a: RGB, b: RGB, k: number): RGB => [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];

export const fx: FxState = {
  state: "idle",
  pal: PALETTES.idle.map((c) => [...c] as RGB),
  level: 0,
  energy: 0.15,
  sweep: 0,
  breathe: 0,
  t: 0,
  still: false,
};

const target = { mic: 0, speak: 0 };
let last = 0;
let lastFrame = -1;
let started = false;
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

function start() {
  if (started) return;
  started = true;
  onEvent((ev) => {
    if (ev.type !== "level") return;
    if (ev.source === "speak") {
      const v = ev.level as number;
      // apply when the block actually leaves the speakers
      setTimeout(() => (target.speak = v), Math.min(300, (ev.delay ?? 0) * 1000));
    } else if (ev.source === "mic") target.mic = ev.level;
  });
}

/** Advance the shared state; safe to call from several RAF loops in the same frame. */
export function tick(now: number): FxState {
  start();
  const frame = Math.floor(now);
  if (frame === lastFrame) return fx;
  lastFrame = frame;
  const dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016;
  last = now;
  fx.still = reduce.matches;
  if (!fx.still) fx.t += dt;
  const st = presence.value;
  fx.state = st;
  const k = 1 - Math.exp(-dt * 5);
  fx.pal = fx.pal.map((c, i) => mix(c, PALETTES[st][i], k));
  const want = st === "speaking" ? target.speak : st === "listening" ? target.mic : 0;
  fx.level += (want - fx.level) * (1 - Math.exp(-dt * (want > fx.level ? 22 : 7)));
  const wantEnergy = st === "idle" ? 0.15 : st === "thinking" ? 0.45 : 0.35 + fx.level * 0.65;
  fx.energy += (wantEnergy - fx.energy) * k;
  fx.sweep = Math.max(0, Math.min(1, fx.sweep + (st === "thinking" ? 1 : -1) * dt * 3));
  fx.breathe = st === "idle" && !fx.still ? Math.sin((fx.t * Math.PI * 2) / 4.5) : 0;
  return fx;
}
