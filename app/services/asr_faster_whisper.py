"""faster-whisper ASR backend.

Wraps `faster-whisper <https://github.com/SYSTRAN/faster-whisper>`_ (a
CTranslate2 reimplementation of OpenAI Whisper) behind the
:class:`~app.services.asr_base.ASRBackend` interface. faster-whisper is an
optional dependency: it is imported lazily inside :meth:`FasterWhisperBackend.load`
so importing this module never requires it to be installed.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.services.asr_base import (
    ASRBackend,
    ASRError,
    TranscriptionResult,
    TranscriptionSegment,
    resample_to_16k,
)

if TYPE_CHECKING:  # avoid importing numpy at module import time
    import numpy as np

    from app.models import ASRSettings

log = get_logger("asr.faster_whisper")


class FasterWhisperBackend(ASRBackend):
    """Speech-to-text backend powered by faster-whisper (CTranslate2).

    The model is loaded lazily on first use via :meth:`load`. All heavy or
    optional dependencies (``faster_whisper``, ``numpy``) are imported inside
    methods, so constructing the backend is cheap and import-safe.
    """

    name = "faster_whisper"

    def __init__(self, settings: ASRSettings) -> None:
        """Initialise the backend without loading any model.

        Args:
            settings: The resolved ASR settings controlling model, device and
                decoding parameters.
        """

        super().__init__(settings)
        self._model: Any | None = None
        self._model_name: str = ""


    def load(self) -> None:
        """Load the Whisper model into memory.

        Imports ``faster_whisper`` lazily and constructs a ``WhisperModel``
        using the resolved model name (or an explicit ``model_path``), the
        configured device, compute type and CPU thread count. Downloaded model
        files are cached under the application's models directory. Idempotent:
        a second call is a no-op while the model stays loaded.

        Raises:
            ASRError: If ``faster_whisper`` is not installed or the model
                cannot be constructed/downloaded.
        """

        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # ImportError and any transitive failure
            raise ASRError(
                "The 'faster-whisper' package is required for the "
                "faster_whisper backend but could not be imported. "
                "Install it with `pip install faster-whisper` "
                "(this also pulls in the CTranslate2 runtime)."
            ) from exc

        # Resolve the models cache directory lazily so importing this module
        # does not pull in app.config at import time.
        try:
            from app.config import get_paths

            download_root = str(get_paths().models_dir)
        except Exception as exc:  # pragma: no cover - defensive
            raise ASRError(
                f"Could not determine the models cache directory: {exc}"
            ) from exc

        model_ref = self.settings.model_path or self.settings.resolved_model()

        log.info(
            "Loading faster-whisper model=%s device=%s compute_type=%s "
            "cpu_threads=%s (download_root=%s)",
            model_ref,
            self.settings.device,
            self.settings.compute_type,
            self.settings.cpu_threads,
            download_root,
        )

        start = time.perf_counter()
        try:
            self._model = WhisperModel(
                model_ref,
                device=self.settings.device,
                compute_type=self.settings.compute_type,
                cpu_threads=self.settings.cpu_threads,
                download_root=download_root,
            )
        except Exception as exc:
            raise ASRError(
                f"Failed to load faster-whisper model {model_ref!r} "
                f"(device={self.settings.device}, "
                f"compute_type={self.settings.compute_type}): {exc}. "
                "Check the model name/path is valid, that you have network "
                "access for the first download, and that the compute_type is "
                "supported on this device (try compute_type='int8' on CPU)."
            ) from exc

        self._model_name = model_ref
        elapsed = time.perf_counter() - start
        log.info("faster-whisper model %r loaded in %.2fs", model_ref, elapsed)

    def is_loaded(self) -> bool:
        """Return ``True`` when a model is currently held in memory."""

        return self._model is not None

    def unload(self) -> None:
        """Release the loaded model and its memory."""

        if self._model is not None:
            log.info("Unloading faster-whisper model %r", self._model_name)
        self._model = None
        self._model_name = ""


    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a mono float32 waveform.

        Args:
            audio: Mono ``float32`` samples in ``[-1.0, 1.0]``.
            sample_rate: Sample rate of ``audio``; resampled to 16 kHz if
                different.
            language: Explicit language code, or ``None`` to fall back to the
                configured language (``ASRSettings.resolved_language``).

        Returns:
            A :class:`TranscriptionResult` with joined text, per-segment
            timings and diagnostic metadata. Empty audio yields an empty
            result without invoking the model.

        Raises:
            ASRError: If the model is not loaded or inference fails.
        """

        import numpy as np

        # Normalise to a contiguous mono float32 array first so we can
        # short-circuit empty audio WITHOUT triggering a (potentially slow or
        # failing) model load — as the docstring promises.
        samples = np.asarray(audio, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples.reshape(samples.shape[0], -1).mean(axis=1)
        samples = np.ascontiguousarray(samples, dtype=np.float32)

        if samples.size == 0:
            log.debug("Empty audio passed to transcribe; returning empty result.")
            return TranscriptionResult(
                text="",
                segments=[],
                language=language or self.settings.resolved_language() or "en",
                language_probability=0.0,
                duration=0.0,
                inference_seconds=0.0,
                backend=self.name,
                model=self._model_name,
            )

        self.ensure_loaded()
        if self._model is None:  # pragma: no cover - ensure_loaded guarantees
            raise ASRError("faster-whisper model is not loaded.")

        if sample_rate != 16000:
            log.debug("Resampling audio from %d Hz to 16 kHz", sample_rate)
            samples = resample_to_16k(samples, sample_rate)

        resolved_language = language or self.settings.resolved_language()

        vad_enabled = bool(self.settings.vad.enabled)
        vad_parameters = (
            self.settings.vad.to_faster_whisper_params() if vad_enabled else None
        )

        log.info(
            "Transcribing %.2fs of audio (model=%s, language=%s, beam_size=%d, "
            "vad=%s)",
            samples.shape[0] / 16000.0,
            self._model_name,
            resolved_language or "auto",
            self.settings.beam_size,
            vad_enabled,
        )

        start = time.perf_counter()
        try:
            segments_gen, info = self._model.transcribe(
                samples,
                language=resolved_language,
                beam_size=self.settings.beam_size,
                temperature=self.settings.temperature,
                vad_filter=vad_enabled,
                vad_parameters=vad_parameters,
            )
            # faster-whisper streams segments from a generator; realise them
            # here so all inference (and its wall-clock cost) is captured.
            segments: list[TranscriptionSegment] = []
            text_parts: list[str] = []
            for seg in segments_gen:
                seg_text = (seg.text or "").strip()
                segments.append(
                    TranscriptionSegment(
                        start=float(seg.start),
                        end=float(seg.end),
                        text=seg_text,
                    )
                )
                if seg_text:
                    text_parts.append(seg_text)
        except Exception as exc:
            raise ASRError(
                f"faster-whisper transcription failed: {exc}"
            ) from exc

        inference_seconds = time.perf_counter() - start

        full_text = " ".join(text_parts)
        # Collapse any incidental double spaces from joining stripped parts.
        full_text = " ".join(full_text.split())

        result = TranscriptionResult(
            text=full_text,
            segments=segments,
            language=getattr(info, "language", None) or resolved_language or "en",
            language_probability=float(
                getattr(info, "language_probability", 1.0) or 0.0
            ),
            duration=float(
                getattr(info, "duration", samples.shape[0] / 16000.0)
            ),
            inference_seconds=inference_seconds,
            backend=self.name,
            model=self._model_name,
        )
        log.info(
            "Transcription done: %d chars, %d segments, %.2fs inference "
            "(RTF=%.2f)",
            len(result.text),
            len(result.segments),
            inference_seconds,
            result.real_time_factor,
        )
        return result
