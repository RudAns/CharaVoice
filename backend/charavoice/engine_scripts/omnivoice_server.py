"""OmniVoice (k2-fsa, Apache-2.0 code) as a local engine process for CharaVoice.

Runs inside its own virtual environment (CUDA PyTorch + `omnivoice`), started by the
CharaVoice backend:

    <omnivoice venv>\\python.exe omnivoice_server.py --port 9881 --model-dir <dir>

POST /tts  {text, language?, ref_audio, ref_text, speed?, instruct?, stream?}
    Streams a WAV header followed by raw 16-bit PCM, one sentence at a time, the same
    framing GPT-SoVITS api_v2 uses, so the backend parses both engines identically.
GET /health
"""

import argparse
import io
import os
import traceback
import re
import threading
import wave

import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9881)
ap.add_argument("--model-dir", default="")
ap.add_argument("--device", default="cuda:0")
args = ap.parse_args()
if args.model_dir:
    os.environ.setdefault("HF_HOME", args.model_dir)

import torch  # noqa: E402
from omnivoice import OmniVoice  # noqa: E402
from omnivoice.models.omnivoice import OmniVoiceGenerationConfig  # noqa: E402

model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=args.device, dtype=torch.float16)
SR = int(model.sampling_rate or 24000)
lock = threading.Lock()
prompts: dict = {}
app = FastAPI()

# split after sentence punctuation (CJK and Latin); keep short pieces attached to the next one
_SPLIT = re.compile(r"(?<=[。！？!?；;…])|(?<=[.])\s+")


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 6:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out or [text]


def header(sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"")
    return buf.getvalue()


class TTSReq(BaseModel):
    text: str
    language: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    speed: float | None = None
    instruct: str | None = None
    stream: bool = True
    steps: int = 16  # diffusion steps: 32 is the library default, 16 is ~2x faster


def prompt_for(path: str, text: str | None):
    key = (path, text or "")
    if key not in prompts:
        prompts[key] = model.create_voice_clone_prompt(ref_audio=path, ref_text=text or None)
    return prompts[key]


def gen(req: TTSReq, piece: str) -> np.ndarray:
    kw = {"text": piece, "language": req.language or None, "speed": req.speed or None,
          "generation_config": OmniVoiceGenerationConfig(num_step=max(4, min(64, req.steps)))}
    if req.ref_audio:
        kw["voice_clone_prompt"] = prompt_for(req.ref_audio, req.ref_text)
    if req.instruct:  # voice design attributes; combined with a reference they steer the cloned voice
        kw["instruct"] = req.instruct
    with lock, torch.inference_mode():
        audio = model.generate(**kw)[0]
    return (np.clip(audio, -1, 1) * 32767).astype("<i2")


@app.get("/health")
def health():
    return {"ok": True, "sr": SR, "device": args.device}


@app.post("/tts")
def tts(req: TTSReq):
    try:
        pieces = sentences(req.text) if req.stream else [req.text]
        if req.ref_audio and not os.path.exists(req.ref_audio):
            return JSONResponse(status_code=400, content={"message": f"ref_audio not found: {req.ref_audio}"})

        def stream():
            yield header(SR)
            for i, p in enumerate(pieces):
                try:
                    pcm = gen(req, p)
                except Exception:  # the response has started; log and end it short
                    traceback.print_exc()
                    return
                if i:
                    yield np.zeros(int(SR * 0.12), "<i2").tobytes()  # short pause between sentences
                yield pcm.tobytes()

        return StreamingResponse(stream(), media_type="audio/wav")
    except Exception as e:  # report instead of crashing the process
        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
