// Character showcase: avatar (per-state image/loop) composited with the presence ring.
import { presence, url, type AvatarSlot, type Voice } from "../api";
import { Ring } from "./Ring";

const SLOT_FOR: Record<string, AvatarSlot> = { speaking: "speaking", listening: "listening", idle: "idle", thinking: "idle" };

export function Showcase({ voice, compact = false }: { voice?: Voice; compact?: boolean }) {
  const st = presence.value;
  const mode = voice?.avatar_mode ?? "circle";
  const av = voice?.avatar;
  const pick = (slot: AvatarSlot) => av?.[slot] ?? av?.default ?? null;
  const active = pick(SLOT_FOR[st]);
  // Keep every distinct source mounted so videos stay warm and state switches cross-fade instantly.
  const sources = av
    ? Array.from(new Map((["default", "idle", "listening", "speaking"] as AvatarSlot[]).map((s) => av[s]).filter(Boolean).map((a) => [a!.url, a!])).values())
    : [];

  return (
    <div class={`showcase mode-${mode} state-${st} ${compact ? "compact" : ""}`}>
      {mode === "free" && <Ring class="ring" />}
      <div class="avatar">
        {sources.length === 0 && <div class="avatar-empty">{voice ? voice.name.slice(0, 1) : "?"}</div>}
        {sources.map((a) =>
          a.kind === "video" ? (
            <video key={a.url} class={a.url === active?.url ? "on" : ""} src={url(a.url, true)} autoplay loop muted playsInline />
          ) : (
            <img key={a.url} class={a.url === active?.url ? "on" : ""} src={url(a.url, true)} alt="" draggable={false} />
          ),
        )}
      </div>
      {mode !== "free" && <Ring class="ring" />}
    </div>
  );
}
