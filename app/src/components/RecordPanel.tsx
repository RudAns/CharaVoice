// Loopback recording: capture what the computer is playing (WASAPI) as training material.
import { useEffect, useState } from "preact/hooks";
import { api, onEvent } from "../api";
import { notify } from "../store";

interface Dev {
  index: number;
  name: string;
  channels: number;
  rate: number;
  default: boolean;
}

const fmt = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export function RecordPanel({ voice, onClose, onRecorded }: { voice: { id: string; name: string; consent: { label: string } }; onClose: () => void; onRecorded: (src: any) => void }) {
  const [devs, setDevs] = useState<Dev[]>([]);
  const [dev, setDev] = useState<number | null>(null);
  const [ack, setAck] = useState(false);
  const [rec, setRec] = useState(false);
  const [secs, setSecs] = useState(0);
  const [level, setLevel] = useState(0);
  const [peak, setPeak] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ devices: Dev[] }>("/v1/record/devices").then(
      (r) => {
        setDevs(r.devices);
        setDev(r.devices.find((d) => d.default)?.index ?? r.devices[0]?.index ?? null);
      },
      (e) => notify(e.message, "error"),
    );
    api("/v1/record/status").then((s) => {
      if (s.recording) {
        setRec(true);
        setSecs(s.seconds);
      }
    });
    return onEvent((ev) => {
      if (ev.type === "rec_level") {
        setLevel(ev.level);
        setSecs(ev.seconds);
        setPeak((p) => Math.max(ev.level, p * 0.96));
      } else if (ev.type === "rec_stop" && ev.error) {
        notify(ev.error, "error");
        setRec(false);
      }
    });
  }, []);

  async function start() {
    setBusy(true);
    try {
      await api("/v1/record/start", { method: "POST", json: { voice: voice.id, device: dev, consent_ack: ack } });
      setRec(true);
      setSecs(0);
    } catch (e: any) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    setBusy(true);
    try {
      const r = await api("/v1/record/stop", { method: "POST" });
      setRec(false);
      onRecorded(r.source);
    } catch (e: any) {
      setRec(false);
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="modal-back">
      <div class="modal record" role="dialog" aria-modal="true" aria-label="录制电脑声音">
        <h2>录制电脑声音</h2>
        <p class="muted">录下电脑正在播放的声音（视频、直播回放、语音通话录音等），作为「{voice.name}」的训练素材。录完会进入裁剪编辑器。</p>
        <div class="notice">只录制本人或已获授权的声音。这个声音的授权记录：{voice.consent.label}。请告知被模仿的人并取得其单独同意。</div>
        <label class="field">
          <span>录制哪个输出设备</span>
          <select disabled={rec} value={dev ?? ""} onChange={(e) => setDev(+(e.target as HTMLSelectElement).value)}>
            {devs.map((d) => (
              <option value={d.index}>
                {d.name}
                {d.default ? "（默认）" : ""} · {d.rate / 1000}kHz
              </option>
            ))}
          </select>
        </label>
        <div class={`rec-meter ${rec ? "on" : ""}`}>
          <div class="rec-time">
            <i class="rec-dot" />
            {fmt(secs)}
          </div>
          <div class="meter" aria-label="电平">
            <div class="meter-fill" style={{ width: `${Math.round(level * 100)}%` }} />
            <div class="meter-peak" style={{ left: `${Math.round(peak * 100)}%` }} />
          </div>
          <small class="muted">{rec ? (level < 0.05 ? "没有声音——确认目标设备正在播放" : "正在录制…") : "准备就绪"}</small>
        </div>
        {!rec && (
          <label class="check">
            <input type="checkbox" checked={ack} onChange={(e) => setAck((e.target as HTMLInputElement).checked)} />
            我确认只录制本人或已获授权的声音
          </label>
        )}
        <div class="btn-row end">
          {!rec && <button onClick={onClose}>取消</button>}
          {rec ? (
            <button class="primary" disabled={busy} onClick={stop}>■ 停止并裁剪</button>
          ) : (
            <button class="primary rec-btn" disabled={!ack || busy || dev === null} onClick={start}>● 开始录制</button>
          )}
        </div>
      </div>
    </div>
  );
}
