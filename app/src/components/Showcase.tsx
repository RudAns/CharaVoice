// Character showcase: avatar (per-state image/loop) composited with the presence ring.
// Geometry (fractions of the square box, see RING_RADIUS = 0.34):
//   circle mode  avatar fills a disc of 0.86 R, object-fit: cover
//   free mode    transparent sprite scaled by height, bottom-aligned, masked to 0.94 R
// Everything stays inside the ring; with no avatar (or a file that fails to load)
// a quiet placeholder is shown instead.
import { useState } from "preact/hooks";
import { presence, url, type AvatarSlot, type Voice } from "../api";
import { Ring } from "./Ring";

const SLOT_FOR: Record<string, AvatarSlot> = { speaking: "speaking", listening: "listening", idle: "idle", thinking: "idle" };

export function Showcase({ voice, compact = false, onUpload }: { voice?: Voice; compact?: boolean; onUpload?: () => void }) {
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  const st = presence.value;
  const mode = voice?.avatar_mode ?? "circle";
  const av = voice?.avatar;
  const usable = (slot: AvatarSlot) => {
    const a = av?.[slot];
    return a && !failed[a.url] ? a : null;
  };
  const active = usable(SLOT_FOR[st]) ?? usable("default") ?? usable("idle") ?? usable("speaking") ?? usable("listening");
  // Keep every distinct source mounted so videos stay warm and state switches cross-fade instantly.
  const sources = av
    ? Array.from(
        new Map(
          (["default", "idle", "listening", "speaking"] as AvatarSlot[])
            .map((s) => av[s])
            .filter((a): a is NonNullable<typeof a> => !!a && !failed[a.url])
            .map((a) => [a.url, a]),
        ).values(),
      )
    : [];
  const fail = (u: string) => setFailed((f) => ({ ...f, [u]: true }));

  return (
    <div class={`showcase mode-${mode} state-${st} ${compact ? "compact" : ""}`}>
      {!active && (
        <div class="avatar-placeholder">
          <svg viewBox="0 0 64 64" aria-hidden="true">
            <circle cx="32" cy="24" r="10" />
            <path d="M14 52c2.5-9.5 9.5-15 18-15s15.5 5.5 18 15" />
          </svg>
          {!compact && onUpload && (
            <button class="link" onClick={onUpload}>
              上传角色形象
            </button>
          )}
        </div>
      )}
      {active && (
        <div class="avatar">
          {sources.map((a) =>
            a.kind === "video" ? (
              <video key={a.url} class={a.url === active.url ? "on" : ""} src={url(a.url, true)} autoplay loop muted playsInline onError={() => fail(a.url)} />
            ) : (
              <img key={a.url} class={a.url === active.url ? "on" : ""} src={url(a.url, true)} alt="" draggable={false} onError={() => fail(a.url)} />
            ),
          )}
        </div>
      )}
      <Ring class="ring" />
    </div>
  );
}
