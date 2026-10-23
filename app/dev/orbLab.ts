// Dev-only comparison page: every orb style x presence state, rendered with fixed parameters.
// Open http://localhost:1420/dev/orb-lab.html (add ?bg=light, ?styles=0,1,2, ?t=3.7).
import { PALETTES, type FxState } from "../src/fx";
import { createOrbRenderer } from "../src/orbGL";
import { drawRing, RING_RADIUS } from "../src/ringDraw";

const q = new URLSearchParams(location.search);
if (q.get("bg") === "light") document.body.classList.add("light");
const styles = (q.get("styles") ?? "0,1,2").split(",").map(Number);
const T = Number(q.get("t") ?? 3.7);
const NAMES = ["A 流光丝带", "B 极光色团", "C 内核细线", "D 精修极光", "E 发光星云"];
const STATES = [
  { st: "idle", label: "待机", level: 0, energy: 0.15, sweep: 0, breathe: 0.6 },
  { st: "listening", label: "聆听", level: 0.55, energy: 0.7, sweep: 0, breathe: 0 },
  { st: "thinking", label: "合成中", level: 0, energy: 0.45, sweep: 1, breathe: 0 },
  { st: "speaking", label: "说话", level: 0.75, energy: 0.85, sweep: 0, breathe: 0 },
] as const;

const lab = document.getElementById("lab")!;
const head = document.createElement("div");
head.className = "row";
head.innerHTML = "<div></div>" + STATES.map((s) => `<div class="head">${s.label}</div>`).join("");
lab.appendChild(head);
for (const style of styles) {
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `<div>${NAMES[style]}</div>`;
  for (const s of STATES) {
    const cell = document.createElement("div");
    cell.className = "cell";
    const orb = document.createElement("canvas");
    const ring = document.createElement("canvas");
    cell.append(orb, ring);
    row.appendChild(cell);
    requestAnimationFrame(() => {
      const fx: FxState = { state: s.st, pal: PALETTES[s.st], level: s.level, energy: s.energy, sweep: s.sweep, breathe: s.breathe, t: T, still: false };
      createOrbRenderer(orb)?.draw({ t: T, level: s.level, energy: s.energy, sweep: s.sweep, breathe: s.breathe, pal: PALETTES[s.st], radius: RING_RADIUS * 0.8, style });
      const dpr = window.devicePixelRatio || 1;
      ring.width = 300 * dpr;
      ring.height = 300 * dpr;
      const ctx = ring.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawRing(ctx, 300, 300, fx);
    });
  }
  lab.appendChild(row);
}
