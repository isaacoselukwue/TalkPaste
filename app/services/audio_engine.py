"""Microphone capture engine for TalkPaste.

Audio is captured with a callback-driven PortAudio input stream (via
:mod:`sounddevice`) at 16 kHz / mono / ``float32`` — exactly the format the
Whisper family of models expects, so no post-capture resampling is required in
the common case. Blocks delivered by PortAudio are copied (PortAudio reuses its
own buffer) and appended to an internal, bounded ring buffer whose capacity is
``settings.max_seconds``; older audio is dropped once the ceiling is reached so
a runaway recording can never exhaust memory.

All heavy/optional dependencies (:mod:`sounddevice`, :mod:`numpy`) are imported
lazily inside functions so merely importing this module never requires PortAudio
to be installed. When it *is* missing, callers get a clear :class:`AudioError`
with an install hint rather than a bare ``ImportError``.

The engine is thread-safe: the PortAudio callback runs on a dedicated audio
thread while :meth:`start`/:meth:`stop` are called from the controller thread,
so the shared buffer and level are guarded by a :class:`threading.Lock`.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # avoid importing numpy at module import time
    import numpy as np

    from app.models import AudioSettings

log = get_logger("audio")


@dataclass
class AudioDevice:
    """A PortAudio input-capable device as surfaced to the settings UI."""

    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool


class AudioError(RuntimeError):
    """Raised when audio capture cannot start, run or enumerate devices.

    The message is intended to be user-facing and should include a remediation
    hint (e.g. how to install PortAudio) where one applies.
    """


def _import_sounddevice() -> Any:
    """Import :mod:`sounddevice` lazily, translating absence into AudioError.

    Returns:
        The imported ``sounddevice`` module.

    Raises:
        AudioError: If ``sounddevice`` (or its PortAudio backend) is missing.
    """

    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except OSError as exc:
        # sounddevice raises OSError when the PortAudio shared library itself
        # cannot be located/loaded even though the Python package is present.
        raise AudioError(
            "The PortAudio library could not be loaded. Install it with "
            "`sudo apt install libportaudio2` (Debian/Ubuntu), `brew install "
            "portaudio` (macOS), or reinstall the sounddevice wheel on Windows. "
            f"Underlying error: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - ImportError and friends
        raise AudioError(
            "The 'sounddevice' package is required for microphone capture but "
            "is not available. Install it with `pip install sounddevice` (it "
            "bundles PortAudio on Windows/macOS; on Linux also `sudo apt "
            f"install libportaudio2`). Underlying error: {exc}"
        ) from exc
    return sd


def list_audio_devices() -> list[AudioDevice]:
    """Enumerate input-capable audio devices.

    Returns:
        A list of :class:`AudioDevice`, one per device that exposes at least one
        input channel, in PortAudio index order. The system default input device
        (if any) has ``is_default`` set to ``True``.

    Raises:
        AudioError: If ``sounddevice``/PortAudio is unavailable, or the device
            list cannot be queried.
    """

    sd = _import_sounddevice()

    try:
        raw_devices = sd.query_devices()
    except Exception as exc:
        raise AudioError(
            "Failed to query audio devices from PortAudio. Ensure an audio "
            "backend is running (e.g. PipeWire/PulseAudio on Linux) and that "
            f"your user can access it. Underlying error: {exc}"
        ) from exc

    # sd.default.device is a (input_index, output_index) pair; -1 means "no
    # default". Guard defensively in case the attribute is missing/malformed.
    default_input = -1
    try:
        default = sd.default.device
        if isinstance(default, (list, tuple)) and len(default) >= 1:
            default_input = int(default[0])
        elif isinstance(default, int):
            default_input = int(default)
    except Exception:  # pragma: no cover - depends on backend state
        default_input = -1

    devices: list[AudioDevice] = []
    for index, info in enumerate(raw_devices):
        max_in = int(info.get("max_input_channels", 0) or 0)
        if max_in <= 0:
            continue
        devices.append(
            AudioDevice(
                index=index,
                name=str(info.get("name", f"device {index}")),
                max_input_channels=max_in,
                default_samplerate=float(info.get("default_samplerate", 0.0) or 0.0),
                is_default=(index == default_input),
            )
        )

    log.debug("Enumerated %d input-capable audio device(s)", len(devices))
    return devices


class AudioEngine:
    """Callback-driven microphone capture into a bounded float32 buffer.

    Lifecycle: construct cheaply, :meth:`start` to open the stream, :meth:`stop`
    to close it and retrieve the captured audio, and :meth:`close` to release
    the stream if it is still open. :meth:`record_for` is a blocking helper for
    the CLI ``record-once`` command.
    """

    def __init__(self, settings: AudioSettings) -> None:
        """Initialise the engine (no device is opened until :meth:`start`).

        Args:
            settings: Audio capture configuration. ``sample_rate``, ``channels``,
                ``block_size``, ``max_seconds`` and ``device_index`` are honoured.
        """

        self._settings = settings
        self._lock = threading.Lock()
        self._stream: Any = None
        self._recording = False
        # Captured audio blocks (each a 1-D mono float32 ndarray).
        self._blocks: list[np.ndarray] = []
        # Total captured frames currently retained (for the bounded cap).
        self._frames = 0
        # Last-block RMS in 0..1, updated by the callback for the UI meter.
        self._level = 0.0
        # Count of callbacks reporting overflow/underflow, for diagnostics.
        self._xrun_count = 0

        rate = int(self._settings.sample_rate) or 16000
        max_seconds = float(self._settings.max_seconds)
        if max_seconds <= 0:
            # A non-positive cap would mean "keep nothing"; treat as unbounded-ish
            # but still finite to preserve the memory guarantee.
            max_seconds = 300.0
        self._max_frames = int(math.ceil(max_seconds * rate))


    @property
    def is_recording(self) -> bool:
        """Whether a capture stream is currently open and running."""

        with self._lock:
            return self._recording

    @property
    def level(self) -> float:
        """Root-mean-square level of the most recent block, in ``0..1``.

        Intended to drive a simple UI volume meter. Returns ``0.0`` when no
        audio has been captured yet or when recording is stopped.
        """

        with self._lock:
            return self._level


    def start(self) -> None:
        """Open a callback-driven input stream and begin buffering audio.

        The stream is 16 kHz (or ``settings.sample_rate``) mono ``float32``. Each
        delivered block is copied and appended to the internal bounded buffer;
        once ``settings.max_seconds`` of audio is retained the oldest blocks are
        dropped so memory stays bounded.

        Raises:
            AudioError: If already recording, or if the stream cannot be opened
                (e.g. PortAudio unavailable, invalid device, busy device).
        """

        with self._lock:
            if self._recording:
                raise AudioError(
                    "Audio capture is already running; call stop() before "
                    "starting a new recording."
                )
            # Reset per-recording state up front so a failed open leaves a clean
            # engine.
            self._blocks = []
            self._frames = 0
            self._level = 0.0
            self._xrun_count = 0

        sd = _import_sounddevice()
        import numpy as np

        rate = int(self._settings.sample_rate) or 16000
        channels = int(self._settings.channels) or 1
        block_size = int(self._settings.block_size) or 0
        device = self._settings.device_index

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            """PortAudio callback: copy the block, down-mix, update level.

            Runs on the dedicated PortAudio thread. Must be fast and must not
            raise (an exception here would abort the stream), so all work is
            wrapped defensively.
            """

            try:
                if status:
                    # Overflows/underflows are expected under load; count them
                    # for diagnostics rather than spamming the log.
                    self._xrun_count += 1

                # sounddevice reuses `indata`; copy before retaining it.
                block = np.array(indata, dtype=np.float32, copy=True)
                if block.ndim > 1:
                    # Down-mix to mono if the device forced >1 channel.
                    if block.shape[1] > 1:
                        block = block.mean(axis=1)
                    else:
                        block = block.reshape(-1)
                else:
                    block = block.reshape(-1)

                if block.size:
                    rms = float(np.sqrt(np.mean(np.square(block))))
                    if not math.isfinite(rms):
                        rms = 0.0
                    level = max(0.0, min(1.0, rms))
                else:
                    level = 0.0

                with self._lock:
                    self._level = level
                    self._blocks.append(block)
                    self._frames += int(block.shape[0])
                    self._drop_overflow_locked()
            except Exception as exc:  # pragma: no cover - defensive
                # Never propagate out of the callback (would abort the stream).
                log.error("Audio callback error: %s", exc)

        stream_kwargs: dict[str, Any] = {
            "samplerate": rate,
            "channels": channels,
            "dtype": "float32",
            "callback": _callback,
        }
        if block_size > 0:
            stream_kwargs["blocksize"] = block_size
        if device is not None:
            stream_kwargs["device"] = device

        stream = None
        try:
            stream = sd.InputStream(**stream_kwargs)
            stream.start()
        except Exception as exc:
            # The InputStream constructor opens the PortAudio device; if start()
            # then fails we must close it or the device handle leaks and stays
            # exclusively held, breaking later start() attempts.
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            hint = (
                "Could not open the microphone input stream. Check that a "
                "microphone is connected and not exclusively held by another "
                "application, and that the configured device index "
                f"({device!r}) is valid (see `talkpaste list-audio-devices`)."
            )
            raise AudioError(f"{hint} Underlying error: {exc}") from exc

        with self._lock:
            self._stream = stream
            self._recording = True

        log.info(
            "Recording started (rate=%d Hz, channels=%d, block=%d, device=%s, cap=%.0fs)",
            rate,
            channels,
            block_size,
            device if device is not None else "default",
            self._max_frames / float(rate) if rate else 0.0,
        )

    def stop(self) -> np.ndarray:
        """Stop the stream and return the captured mono float32 waveform.

        Returns:
            The concatenated capture as a 1-D ``float32`` numpy array in
            ``[-1, 1]`` at ``settings.sample_rate``. An empty array is returned
            if nothing was captured (or the engine was not recording).

        Raises:
            AudioError: If numpy is unavailable while assembling the result.
        """

        import numpy as np

        with self._lock:
            stream = self._stream
            was_recording = self._recording
            self._stream = None
            self._recording = False

        if stream is not None:
            try:
                stream.stop()
            except Exception as exc:  # pragma: no cover - backend dependent
                log.warning("Error stopping audio stream: %s", exc)
            try:
                stream.close()
            except Exception as exc:  # pragma: no cover - backend dependent
                log.warning("Error closing audio stream: %s", exc)

        with self._lock:
            blocks = self._blocks
            self._blocks = []
            frames = self._frames
            self._frames = 0
            xruns = self._xrun_count
            self._level = 0.0

        if not blocks:
            if was_recording:
                log.info("Recording stopped: no audio captured")
            return np.zeros(0, dtype=np.float32)

        try:
            audio = np.concatenate(blocks).astype(np.float32, copy=False)
        except Exception as exc:  # pragma: no cover - defensive
            raise AudioError(f"Failed to assemble captured audio: {exc}") from exc

        rate = int(self._settings.sample_rate) or 16000
        duration = audio.shape[0] / float(rate) if rate else 0.0
        log.info(
            "Recording stopped: %d frames (%.2fs)%s",
            frames,
            duration,
            f", {xruns} buffer over/underrun(s)" if xruns else "",
        )
        return audio

    def record_for(self, seconds: float) -> np.ndarray:
        """Record for a fixed duration and return the captured audio.

        Blocking convenience used by the CLI ``record-once`` command: it starts
        the stream, sleeps for ``seconds`` (polling so it can be interrupted),
        then stops and returns the result.

        Args:
            seconds: How long to record, in seconds. Values ``<= 0`` capture
                nothing and return an empty array.

        Returns:
            The captured mono float32 waveform (see :meth:`stop`).

        Raises:
            AudioError: If the stream cannot be opened.
        """

        import numpy as np

        if seconds <= 0:
            log.debug("record_for called with non-positive duration; returning empty")
            return np.zeros(0, dtype=np.float32)

        self.start()
        try:
            deadline = time.monotonic() + float(seconds)
            # Poll in short slices so a KeyboardInterrupt is honoured promptly
            # and we never oversleep the requested window by much.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        finally:
            audio = self.stop()
        return audio

    def close(self) -> None:
        """Release the stream if still open. Safe to call multiple times."""

        with self._lock:
            stream = self._stream
            self._stream = None
            self._recording = False

        if stream is not None:
            try:
                stream.stop()
            except Exception as exc:  # pragma: no cover - backend dependent
                log.debug("Error stopping stream during close: %s", exc)
            try:
                stream.close()
            except Exception as exc:  # pragma: no cover - backend dependent
                log.debug("Error closing stream during close: %s", exc)
            log.debug("Audio stream closed")


    def _drop_overflow_locked(self) -> None:
        """Drop the oldest blocks so retained frames stay within the cap.

        Must be called while holding ``self._lock``. Whole leading blocks are
        discarded first; the block that straddles the boundary is trimmed so the
        retained audio is exactly ``self._max_frames`` frames at most.
        """

        if self._max_frames <= 0:
            return
        while self._frames > self._max_frames and self._blocks:
            excess = self._frames - self._max_frames
            head = self._blocks[0]
            head_len = int(head.shape[0])
            if head_len <= excess:
                self._blocks.pop(0)
                self._frames -= head_len
            else:
                self._blocks[0] = head[excess:]
                self._frames -= excess
