// Default showcase when a voice has no avatar: a glowing fluid orb (WebGL fragment shader).
// Same palette and live level as the ring (see fx.ts): idle breathes slowly, listening and
// speaking swell and deform with the real level, thinking spins faster with a light band.
import { useEffect, useRef, useState } from "preact/hooks";
import { tick } from "../fx";
import { RING_RADIUS } from "./Ring";

const VERT = `attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }`;
const FRAG = `
precision highp float;
uniform vec2 uRes;
uniform float uTime, uLevel, uEnergy, uSweep, uBreathe, uRadius;
uniform vec3 uC0, uC1, uC2;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p){
  vec2 i = floor(p), f = fract(p); vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p){ float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++){ v += a * noise(p); p = p * 2.03 + vec2(1.7, 9.2); a *= 0.5; } return v; }
void main(){
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y);
  float r = length(uv), ang = atan(uv.y, uv.x), t = uTime;
  float wob = sin(3.0 * ang + t * 1.7) * 0.5 + sin(5.0 * ang - t * 1.3) * 0.3 + (fbm(vec2(ang * 1.5, t * 0.8)) - 0.5) * 0.8;
  float R = uRadius * (1.0 + 0.02 * uBreathe + 0.06 * uLevel) * (1.0 + (0.01 + 0.09 * uLevel + 0.02 * uSweep) * wob);
  float d = r / R;
  float px = 1.5 / (min(uRes.x, uRes.y) * R);
  float inside = 1.0 - smoothstep(1.0 - px, 1.0 + px, d);
  float z = sqrt(max(0.0, 1.0 - d * d));
  vec2 p = uv / R;
  float sw = t * (0.25 + 0.6 * uEnergy) + uSweep * t * 1.2;
  mat2 rot = mat2(cos(sw), -sin(sw), sin(sw), cos(sw));
  vec2 q = rot * p * 1.6 / (0.6 + z * 0.6);
  float n1 = fbm(q * 0.65 + vec2(t * 0.15, -t * 0.1));
  float n2 = fbm(q * 0.9 - vec2(t * 0.2, t * 0.12) + n1 * 1.2);
  vec3 col = mix(uC0, uC1, smoothstep(0.2, 0.8, n1));
  col = mix(col, uC2, smoothstep(0.3, 0.85, n2) * 0.65);
  float vein = pow(1.0 - abs(n2 - 0.5) * 2.0, 3.0);
  float rim = pow(1.0 - z, 2.2), core = pow(z, 1.5);
  vec3 sphere = col * (0.34 + 0.5 * core) + col * rim * 1.1 * (0.6 + uEnergy) + mix(col, vec3(1.0), 0.4) * vein * (0.06 + 0.12 * uEnergy);
  vec2 hl = p - vec2(-0.35, 0.38);
  sphere += vec3(1.0) * exp(-dot(hl, hl) * 9.0) * 0.22;
  sphere += mix(uC0, uC1, 0.5) * smoothstep(0.82, 1.0, cos(ang - t * 2.6)) * uSweep * rim * 1.6;
  sphere *= 0.72 + 0.6 * uEnergy;
  float aIn = inside * (0.8 + 0.2 * rim);
  float glow = exp(-max(0.0, d - 1.0) * 7.0) * (1.0 - inside) * (0.22 + 0.45 * uEnergy);
  vec3 outc = sphere * aIn + mix(uC0, uC1, 0.5) * glow;
  gl_FragColor = vec4(outc, clamp(aIn + glow, 0.0, 1.0));
}`;

export const ORB_RADIUS = RING_RADIUS * 0.8;

export function Orb({ class: cls }: { class?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const gl = cv.getContext("webgl", { premultipliedAlpha: true, alpha: true, antialias: true });
    if (!gl || gl.isContextLost()) {
      setFallback(true);
      return;
    }
    const sh = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader");
      return s;
    };
    let prog: WebGLProgram;
    try {
      prog = gl.createProgram()!;
      gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
      gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? "link");
    } catch (e) {
      console.warn("orb shader failed", e);
      setFallback(true);
      return;
    }
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const U = (n: string) => gl.getUniformLocation(prog, n);
    const u = { res: U("uRes"), time: U("uTime"), level: U("uLevel"), energy: U("uEnergy"), sweep: U("uSweep"), breathe: U("uBreathe"), radius: U("uRadius"), c0: U("uC0"), c1: U("uC1"), c2: U("uC2") };

    const ro = new ResizeObserver(() => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      cv.width = Math.round(cv.clientWidth * dpr);
      cv.height = Math.round(cv.clientHeight * dpr);
    });
    ro.observe(cv);
    let raf = 0;
    const frame = (now: number) => {
      const f = tick(now);
      gl.viewport(0, 0, cv.width, cv.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(u.res, cv.width, cv.height);
      gl.uniform1f(u.time, f.t);
      gl.uniform1f(u.level, f.still ? 0 : f.level);
      gl.uniform1f(u.energy, f.energy + (f.still ? f.level * 0.4 : 0));
      gl.uniform1f(u.sweep, f.sweep);
      gl.uniform1f(u.breathe, f.breathe);
      gl.uniform1f(u.radius, ORB_RADIUS);
      gl.uniform3f(u.c0, f.pal[0][0] / 255, f.pal[0][1] / 255, f.pal[0][2] / 255);
      gl.uniform3f(u.c1, f.pal[1][0] / 255, f.pal[1][1] / 255, f.pal[1][2] / 255);
      gl.uniform3f(u.c2, f.pal[2][0] / 255, f.pal[2][1] / 255, f.pal[2][2] / 255);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      // Free GPU objects but keep the context: the same canvas may be reused on remount.
      gl.deleteBuffer(buf);
      gl.deleteProgram(prog);
    };
  }, []);

  if (fallback) return <div class={`${cls ?? ""} orb-fallback`} aria-hidden="true" />;
  return <canvas ref={canvas} class={cls} aria-hidden="true" />;
}
