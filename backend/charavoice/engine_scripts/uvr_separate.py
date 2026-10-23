"""Vocal separation driver, executed by the GPT-SoVITS package's own Python runtime.

It only calls the package's UVR5 loaders (tools/uvr5); no UVR code is copied here.

    runtime\\python.exe -I <this file> <engine_dir> <model_name> <input.wav> <vocal_dir> <other_dir> <device> <is_half>

model_name is a file stem from tools/uvr5/uvr5_weights, e.g.
"model_bs_roformer_ep_317_sdr_12.9755" (BS-Roformer) or "HP5_only_main_vocal".
Input must already be 44.1 kHz stereo WAV. Prints "UVR_DONE <vocal file>" on success.
"""

import glob
import os
import sys


def main() -> None:
    engine_dir, model_name, inp, vocal_dir, other_dir, device, is_half = sys.argv[1:8]
    os.chdir(engine_dir)
    sys.path[:0] = [engine_dir, os.path.join(engine_dir, "tools", "uvr5")]
    from tools.portable_runtime import activate

    activate(change_cwd=True)
    import torch

    half = is_half == "1" and device.startswith("cuda") and torch.cuda.is_available()
    weights = os.path.join("tools", "uvr5", "uvr5_weights")
    if "roformer" in model_name.lower():
        from bsroformer import Roformer_Loader

        pre = Roformer_Loader(
            model_path=os.path.join(weights, model_name + ".ckpt"),
            config_path=os.path.join(weights, model_name + ".yaml"),
            device=device,
            is_half=half,
        )
    else:
        from vr import AudioPre, AudioPreDeEcho

        cls = AudioPreDeEcho if "DeEcho" in model_name else AudioPre
        pre = cls(agg=10, model_path=os.path.join(weights, model_name + ".pth"), device=device, is_half=half)
    os.makedirs(vocal_dir, exist_ok=True)
    os.makedirs(other_dir, exist_ok=True)
    before = set(glob.glob(os.path.join(vocal_dir, "*")))
    pre._path_audio_(inp, other_dir, vocal_dir, "wav", "HP3" in model_name)
    produced = sorted(set(glob.glob(os.path.join(vocal_dir, "*"))) - before, key=os.path.getmtime)
    if not produced:
        print("UVR_FAILED no output", flush=True)
        sys.exit(2)
    print("UVR_DONE " + produced[-1], flush=True)


if __name__ == "__main__":
    main()
