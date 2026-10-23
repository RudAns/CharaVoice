// WebGL renderer for the presence orb, shared by the app (Orb.tsx) and the dev look-lab page.
// Three looks (uStyle):
//   0 ribbons  translucent glass sphere with soft light ribbons slowly weaving inside
//   1 aurora   soft colour clouds orbiting inside the glass (no hard edges)
//   2 core     dark glass, luminous core and a few fine light threads
// Output is premultiplied alpha so it composites cleanly on dark and light backgrounds.
import type { RGB } from "./fx";

export interface OrbParams {
  t: number;
  level: number; // 0..1 live level
  energy: number; // 0..1 state intensity
  sweep: number; // 0..1 thinking
  breathe: number; // -1..1
  pal: RGB[];
  radius: number; // sphere radius as a fraction of the smaller side
  style: number;
}

const VERT = `attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }`;

const FRAG = `
precision highp float;
uniform vec2 uRes;
uniform float uTime, uLevel, uEnergy, uSweep, uBreathe, uRadius, uStyle;
uniform vec3 uC0, uC1, uC2;

mat2 rot(float a){ float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
vec3 screen(vec3 a, vec3 b){ return 1.0 - (1.0 - a) * (1.0 - b); }
vec3 sat(vec3 c, float k){ float l = dot(c, vec3(0.299, 0.587, 0.114)); return clamp(mix(vec3(l), c, k), 0.0, 1.0); }

// one soft ribbon: a gaussian band around a slowly bending curve, in rotated sphere space
float ribbon(vec2 q, float ang, float freq, float phase, float amp, float width){
  vec2 r = rot(ang) * q;
  float y = amp * sin(r.x * freq + phase) + 0.18 * amp * sin(r.x * freq * 2.3 - phase * 1.7);
  float d = (r.y - y) / width;
  return exp(-d * d);
}

void main(){
  float m = min(uRes.x, uRes.y);
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / m;
  float t = uTime;
  float lv = uLevel;

  // gentle silhouette: breathing, and a smooth low-order swell with the voice
  float ang = atan(uv.y, uv.x);
  float swell = (0.010 + 0.035 * lv) * (sin(2.0 * ang + t * 1.3) * 0.6 + sin(3.0 * ang - t * 0.9) * 0.4);
  float R = uRadius * (1.0 + 0.018 * uBreathe + 0.05 * lv) * (1.0 + swell);
  vec2 p = uv / R;
  float d = length(p);
  float aa = 1.5 / (m * R);
  float inside = 1.0 - smoothstep(1.0 - aa, 1.0 + aa, d);
  float z = sqrt(max(0.0, 1.0 - min(d, 1.0) * min(d, 1.0)));
  // lens-like warp toward the rim gives the interior depth
  vec2 q = p * (1.0 + 0.45 * (1.0 - z));

  vec3 tint = mix(uC0, uC1, 0.5);
  vec3 col;
  float alpha;
  float spin = t * (0.18 + 0.35 * uEnergy) + uSweep * t * 0.9;

  if (uStyle < 0.5) {
    // ---- A: ribbons
    float w = 0.20 + 0.10 * uEnergy + 0.06 * lv;
    float a = 0.30 + 0.45 * lv + 0.12 * uSweep;
    float r0 = ribbon(q, spin, 2.1, t * 0.9, a, w);
    float r1 = ribbon(q, -spin * 0.8 + 2.094, 2.6, -t * 1.1 + 1.0, a * 0.9, w * 0.85);
    float r2 = ribbon(q, spin * 0.6 + 4.188, 1.7, t * 0.7 + 2.0, a * 1.1, w * 1.1);
    vec3 base = tint * (0.10 + 0.10 * z);
    col = base;
    col = screen(col, uC0 * r0 * (0.55 + 0.35 * z));
    col = screen(col, uC1 * r1 * (0.55 + 0.35 * z));
    col = screen(col, uC2 * r2 * (0.50 + 0.35 * z));
    alpha = 0.55 + 0.35 * max(max(r0, r1), r2);
  } else if (uStyle < 1.5) {
    // ---- B: aurora clouds
    vec3 acc = vec3(0.0);
    float wsum = 0.0;
    for (int i = 0; i < 3; i++) {
      float fi = float(i);
      float s = spin * (1.0 + 0.35 * fi) + fi * 2.094;
      vec2 c = vec2(cos(s), sin(s * 1.13)) * (0.42 + 0.12 * sin(t * 0.5 + fi));
      float f = exp(-dot(q - c, q - c) / (0.30 + 0.12 * lv));
      vec3 pc = i == 0 ? uC0 : (i == 1 ? uC1 : uC2);
      acc += pc * f;
      wsum += f;
    }
    vec3 cloud = acc / max(wsum, 1e-3);
    float density = clamp(wsum, 0.0, 1.0);
    col = mix(tint * 0.35, cloud, 0.35 + 0.55 * density) * (0.55 + 0.45 * z);
    alpha = 0.72 + 0.2 * density;
  } else if (uStyle > 3.5) {
    // ---- E: glowing nebula - additive light clouds in dark glass (hues stay separate, never grey)
    vec3 base = vec3(0.020, 0.024, 0.050);
    vec3 emis = vec3(0.0);
    for (int i = 0; i < 3; i++) {
      float fi = float(i);
      // evenly spaced around the centre, each with its own slow drift
      float s = spin + fi * 2.094 + 0.35 * sin(t * 0.37 + fi * 2.3);
      vec2 c = vec2(cos(s), sin(s)) * (0.40 + 0.07 * sin(t * 0.41 + fi * 1.7) + 0.06 * lv);
      float f = exp(-dot(q - c, q - c) / (0.25 + 0.08 * lv + 0.03 * uEnergy));
      vec3 pc = i == 0 ? uC0 : (i == 1 ? uC1 : uC2);
      emis += sat(pc, 1.3) * f * 0.62;
    }
    // luminous heart that brightens with the voice
    float heart = exp(-dot(q, q) / (0.10 + 0.08 * lv)) * (0.12 + 0.18 * lv + 0.08 * uEnergy);
    emis += mix(uC0, uC1, 0.5) * heart;
    float sheen = ribbon(q, spin * 0.7, 1.5, t * 0.6, 0.30 + 0.30 * lv, 0.30) * 0.18;
    emis += mix(uC1, uC2, 0.5) * sheen;
    col = base + emis * (0.45 + 0.55 * z) * (0.8 + 0.25 * uEnergy);
    alpha = 1.0;
  } else if (uStyle > 2.5) {
    // ---- D: refined aurora - luminous colour clouds in dark clear glass, with a faint sheen
    vec3 acc = vec3(0.0);
    float wsum = 0.0;
    for (int i = 0; i < 3; i++) {
      float fi = float(i);
      float s = spin * (1.0 + 0.3 * fi) + fi * 2.094;
      vec2 c = vec2(cos(s), sin(s * 1.17 + 0.6)) * (0.40 + 0.10 * sin(t * 0.45 + fi * 1.3));
      float f = exp(-dot(q - c, q - c) / (0.22 + 0.10 * lv + 0.04 * uEnergy));
      vec3 pc = i == 0 ? uC0 : (i == 1 ? uC1 : uC2);
      acc += pc * f;
      wsum += f;
    }
    vec3 cloud = acc / max(wsum, 1e-3);
    float glow = 1.0 - exp(-wsum * (1.1 + 0.8 * uEnergy));
    vec3 glass = vec3(0.035, 0.04, 0.075) + tint * 0.05;
    col = glass + cloud * glow * (0.55 + 0.45 * z) * (0.75 + 0.5 * uEnergy);
    float sheen = ribbon(q, spin * 0.7, 1.6, t * 0.6, 0.35 + 0.3 * lv, 0.32) * 0.22;
    col = screen(col, mix(uC1, uC2, 0.5) * sheen);
    alpha = 0.86;
  } else {
    // ---- C: luminous core + fine threads
    float core = exp(-d * d * (5.0 - 2.5 * lv)) * (0.55 + 0.45 * uEnergy);
    col = tint * 0.06 + mix(uC0, uC1, 0.4) * core;
    for (int i = 0; i < 4; i++) {
      float fi = float(i);
      float th = ribbon(q, spin * (1.0 - 0.3 * fi) + fi * 1.571, 2.0 + 0.4 * fi, t * (0.8 + 0.2 * fi) + fi, 0.25 + 0.4 * lv, 0.035);
      vec3 pc = i == 0 ? uC0 : (i == 1 ? uC1 : (i == 2 ? uC2 : mix(uC0, uC2, 0.5)));
      col = screen(col, pc * th * 0.9);
    }
    alpha = 0.78;
  }

  // thinking: a soft comet of light travelling around just inside the rim
  float comet = pow(max(0.0, cos(ang - t * 2.4)), 40.0) * uSweep * smoothstep(0.78, 0.985, d) * (1.0 - smoothstep(0.985, 1.0, d));
  col = screen(col, sat(mix(uC0, uC2, 0.4), 1.2) * comet * 0.75);

  // glass: fresnel rim, soft specular, inner shadow at the bottom
  float fres = pow(1.0 - z, 2.6);
  vec3 rimCol = uStyle > 2.5 ? sat( mix(uC0, uC1, 0.5 + 0.5 * sin(ang + t * 0.3)), 1.2) : mix(uC1, vec3(1.0), 0.35);
  col = screen(col, rimCol * fres * (0.55 + 0.35 * uEnergy));
  vec2 hp = p - vec2(-0.34, 0.42);
  vec2 hs = hp * vec2(1.0, 1.7);
  float spec = uStyle > 2.5 ? exp(-dot(hs, hs) * 160.0) * 0.38 + exp(-dot(hp, hp) * 18.0) * 0.06
                            : exp(-dot(hp, hp) * 14.0) * 0.30 + exp(-dot(hp, hp) * 60.0) * 0.25;
  col = screen(col, vec3(spec));
  col *= 1.0 - 0.18 * smoothstep(0.1, 1.0, -p.y) * (1.0 - fres);
  alpha = max(alpha, fres * 0.9);

  // tone map to keep colours soft (no clipping to flat white)
  col = 1.0 - exp(-col * 1.6);

  // outer glow (premultiplied, fades smoothly)
  float halo = exp(-max(0.0, d - 1.0) * 5.5) * (1.0 - inside) * (0.20 + 0.30 * uEnergy + 0.25 * lv);
  vec3 outc = col * alpha * inside + tint * halo;
  gl_FragColor = vec4(outc, clamp(alpha * inside + halo, 0.0, 1.0));
}`;

export interface OrbRenderer {
  draw(p: OrbParams): void;
  dispose(): void;
}

export function createOrbRenderer(cv: HTMLCanvasElement): OrbRenderer | null {
  const gl = cv.getContext("webgl", { premultipliedAlpha: true, alpha: true, antialias: true });
  if (!gl || gl.isContextLost()) return null;
  const sh = (type: number, src: string) => {
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader");
    return s;
  };
  const prog = gl.createProgram()!;
  try {
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? "link");
  } catch (e) {
    console.warn("orb shader failed", e);
    return null;
  }
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = (n: string) => gl.getUniformLocation(prog, n);
  const u = {
    res: U("uRes"), time: U("uTime"), level: U("uLevel"), energy: U("uEnergy"), sweep: U("uSweep"),
    breathe: U("uBreathe"), radius: U("uRadius"), style: U("uStyle"), c0: U("uC0"), c1: U("uC1"), c2: U("uC2"),
  };
  return {
    draw(p) {
      const dpr = Math.min(2.5, window.devicePixelRatio || 1);
      const w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
      if (cv.width !== w || cv.height !== h) {
        cv.width = w;
        cv.height = h;
      }
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(u.res, w, h);
      gl.uniform1f(u.time, p.t);
      gl.uniform1f(u.level, p.level);
      gl.uniform1f(u.energy, p.energy);
      gl.uniform1f(u.sweep, p.sweep);
      gl.uniform1f(u.breathe, p.breathe);
      gl.uniform1f(u.radius, p.radius);
      gl.uniform1f(u.style, p.style);
      const c = (i: number) => [p.pal[i][0] / 255, p.pal[i][1] / 255, p.pal[i][2] / 255] as const;
      gl.uniform3f(u.c0, ...c(0));
      gl.uniform3f(u.c1, ...c(1));
      gl.uniform3f(u.c2, ...c(2));
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    },
    dispose() {
      // free GPU objects but keep the context: the same canvas may be reused on remount
      gl.deleteBuffer(buf);
      gl.deleteProgram(prog);
    },
  };
}
