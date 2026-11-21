"""Cut candidate backchannel clips (the engine's own "mm-hm"/"yeah") from replay AI tracks.

Every AI talkspurt of 0.25-0.7 s that dessa_timing counts as a backchannel (inside the human's talkspurt) is written
as 48 kHz mono s16 WAV to OUT_DIR, padded 30 ms each side, with a 10 ms fade. Pick clips by ear or ASR.

  python bench/bc_clips.py OUT_DIR RUN_DIR...
"""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import dessa_timing as dt  # noqa: E402
import subprocess  # noqa: E402


def pcm48(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16)


def main():
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    for run in sys.argv[2:]:
        for hw in sorted(Path(run).glob("*-human.wav")):
            aw = hw.with_name(hw.name.replace("-human", "-ai"))
            hs, as_ = dt.spurts(dt.pcm(hw)), dt.spurts(dt.pcm(aw))
            x = None
            for s, e in as_:
                if not 0.25 <= e - s <= 0.7 or not any(a <= s and e <= b for a, b in hs):
                    continue
                x = pcm48(aw) if x is None else x
                seg = x[int((s - 0.03) * 48000): int((e + 0.03) * 48000)].astype(np.float32)
                ramp = np.linspace(0, 1, 480)
                seg[:480] *= ramp; seg[-480:] *= ramp[::-1]
                name = out / f"{Path(run).name}-{hw.name.split('-human')[0]}-{s:.2f}.wav"
                with wave.open(str(name), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
                    w.writeframes(seg.astype(np.int16).tobytes())
                print(name.name, round(e - s, 2), int(np.abs(seg).max()))


if __name__ == "__main__":
    main()
