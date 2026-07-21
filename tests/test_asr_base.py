"""Tests for the ASR base helpers: WAV reading, resampling, and the factory."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from app.models import ASRBackendKind, ASRSettings
from app.services.asr_base import (
    ASRError,
    TranscriptionResult,
    create_asr_backend,
    read_wav_mono_float32,
    resample_to_16k,
)


def _write_wav(path, data: np.ndarray, rate: int, sampwidth: int, channels: int):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())


def test_read_16bit_mono(tmp_path):
    path = tmp_path / "a.wav"
    samples = (np.array([0, 16384, -16384, 32767], dtype="<i2"))
    _write_wav(path, samples, 16000, 2, 1)
    audio, rate = read_wav_mono_float32(path)
    assert rate == 16000
    assert audio.dtype == np.float32
    assert audio.shape == (4,)
    assert abs(audio[1] - 0.5) < 0.01


def test_read_8bit(tmp_path):
    path = tmp_path / "b.wav"
    samples = np.array([128, 255, 0, 128], dtype=np.uint8)
    _write_wav(path, samples, 8000, 1, 1)
    audio, rate = read_wav_mono_float32(path)
    assert rate == 8000
    assert abs(audio[0]) < 0.02  # 128 ~= 0


def test_read_stereo_downmixed_to_mono(tmp_path):
    path = tmp_path / "c.wav"
    # Interleaved L/R: L=+full, R=-full -> average ~0.
    inter = np.array([16384, -16384, 16384, -16384], dtype="<i2")
    _write_wav(path, inter, 16000, 2, 2)
    audio, _ = read_wav_mono_float32(path)
    assert audio.shape == (2,)
    assert abs(audio[0]) < 0.01


def test_read_fixture(sample_wav):
    audio, rate = read_wav_mono_float32(sample_wav)
    assert rate == 16000
    assert audio.dtype == np.float32
    assert len(audio) > 0


def test_resample_noop_when_same_rate():
    x = np.linspace(-1, 1, 100, dtype=np.float32)
    out = resample_to_16k(x, 16000)
    assert out.shape == x.shape


def test_resample_changes_length():
    x = np.zeros(16000, dtype=np.float32)  # 1s @ 16k
    out = resample_to_16k(x, 8000)  # claim it was 8k -> expect ~2x length
    assert abs(len(out) - 32000) <= 2


def test_resample_empty():
    out = resample_to_16k(np.zeros(0, dtype=np.float32), 8000)
    assert out.shape == (0,)


def test_factory_faster_whisper():
    s = ASRSettings(backend=ASRBackendKind.FASTER_WHISPER)
    backend = create_asr_backend(s)
    assert backend.name == "faster_whisper"
    assert not backend.is_loaded()


def test_factory_whisper_cpp():
    s = ASRSettings(backend=ASRBackendKind.WHISPER_CPP)
    backend = create_asr_backend(s)
    assert backend.name == "whisper_cpp"


def test_transcription_result_helpers():
    r = TranscriptionResult(text="  ", duration=2.0, inference_seconds=1.0)
    assert r.is_empty
    assert r.real_time_factor == 0.5
    assert TranscriptionResult(text="hi").is_empty is False


def test_unsupported_sample_width_raises(tmp_path):
    # Craft a WAV-like file we can read raw; 5-byte width is invalid.
    from app.services.asr_base import _pcm_bytes_to_float32

    with pytest.raises(ASRError):
        _pcm_bytes_to_float32(b"\x00" * 10, 5)
