"""whisper.cpp ASR backend (optional, low-RAM).

Provides a :class:`~app.services.asr_base.ASRBackend` implementation on top of
`whisper.cpp <https://github.com/ggerganov/whisper.cpp>`_. Two execution paths
are supported, preferred in this order:

1. The ``pywhispercpp`` Python bindings, if importable.
2. An external whisper.cpp command-line binary (``whisper-cli``, ``whisper`` or
   ``main``) discovered on ``PATH`` or next to the configured model file. The
   float32 audio is written to a temporary 16 kHz / mono / 16-bit WAV and the
   binary is invoked with ``-otxt -nt`` to emit a plain transcript.

Both the bindings and the binary are optional and are only touched inside
methods, so importing this module never requires whisper.cpp to be present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
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

log = get_logger("asr.whisper_cpp")

#: Candidate names for a whisper.cpp CLI binary, most specific first.
_BINARY_CANDIDATES = ("whisper-cli", "whisper-cpp", "whisper", "main")

#: How long (seconds) to allow a single CLI transcription before giving up.
_CLI_TIMEOUT_SECONDS = 600.0


class WhisperCppBackend(ASRBackend):
    """Low-RAM speech-to-text backend backed by whisper.cpp.

    Uses the ``pywhispercpp`` bindings when available, otherwise shells out to
    a whisper.cpp CLI binary. The chosen path is resolved lazily in
    :meth:`load` and logged explicitly.
    """

    name = "whisper_cpp"

    def __init__(self, settings: ASRSettings) -> None:
        """Initialise the backend without loading any model.

        Args:
            settings: The resolved ASR settings controlling model resolution
                and decoding parameters.
        """

        super().__init__(settings)
        #: One of ``"bindings"`` or ``"binary"`` once loaded, else ``""``.
        self._mode: str = ""
        #: The ``pywhispercpp`` model instance when using the bindings path.
        self._model: Any | None = None
        #: Path to the CLI binary when using the subprocess path.
        self._binary: Path | None = None
        #: Path to the ggml model file (resolved on load).
        self._model_path: Path | None = None
        self._model_name: str = ""


    def _resolve_model_path(self) -> Path | None:
        """Locate the ggml ``.bin`` model file, or ``None`` if not found.

        Prefers an explicit ``settings.model_path``. Otherwise looks in the
        application models directory for a file named after the resolved model
        (e.g. ``ggml-base.en.bin`` or ``base.en.bin``).
        """

        if self.settings.model_path:
            path = Path(self.settings.model_path).expanduser()
            return path if path.is_file() else None

        stem = self.settings.resolved_model()  # e.g. "base.en"
        try:
            from app.config import get_paths

            models_dir = get_paths().models_dir
        except Exception:  # pragma: no cover - defensive
            return None

        candidates = [
            models_dir / f"ggml-{stem}.bin",
            models_dir / f"{stem}.bin",
            models_dir / f"ggml-{stem}.gguf",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        # Fall back to any ggml .bin that mentions the stem.
        if models_dir.is_dir():
            for candidate in sorted(models_dir.glob("*.bin")):
                if stem in candidate.name:
                    return candidate
        return None

    def _find_binary(self) -> Path | None:
        """Find a whisper.cpp CLI binary on PATH or beside the model file."""

        for name in _BINARY_CANDIDATES:
            found = shutil.which(name)
            if found:
                return Path(found)

        # Look next to the model file (common when built locally).
        search_dirs: list[Path] = []
        if self._model_path is not None:
            search_dirs.append(self._model_path.parent)
        if self.settings.model_path:
            search_dirs.append(Path(self.settings.model_path).expanduser().parent)

        for directory in search_dirs:
            for name in _BINARY_CANDIDATES:
                for candidate in (directory / name, directory / f"{name}.exe"):
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        return candidate
        return None


    def load(self) -> None:
        """Prepare the backend for transcription.

        Resolves the ggml model file, then selects an execution path: the
        ``pywhispercpp`` bindings if importable, otherwise a CLI binary.
        Idempotent while loaded.

        Raises:
            ASRError: If no model file can be found, or neither the bindings
                nor a CLI binary are available. The message explains how to
                install whisper.cpp and where to place the ggml model.
        """

        if self._mode:
            return

        model_path = self._resolve_model_path()
        if model_path is None:
            hint_dir = self._models_dir_hint()
            stem = self.settings.resolved_model()
            raise ASRError(
                "No whisper.cpp ggml model file was found. Set asr.model_path "
                "to a .bin file, or place one named "
                f"'ggml-{stem}.bin' in {hint_dir}. Download models from "
                "https://huggingface.co/ggerganov/whisper.cpp (e.g. "
                f"ggml-{stem}.bin)."
            )
        self._model_path = model_path
        self._model_name = model_path.name

        # Path 1: pywhispercpp bindings.
        try:
            from pywhispercpp.model import Model as _PwcModel
        except Exception:
            _PwcModel = None  # bindings not installed; try the binary path.

        if _PwcModel is not None:
            log.info(
                "Loading whisper.cpp via pywhispercpp bindings (model=%s)",
                model_path,
            )
            start = time.perf_counter()
            try:
                self._model = _PwcModel(
                    model=str(model_path),
                    n_threads=self.settings.cpu_threads,
                    print_progress=False,
                    print_realtime=False,
                )
            except Exception as exc:
                raise ASRError(
                    f"Failed to initialise pywhispercpp model "
                    f"{model_path}: {exc}"
                ) from exc
            self._mode = "bindings"
            log.info(
                "pywhispercpp model loaded in %.2fs",
                time.perf_counter() - start,
            )
            return

        # Path 2: external CLI binary.
        binary = self._find_binary()
        if binary is not None:
            self._binary = binary
            self._mode = "binary"
            log.info(
                "Using whisper.cpp CLI binary %s (model=%s)",
                binary,
                model_path,
            )
            return

        raise ASRError(
            "whisper.cpp is unavailable: neither the 'pywhispercpp' bindings "
            "nor a CLI binary (one of "
            f"{', '.join(_BINARY_CANDIDATES)}) could be found. Install the "
            "bindings with `pip install pywhispercpp`, or build whisper.cpp "
            "(https://github.com/ggerganov/whisper.cpp) and put its "
            "'whisper-cli' binary on your PATH. A ggml model file was located "
            f"at {model_path}."
        )

    def _models_dir_hint(self) -> str:
        try:
            from app.config import get_paths

            return str(get_paths().models_dir)
        except Exception:  # pragma: no cover - defensive
            return "the models directory"

    def is_loaded(self) -> bool:
        """Return whether an execution path has been resolved."""

        return bool(self._mode)

    def unload(self) -> None:
        """Release any loaded bindings model and reset the execution path."""

        if self._mode:
            log.info("Unloading whisper.cpp backend (mode=%s)", self._mode)
        self._model = None
        self._binary = None
        self._mode = ""


    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a mono float32 waveform via whisper.cpp.

        Args:
            audio: Mono ``float32`` samples in ``[-1.0, 1.0]``.
            sample_rate: Sample rate of ``audio``; resampled to 16 kHz if
                different.
            language: Explicit language code, or ``None`` to fall back to the
                configured language.

        Returns:
            A :class:`TranscriptionResult`. Depending on the path, segments may
            be per-utterance (bindings) or a single segment covering the whole
            clip (CLI). Empty audio yields an empty result.

        Raises:
            ASRError: If the backend is not loaded or transcription fails.
        """

        import numpy as np

        self.ensure_loaded()

        samples = np.asarray(audio, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples.reshape(samples.shape[0], -1).mean(axis=1)
        samples = np.ascontiguousarray(samples, dtype=np.float32)

        resolved_language = language or self.settings.resolved_language()

        if samples.size == 0:
            log.debug("Empty audio passed to transcribe; returning empty result.")
            return TranscriptionResult(
                text="",
                segments=[],
                language=resolved_language or "en",
                language_probability=0.0,
                duration=0.0,
                inference_seconds=0.0,
                backend=self.name,
                model=self._model_name,
            )

        if sample_rate != 16000:
            log.debug("Resampling audio from %d Hz to 16 kHz", sample_rate)
            samples = resample_to_16k(samples, sample_rate)

        duration = samples.shape[0] / 16000.0
        log.info(
            "Transcribing %.2fs of audio via whisper.cpp (mode=%s, language=%s)",
            duration,
            self._mode,
            resolved_language or "auto",
        )

        if self._mode == "bindings":
            text, segments, inference_seconds = self._transcribe_bindings(
                samples, resolved_language
            )
        else:
            text, segments, inference_seconds = self._transcribe_binary(
                samples, resolved_language
            )

        # If we produced no explicit segments, cover the whole clip with one.
        if not segments and text:
            segments = [TranscriptionSegment(start=0.0, end=duration, text=text)]

        result = TranscriptionResult(
            text=text,
            segments=segments,
            language=resolved_language or "en",
            language_probability=1.0,
            duration=duration,
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

    def _transcribe_bindings(
        self, samples: np.ndarray, language: str | None
    ) -> tuple[str, list[TranscriptionSegment], float]:
        """Transcribe using the pywhispercpp bindings.

        Returns:
            A ``(text, segments, inference_seconds)`` tuple.
        """

        if self._model is None:  # pragma: no cover - ensure_loaded guarantees
            raise ASRError("pywhispercpp model is not loaded.")

        kwargs: dict[str, Any] = {}
        if language:
            kwargs["language"] = language

        start = time.perf_counter()
        try:
            raw_segments = self._model.transcribe(samples, **kwargs)
        except TypeError:
            # Older/newer binding signatures may reject kwargs; retry plainly.
            try:
                raw_segments = self._model.transcribe(samples)
            except Exception as exc:
                raise ASRError(
                    f"pywhispercpp transcription failed: {exc}"
                ) from exc
        except Exception as exc:
            raise ASRError(f"pywhispercpp transcription failed: {exc}") from exc
        inference_seconds = time.perf_counter() - start

        segments: list[TranscriptionSegment] = []
        text_parts: list[str] = []
        for seg in raw_segments or []:
            seg_text = str(getattr(seg, "text", "") or "").strip()
            # pywhispercpp reports timestamps in centiseconds (t0/t1).
            t0 = float(getattr(seg, "t0", 0.0) or 0.0) / 100.0
            t1 = float(getattr(seg, "t1", 0.0) or 0.0) / 100.0
            if seg_text:
                segments.append(
                    TranscriptionSegment(start=t0, end=t1, text=seg_text)
                )
                text_parts.append(seg_text)

        text = " ".join(" ".join(text_parts).split())
        return text, segments, inference_seconds

    def _transcribe_binary(
        self, samples: np.ndarray, language: str | None
    ) -> tuple[str, list[TranscriptionSegment], float]:
        """Transcribe by shelling out to a whisper.cpp CLI binary.

        Writes the audio to a temporary 16 kHz / mono / 16-bit WAV, invokes the
        binary with ``-otxt -nt`` and reads back the produced ``.txt``.

        Returns:
            A ``(text, segments, inference_seconds)`` tuple (segments empty; the
            caller wraps the text in a single whole-clip segment).
        """

        import numpy as np

        if self._binary is None or self._model_path is None:  # pragma: no cover
            raise ASRError("whisper.cpp CLI binary is not resolved.")

        tmp_dir = tempfile.mkdtemp(prefix="talkpaste_wcpp_")
        wav_path = Path(tmp_dir) / "clip.wav"
        txt_path = Path(str(wav_path) + ".txt")
        try:
            # Convert float32 [-1, 1] to signed 16-bit PCM and write a WAV.
            clipped = np.clip(samples, -1.0, 1.0)
            pcm16 = (clipped * 32767.0).astype("<i2")
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm16.tobytes())

            cmd = [
                str(self._binary),
                "-m",
                str(self._model_path),
                "-f",
                str(wav_path),
                "-otxt",
                "-nt",
                "-t",
                str(self.settings.cpu_threads),
            ]
            if language:
                cmd += ["-l", language]

            log.debug("Running whisper.cpp: %s", " ".join(cmd))
            start = time.perf_counter()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=_CLI_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ASRError(
                    "whisper.cpp CLI timed out after "
                    f"{_CLI_TIMEOUT_SECONDS:.0f}s. Try a smaller model or "
                    "shorter audio."
                ) from exc
            except OSError as exc:
                raise ASRError(
                    f"Failed to run whisper.cpp binary {self._binary}: {exc}"
                ) from exc
            inference_seconds = time.perf_counter() - start

            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                raise ASRError(
                    f"whisper.cpp exited with code {proc.returncode}: "
                    f"{stderr or '(no stderr)'}"
                )

            text = self._read_cli_output(txt_path, proc.stdout)
            return text, [], inference_seconds
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _read_cli_output(txt_path: Path, stdout: bytes) -> str:
        """Read the transcript from the ``.txt`` file, or fall back to stdout."""

        text = ""
        if txt_path.is_file():
            try:
                text = txt_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover - filesystem dependent
                log.warning("Could not read whisper.cpp output %s: %s", txt_path, exc)
        if not text.strip():
            # Some builds print the transcript to stdout instead.
            text = stdout.decode("utf-8", errors="replace")
        return " ".join(text.split())
