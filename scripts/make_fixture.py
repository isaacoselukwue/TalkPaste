#!/usr/bin/env python3
"""Generate the small WAV test fixture (``tests/fixtures/short_en.wav``).

No text-to-speech engine is assumed to be present, so this produces a short,
*valid* 16 kHz mono PCM16 clip with a speech-like amplitude envelope and a
couple of formant-ish tones. It exists so the WAV-reading and pipeline tests
have a real file to chew on. It is NOT intelligible speech — to test real
transcription, drop an actual short English recording at the same path, or run
the ``requires_model`` test with faster-whisper + a model installed.

Usage:
    python scripts/make_fixture.py [output.wav] [--seconds 1.5]
"""

from __future__ import annotations

import argparse
import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16000


def synth(seconds: float) -> bytes:
    """Synthesise a speech-like PCM16 mono buffer."""

    n = int(SAMPLE_RATE * seconds)
    frames = bytearray()
    # A few "syllables": bursts of two formant tones under an envelope.
    formant_pairs = [(320, 800), (450, 1100), (300, 900), (500, 1400)]
    syllable = max(1, n // len(formant_pairs))
    for i in range(n):
        t = i / SAMPLE_RATE
        f1, f2 = formant_pairs[min(i // syllable, len(formant_pairs) - 1)]
        # Amplitude envelope per syllable (raised cosine) + global fade.
        local = (i % syllable) / syllable
        env = 0.5 - 0.5 * math.cos(2 * math.pi * local)
        fade = min(1.0, t * 8, (seconds - t) * 8)
        sample = env * fade * (
            0.6 * math.sin(2 * math.pi * f1 * t) + 0.4 * math.sin(2 * math.pi * f2 * t)
        )
        value = int(max(-1.0, min(1.0, sample)) * 20000)
        frames += struct.pack("<h", value)
    return bytes(frames)


def write_wav(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "short_en.wav"),
    )
    parser.add_argument("--seconds", type=float, default=1.4)
    args = parser.parse_args()

    out = Path(args.output)
    write_wav(out, synth(args.seconds))
    print(f"Wrote {out} ({args.seconds:.1f}s, {SAMPLE_RATE} Hz mono PCM16)")


if __name__ == "__main__":
    main()
