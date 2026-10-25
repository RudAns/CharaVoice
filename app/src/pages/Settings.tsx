import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "preact/hooks";
import { api, engine } from "../api";
import { notify } from "../store";

interface Device {
  index: number;
  name: string;
  hostapi: string;
  inputs: number;
  outputs: number;
}

const ENGINE_LABEL: Record<string, string> = { ready: "运行中", starting: "启动中", stopped: "已停止", error: "出错" };

export function Settings() {
  const [s, setS] = useState<any>(null);
  const [devs, setDevs] = useState<Device[]>([]);
  const [logs, setLogs] = useState<string[] | null>(null);
  const [hotkey, setHotkey] = useState<string | null>(null);

  useEffect(() => {
    api("/v1/settings").then(setS);
    api<{ devices: Device[] }>("/v1/devices").then((r) => setDevs(r.devices)).catch(() => {});
    invoke<string>("dictation_hotkey").then(setHotkey).catch(() => setHotkey(""));
  }, []);

  async function save(patch: any) {
    try {
      setS(await api("/v1/settings", { method: "PATCH", json: patch }));
      notify("已保存");
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  if (!s) return <div class="page doc" />;
  const eng = engine.value;
  // Prefer WASAPI entries, which reflect the Windows default devices most faithfully.
  const pick = (kind: "inputs" | "outputs") => devs.filter((d) => d[kind] > 0 && /WASAPI|MME/.test(d.hostapi));

  return (
    <div class="page doc">
      <h2>设置</h2>

      <h3>声音引擎（GPT-SoVITS）</h3>
      <div class="card">
        <div class="engine-line">
          <span class={`dot ${eng?.state}`} />
          <strong>{ENGINE_LABEL[eng?.state ?? "stopped"]}</strong>
          <span class="muted">{eng?.message}</span>
          <div class="grow" />
          <button onClick={() => api("/v1/engine/start", { method: "POST" })}>启动</button>
          <button onClick={() => api("/v1/engine/stop", { method: "POST" })}>停止</button>
          <button onClick={async () => setLogs((await api<{ lines: string[] }>("/v1/engine/logs")).lines)}>查看日志</button>
        </div>
        <label class="check">
          <input type="checkbox" checked={s.engine.autostart} onChange={(e) => save({ engine: { autostart: (e.target as HTMLInputElement).checked } })} />
          启动 CharaVoice 时自动启动引擎
        </label>
        <label class="check">
          <input type="checkbox" checked={s.engine.use_cuda_graph} onChange={(e) => save({ engine: { use_cuda_graph: (e.target as HTMLInputElement).checked } })} />
          CUDA Graph 加速（首次合成新长度时稍慢，之后更快）
        </label>
        <label class="field narrow">
          <span>出声方式</span>
          <select value={s.engine.streaming_mode} onChange={(e) => save({ engine: { streaming_mode: +(e.target as HTMLSelectElement).value } })}>
            <option value={0}>整段合成后播放（最稳）</option>
            <option value={1}>逐句合成，边合成边播放（推荐）</option>
            <option value={2}>流式（更快开口，音质中等）</option>
            <option value={3}>极速流式（最快开口，音质较低）</option>
          </select>
        </label>
        {logs && <pre class="logs">{logs.slice(-120).join("\n")}</pre>}
      </div>

      <OmniCard />

      <h3>播放与麦克风</h3>
      <div class="card">
        <label class="check">
          <input type="checkbox" checked={s.speech.play_locally} onChange={(e) => save({ speech: { play_locally: (e.target as HTMLInputElement).checked } })} />
          接口调用时默认由 CharaVoice 在本机播放（请求里 play=false 可只取音频）
        </label>
        <div class="field-row">
          <label class="field">
            <span>输出设备</span>
            <select value={s.speech.output_device ?? ""} onChange={(e) => save({ speech: { output_device: (e.target as HTMLSelectElement).value === "" ? null : +(e.target as HTMLSelectElement).value } })}>
              <option value="">系统默认</option>
              {pick("outputs").map((d) => (
                <option value={d.index}>
                  {d.name}（{d.hostapi}）
                </option>
              ))}
            </select>
          </label>
          <label class="field">
            <span>麦克风</span>
            <select value={s.listen.input_device ?? ""} onChange={(e) => save({ listen: { input_device: (e.target as HTMLSelectElement).value === "" ? null : +(e.target as HTMLSelectElement).value } })}>
              <option value="">系统默认</option>
              {pick("inputs").map((d) => (
                <option value={d.index}>
                  {d.name}（{d.hostapi}）
                </option>
              ))}
            </select>
          </label>
        </div>
        <label class="check">
          <input type="checkbox" checked={s.listen.pause_while_speaking} onChange={(e) => save({ listen: { pause_while_speaking: (e.target as HTMLInputElement).checked } })} />
          角色说话时暂停听写（避免把自己的声音录进去）
        </label>
        <label class="field narrow">
          <span>说完判定：静音 {s.listen.min_silence.toFixed(2)} 秒</span>
          <input type="range" min="0.2" max="1.5" step="0.05" value={s.listen.min_silence} onChange={(e) => save({ listen: { min_silence: +(e.target as HTMLInputElement).value } })} />
        </label>
        <div class="hotkey-line">
          <span>全局听写快捷键</span>
          {hotkey ? <kbd>{hotkeyLabel(hotkey)}</kbd> : <span class="muted">{hotkey === "" ? "全部候选组合都被其他程序占用" : "…"}</span>}
          <span class="muted">在任何输入框里按下，说完一句话后文字会自动输入到光标处。</span>
        </div>
        <label class="field narrow">
          <span>全局听写：说完 {(s.listen.dictation_end_silence ?? 0.7).toFixed(2)} 秒后自动输入</span>
          <input type="range" min="0.3" max="2" step="0.05" value={s.listen.dictation_end_silence ?? 0.7} onChange={(e) => save({ listen: { dictation_end_silence: +(e.target as HTMLInputElement).value } })} />
          <small class="muted">越短越快，但说话中间停顿稍长就会提前输入；不会短于上面的“说完判定”。</small>
        </label>
        <label class="field narrow">
          <span>听写模型</span>
          <select value={s.listen.engine} onChange={(e) => save({ listen: { engine: (e.target as HTMLSelectElement).value } })}>
            <option value="sensevoice">SenseVoice（中英日韩粤，推荐）</option>
            <option value="whisper">Whisper（需另行下载模型）</option>
          </select>
        </label>
      </div>

      <h3>合规</h3>
      <div class="card">
        <label class="check">
          <input type="checkbox" checked={s.compliance.metadata_tag} onChange={(e) => save({ compliance: { metadata_tag: (e.target as HTMLInputElement).checked } })} />
          在接口返回的音频文件里写入“AI 合成”元数据
        </label>
        <label class="check">
          <input type="checkbox" checked={s.compliance.watermark} onChange={(e) => save({ compliance: { watermark: (e.target as HTMLInputElement).checked } })} />
          嵌入 AudioSeal 不可听水印
        </label>
      </div>
    </div>
  );
}

function OmniCard() {
  const [e, setE] = useState<any>(null);
  const load = () => api("/v1/engines").then((r) => setE(r.engines.find((x: any) => x.name === "omnivoice")));
  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);
  if (!e) return null;
  return (
    <>
      <h3>零样本引擎（OmniVoice，可选）</h3>
      <div class="card">
        <div class="engine-line">
          <span class={`dot ${e.state}`} />
          <strong>{e.installed ? ENGINE_LABEL[e.state] ?? e.state : "未安装"}</strong>
          <span class="muted">{e.installed ? e.message : "运行 setup.bat --with-omnivoice 安装（约 5GB）"}</span>
          <div class="grow" />
          <button disabled={!e.installed} onClick={() => api("/v1/engines/omnivoice/start", { method: "POST" }).then(load)}>启动</button>
          <button disabled={!e.installed} onClick={() => api("/v1/engines/omnivoice/stop", { method: "POST" }).then(load)}>停止</button>
        </div>
        <p class="muted">不训练也能高相似的零样本引擎（k2-fsa OmniVoice，代码 Apache-2.0）。把声音的“基础模型”选成 OmniVoice 即可使用。它和 GPT-SoVITS 不同时占用显存，切换声音时会自动卸载另一个（约 15 秒）。</p>
      </div>
    </>
  );
}

export const hotkeyLabel = (k: string) => k.split("+").map((p) => p[0].toUpperCase() + p.slice(1)).join(" + ");
