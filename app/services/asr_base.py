"""ASR backend interface and shared result types.

Every speech-to-text engine implements :class:`ASRBackend`. The controller and
CLI depend only on this interface, never on a concrete backend, so new engines
(whisper.cpp, a future Rust helper, a cloud stub for testing) can be dropped in
without touching call sites.

Audio is always passed as a mono ``float32`` numpy array in ``[-1.0, 1.0]`` at
the sample rate given (16 kHz by convention). Backends must not assume a GPU is
present; CPU is the default execution target.
"""

from __future__ import annotations

import abc
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing numpy at module import time
    import numpy as np

    from app.models import ASRSettings


@dataclass
class TranscriptionSegment:
    """A single timestamped segment returned by the backend."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    """The full transcription of one utterance."""

    text: str
    segments: list[TranscriptionSegment] = field(default_factory=list)
    language: str = "en"
    language_probability: float = 1.0
    #: Audio duration in seconds.
    duration: float = 0.0
    #: Wall-clock time the backend spent transcribing, in seconds.
    inference_seconds: float = 0.0
    #: Backend identifier (e.g. ``"faster_whisper"``).
    backend: str = ""
    #: Concrete model name/stem used.
    model: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def real_time_factor(self) -> float:
        """inference_seconds / duration (lower is faster than real time)."""

        return self.inference_seconds / self.duration if self.duration else 0.0


class ASRError(RuntimeError):
    """Raised when a backend cannot load a model or transcribe audio."""


class ASRBackend(abc.ABC):
    """Abstract speech-to-text backend.

    Lifecycle: construct cheaply (no model load), then :meth:`load` lazily
    when first needed, :meth:`transcribe` for each utterance, and
    :meth:`unload` to release memory.
    """

    #: Stable identifier used in results, logs and settings.
    name: str = "base"

    def __init__(self, settings: ASRSettings) -> None:
        self.settings = settings


    @abc.abstractmethod
    def load(self) -> None:
        """Load the model into memory. Idempotent. Raises :class:`ASRError`."""

    @abc.abstractmethod
    def is_loaded(self) -> bool:
        """Return whether the model is currently loaded."""

    def unload(self) -> None:
        """Release model resources. Default is a no-op; override if needed."""

    def ensure_loaded(self) -> None:
        """Load the model if it is not already loaded."""

        if not self.is_loaded():
            self.load()


    @abc.abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a mono float32 waveform. Raises :class:`ASRError`."""

    def transcribe_file(self, path: str | Path) -> TranscriptionResult:
        """Transcribe a WAV file. Provided once here so backends need not
        re-implement WAV decoding. Non-16k / non-mono files are resampled and
        down-mixed with numpy so no external audio library is required."""

        audio, sample_rate = read_wav_mono_float32(path)
        return self.transcribe(audio, sample_rate=sample_rate)


    def describe(self) -> str:
        """Human-readable one-line description for diagnostics."""

        model = self.settings.resolved_model()
        return f"{self.name} (model={model}, device={self.settings.device}, compute={self.settings.compute_type})"


# WAV helpers (numpy only — no soundfile/ffmpeg dependency)


def read_wav_mono_float32(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV file into a mono float32 array in ``[-1, 1]``.

    Supports 8/16/24/32-bit PCM and 32-bit float WAVs. Multi-channel audio is
    averaged to mono. Returns ``(samples, sample_rate)``.
    """

    import numpy as np

    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    samples = _pcm_bytes_to_float32(raw, sample_width)
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples.astype(np.float32, copy=False), sample_rate


def _pcm_bytes_to_float32(raw: bytes, sample_width: int) -> np.ndarray:
    import numpy as np

    if sample_width == 1:  # unsigned 8-bit
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (data - 128.0) / 128.0
    if sample_width == 2:  # signed 16-bit
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        return data / 32768.0
    if sample_width == 3:  # signed 24-bit packed
        a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        return ints.astype(np.float32) / 8388608.0
    if sample_width == 4:  # signed 32-bit int
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32)
        return data / 2147483648.0
    raise ASRError(f"Unsupported WAV sample width: {sample_width} bytes")


def resample_to_16k(audio: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    """Linear-resample a mono float32 signal. Adequate for speech ASR input."""

    import numpy as np

    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    duration = audio.shape[0] / float(src_rate)
    dst_len = int(round(duration * dst_rate))
    if dst_len <= 0:
        return np.zeros(0, dtype=np.float32)
    src_x = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    dst_x = np.linspace(0.0, duration, num=dst_len, endpoint=False)
    return np.interp(dst_x, src_x, audio).astype(np.float32)


def create_asr_backend(settings: ASRSettings) -> ASRBackend:
    """Factory: instantiate the configured ASR backend.

    Imports the concrete backend lazily so that, e.g., faster-whisper is only
    imported when actually selected. Raises :class:`ASRError` for unknown
    backends.
    """

    from app.models import ASRBackendKind

    if settings.backend is ASRBackendKind.FASTER_WHISPER:
        from app.services.asr_faster_whisper import FasterWhisperBackend

        return FasterWhisperBackend(settings)
    if settings.backend is ASRBackendKind.WHISPER_CPP:
        from app.services.asr_whisper_cpp import WhisperCppBackend

        return WhisperCppBackend(settings)
    raise ASRError(f"Unknown ASR backend: {settings.backend!r}")
