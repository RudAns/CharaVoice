// Transcript review for a material folder (simple mode): the reference clip's text and every
// sentence the material pipeline cut out. Sentences where two recognitions disagree (or that fail
// plausibility checks) are marked; a suggested reading can be taken over with one click.
// The reference text is used by synthesis right away; sentence texts are training data, used the
// next time the folder is learned again.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, url } from "../api";
import { notify } from "../store";

interface Item {
  id: string;
  text: string;
  duration: number;
  deleted: boolean;
  edited: boolean;
  doubt: string | null;
  suggest: string | null;
}
interface Data {
  ref: { text: string; duration: number; item?: string; doubt: string | null; suggest: string | null } | null;
  items: Item[];
  doubtful: number;
}

export function Transcripts({ folder, name, onClose }: { folder: string; name: string; onClose: (changed: boolean) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [onlyDoubt, setOnlyDoubt] = useState(false);
  const [refText, setRefText] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);

  async function load() {
    try {
      const d = await api<Data>(`/v1/simple/folders/${folder}/transcripts`);
      setData(d);
      setRefText(d.ref?.text ?? "");
      if (d.doubtful && !data) setOnlyDoubt(true);
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  useEffect(() => {
    load();
    return () => audio.current?.pause();
  }, [folder]);

  function play(key: string, src: string) {
    audio.current?.pause();
    if (playing === key) return setPlaying(null);
    const a = new Audio(url(src, true));
    a.onended = () => setPlaying(null);
    a.play();
    audio.current = a;
    setPlaying(key);
  }
  async function saveItem(it: Item, patch: { text?: string; deleted?: boolean }) {
    try {
      await api(`/v1/simple/folders/${folder}/transcripts/${it.id}`, { method: "PATCH", json: patch });
      setChanged(true);
      await load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }
  async function saveRef(body: { text?: string; item?: string }) {
    try {
      await api(`/v1/simple/folders/${folder}/ref`, { method: "POST", json: body });
      setChanged(true);
      notify(body.item ? "已设为参考音频，朗读立即使用" : "参考文字已保存，朗读立即使用");
      await load();
    } catch (e: any) {
      notify(e.message, "error");
    }
  }

  const items = (data?.items ?? []).filter((i) => !onlyDoubt || i.doubt);
  return (
    <div class="modal-back">
      <div class="modal transcripts" role="dialog" aria-modal="true" aria-label="转写">
        <div class="editor-head">
          <h2>转写 · {name}</h2>
          <div class="grow" />
          {data && (
            <label class="check slim">
              <input type="checkbox" checked={onlyDoubt} onChange={(e) => setOnlyDoubt((e.target as HTMLInputElement).checked)} />
              只看可能有误（{data.doubtful}）
            </label>
          )}
          <button onClick={() => onClose(changed)}>关闭</button>
        </div>
        {!data ? (
          <p class="muted">正在复核转写（第一次打开要把每句再识别一遍，约几秒）…</p>
        ) : (
          <>
            {data.ref && (
              <div class="tr-ref">
                <div class="tr-ref-head">
                  <button class="play" onClick={() => play("ref", `/v1/voices/${folder}/ref/audio`)}>{playing === "ref" ? "■" : "▶"}</button>
                  <strong>参考音频</strong>
                  <span class="muted">{data.ref.duration.toFixed(1)} 秒 · 决定音色和语气，改了立即用于朗读</span>
                </div>
                <div class="tr-row-edit">
                  <input lang="ja" value={refText} onInput={(e) => setRefText((e.target as HTMLInputElement).value)} onKeyDown={(e) => e.key === "Enter" && saveRef({ text: refText })} />
                  <button class="small primary" disabled={refText.trim() === data.ref.text} onClick={() => saveRef({ text: refText })}>保存</button>
                </div>
                {(data.ref.doubt || data.ref.suggest) && (
                  <div class="tr-hint">
                    {data.ref.doubt && <span class="tr-doubt">⚠ {data.ref.doubt}</span>}
                    {data.ref.suggest && (
                      <span>
                        建议：<span lang="ja">{data.ref.suggest}</span>
                        <button class="small" onClick={() => setRefText(data.ref!.suggest!)}>填入</button>
                      </span>
                    )}
                  </div>
                )}
              </div>
            )}
            <p class="muted tr-note">
              下面是从片段里切出的句子，训练就用这些文字。听一下，把错字改对（回车保存），或者排除说错、混了别人声音的句子。改完后点“重新学习”，会沿用这些句子直接训练，不再重新整理。
            </p>
            <div class="tr-list">
              {items.length === 0 && <p class="muted">{onlyDoubt ? "没有可能有误的句子。" : "还没有句子，先点“克隆这个声线”。"}</p>}
              {items.map((it) => (
                <Row
                  key={it.id}
                  it={it}
                  isRef={data.ref?.item === it.id}
                  playing={playing === it.id}
                  onPlay={() => play(it.id, `/v1/voices/${folder}/dataset/items/${it.id}/audio`)}
                  onSave={(p) => saveItem(it, p)}
                  onRef={() => saveRef({ item: it.id })}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Row({ it, isRef, playing, onPlay, onSave, onRef }: {
  it: Item;
  isRef: boolean;
  playing: boolean;
  onPlay: () => void;
  onSave: (p: { text?: string; deleted?: boolean }) => void;
  onRef: () => void;
}) {
  const [text, setText] = useState(it.text);
  useEffect(() => setText(it.text), [it.text]);
  const dirty = text.trim() !== it.text;
  return (
    <div class={`tr-row ${it.deleted ? "out" : ""} ${it.doubt ? "doubt" : ""}`}>
      <div class="tr-row-edit">
        <button class="play" onClick={onPlay}>{playing ? "■" : "▶"}</button>
        <input lang="ja" value={text} disabled={it.deleted} onInput={(e) => setText((e.target as HTMLInputElement).value)} onKeyDown={(e) => e.key === "Enter" && dirty && onSave({ text })} />
        <span class="dur">{it.duration.toFixed(1)} 秒</span>
        {dirty ? (
          <button class="small primary" onClick={() => onSave({ text })}>保存</button>
        ) : (
          <button class="small" onClick={() => onSave({ deleted: !it.deleted })}>{it.deleted ? "恢复" : "排除"}</button>
        )}
        {isRef ? <span class="tr-tag">参考</span> : !it.deleted && <button class="small" title="用这一句当参考音频" onClick={onRef}>设为参考</button>}
      </div>
      {!it.deleted && (it.doubt || it.suggest || it.edited) && (
        <div class="tr-hint">
          {it.doubt && <span class="tr-doubt">⚠ {it.doubt}</span>}
          {it.edited && <span class="tr-edited">已手动修改</span>}
          {it.suggest && !it.edited && (
            <span>
              另一种识别：<span lang="ja">{it.suggest}</span>
              <button class="small" onClick={() => setText(it.suggest!)}>填入</button>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
