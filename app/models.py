"""Typed data models and the organised settings tree for TalkPaste.

Everything user-configurable lives in the :class:`Settings` dataclass tree.
The models are plain :mod:`dataclasses` (no third-party dependency) with
explicit ``to_dict`` / ``from_dict`` helpers so the JSON on disk stays stable
and forward-compatible: unknown keys are ignored on load and missing keys fall
back to defaults.

These types are imported widely, so this module must stay dependency-free
(standard library only) and side-effect free.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Enumerations


class AppState(str, Enum):
    """Lifecycle states surfaced by the tray icon and status popup."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class ModelProfile(str, Enum):
    """Speed/accuracy trade-off presets.

    Each profile maps to a concrete model name via :meth:`ASRSettings.resolved_model`.
    """

    FAST = "fast"          # tiny.en
    BALANCED = "balanced"  # base.en  (default)
    ACCURATE = "accurate"  # small.en
    CUSTOM = "custom"      # use ASRSettings.model_name verbatim


class ASRBackendKind(str, Enum):
    """Which speech-to-text engine to use."""

    FASTER_WHISPER = "faster_whisper"  # default
    WHISPER_CPP = "whisper_cpp"        # optional low-RAM mode


class LanguageMode(str, Enum):
    """English-only vs. multilingual behaviour.

    ``ENGLISH`` pins the English-specific model variants (``*.en``) and forces
    ``language='en'``. ``MULTILINGUAL`` uses the multilingual variants and lets
    the model auto-detect (or honour :attr:`ASRSettings.language`).
    """

    ENGLISH = "english"
    MULTILINGUAL = "multilingual"


class HotkeyMode(str, Enum):
    """How dictation is triggered."""

    PUSH_TO_TALK = "push_to_talk"      # hold to record, release to stop
    HANDS_FREE_TOGGLE = "hands_free"   # press once to start, again to stop


class PasteMode(str, Enum):
    """How final text reaches the focused application."""

    PASTE = "paste"          # set clipboard + inject Ctrl/Cmd+V
    COPY_ONLY = "copy_only"  # only set clipboard; user pastes manually


class PlatformKind(str, Enum):
    """Detected desktop platform / session type."""

    WINDOWS = "windows"
    LINUX_X11 = "linux_x11"
    LINUX_WAYLAND = "linux_wayland"
    MACOS = "macos"
    UNKNOWN = "unknown"


# Settings sections


@dataclass
class AudioSettings:
    """Microphone capture configuration.

    The capture format is fixed at 16 kHz / mono / PCM16 to match the Whisper
    family; only the input device is user-selectable.
    """

    #: Portaudio device index, or ``None`` for the system default device.
    device_index: int | None = None
    #: Human-readable device name (informational; index takes precedence).
    device_name: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    #: Callback block size in frames.
    block_size: int = 1600
    #: Hard ceiling on a single utterance to bound memory (ring buffer size).
    max_seconds: float = 300.0
    #: Trailing silence (seconds) that auto-stops hands-free mode; 0 disables.
    silence_timeout: float = 0.0


@dataclass
class VadSettings:
    """Voice-activity-detection parameters passed to the ASR backend.

    For faster-whisper these feed ``vad_filter`` / ``vad_parameters`` (Silero).
    The structure is deliberately backend-neutral so another VAD can be
    swapped in later.
    """

    enabled: bool = True
    #: Speech probability threshold (Silero).
    threshold: float = 0.5
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 500
    #: Padding kept around detected speech, in milliseconds.
    speech_pad_ms: int = 200

    def to_faster_whisper_params(self) -> dict[str, Any]:
        """Return a dict suitable for faster-whisper ``vad_parameters``."""

        return {
            "threshold": self.threshold,
            "min_speech_duration_ms": self.min_speech_duration_ms,
            "min_silence_duration_ms": self.min_silence_duration_ms,
            "speech_pad_ms": self.speech_pad_ms,
        }


@dataclass
class ASRSettings:
    """Speech-to-text backend and model configuration."""

    backend: ASRBackendKind = ASRBackendKind.FASTER_WHISPER
    profile: ModelProfile = ModelProfile.BALANCED
    language_mode: LanguageMode = LanguageMode.ENGLISH
    #: Explicit language code (e.g. ``"en"``). ``None`` = auto-detect
    #: (multilingual only). Ignored when ``language_mode`` is ENGLISH.
    language: str | None = None
    #: Used only when ``profile`` is CUSTOM, or as an override for whisper.cpp
    #: model file paths.
    model_name: str = ""
    #: Optional explicit path to a model file/dir; overrides name resolution.
    model_path: str | None = None
    #: CTranslate2 compute type. ``int8`` keeps CPU RAM/latency low.
    compute_type: str = "int8"
    device: str = "cpu"          # "cpu" | "cuda"; CPU is the default path
    cpu_threads: int = 4
    beam_size: int = 1
    #: Whisper decoding temperature.
    temperature: float = 0.0
    vad: VadSettings = field(default_factory=VadSettings)

    # Profile -> concrete model stem, per the model policy in the blueprint.
    _EN_MODELS = {
        ModelProfile.FAST: "tiny.en",
        ModelProfile.BALANCED: "base.en",
        ModelProfile.ACCURATE: "small.en",
    }
    _MULTI_MODELS = {
        ModelProfile.FAST: "tiny",
        ModelProfile.BALANCED: "base",
        ModelProfile.ACCURATE: "small",
    }

    def resolved_model(self) -> str:
        """Resolve the concrete model name from profile + language mode."""

        if self.profile is ModelProfile.CUSTOM:
            return self.model_name or "base.en"
        table = (
            self._EN_MODELS
            if self.language_mode is LanguageMode.ENGLISH
            else self._MULTI_MODELS
        )
        return table[self.profile]

    def resolved_language(self) -> str | None:
        """Resolve the language code to hand to the backend."""

        if self.language_mode is LanguageMode.ENGLISH:
            return "en"
        return self.language  # None => auto-detect


@dataclass
class RewriteSettings:
    """Optional local LLM rewrite (grammar/punctuation cleanup only)."""

    enabled: bool = False
    #: Path to a GGUF model in the ~0.6B–1.7B range. Required when enabled.
    model_path: str | None = None
    n_ctx: int = 2048
    n_threads: int = 4
    max_tokens: int = 512
    temperature: float = 0.2
    #: Skip rewrite (keep raw transcript) if it exceeds this wall-clock budget.
    timeout_seconds: float = 8.0
    prompt: str = (
        "Fix grammar and punctuation of this dictated text. "
        "Keep meaning and tone. Output only the final text."
    )


@dataclass
class HotkeySettings:
    """Global shortcut configuration.

    Strings use Qt ``QKeySequence`` portable syntax (e.g. ``"Ctrl+Alt+Space"``)
    so they round-trip cleanly through :class:`QKeySequenceEdit`. Defaults
    deliberately avoid F12.
    """

    mode: HotkeyMode = HotkeyMode.PUSH_TO_TALK
    push_to_talk: str = "Ctrl+Alt+Space"
    hands_free_toggle: str = "Ctrl+Alt+Shift+Space"
    cancel: str = "Esc"


@dataclass
class PasteSettings:
    """Clipboard / injection behaviour."""

    mode: PasteMode = PasteMode.PASTE
    #: Restore the user's previous clipboard contents after pasting.
    restore_clipboard: bool = True
    #: Delay before restoring, so the target app has read the clipboard.
    restore_delay_ms: int = 250
    #: Delay between setting the clipboard and injecting the paste keystroke.
    paste_delay_ms: int = 80
    #: Advanced Wayland-only: use external ``ydotool`` for injection. Never a
    #: silent default — the user must opt in.
    allow_ydotool: bool = False


@dataclass
class FormattingSettings:
    """Deterministic text post-processing toggles."""

    remove_fillers: bool = True
    #: Additional filler words on top of the built-in list.
    extra_fillers: list[str] = field(default_factory=list)
    normalize_whitespace: bool = True
    auto_capitalize: bool = True
    #: Prefer British English spellings in the final output.
    british_english: bool = True
    #: Enable spoken punctuation/formatting commands ("new line", "comma"...).
    enable_commands: bool = True
    #: Expand snippets.json entries.
    enable_snippets: bool = True
    #: Apply dictionary.json replacements.
    enable_dictionary: bool = True
    #: Developer-mode helpers (snake_case / camelCase / filenames / CLI flags).
    developer_mode: bool = False
    #: Collapse a trailing period the user did not dictate.
    trim_trailing_space: bool = True


@dataclass
class HistorySettings:
    """Transcript history persistence."""

    enabled: bool = True
    #: Maximum retained entries; older ones are pruned. 0 = unlimited.
    max_entries: int = 500


@dataclass
class GeneralSettings:
    """Miscellaneous top-level toggles."""

    #: Show desktop notifications for state changes / errors.
    notifications: bool = True
    #: Play a subtle sound cue on start/stop of recording.
    sound_cues: bool = False
    #: Launch the tray app on login (best-effort per platform).
    start_on_login: bool = False
    log_level: str = "INFO"


@dataclass
class Settings:
    """Root settings object serialised to ``settings.json``."""

    #: Schema version to allow future migrations.
    version: int = 1
    general: GeneralSettings = field(default_factory=GeneralSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    asr: ASRSettings = field(default_factory=ASRSettings)
    rewrite: RewriteSettings = field(default_factory=RewriteSettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    paste: PasteSettings = field(default_factory=PasteSettings)
    formatting: FormattingSettings = field(default_factory=FormattingSettings)
    history: HistorySettings = field(default_factory=HistorySettings)


    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (enums become their values)."""

        return _to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Settings:
        """Build from a (possibly partial / older) dict, filling defaults."""

        if not data:
            return cls()
        return _from_jsonable(cls, data)


CURRENT_SETTINGS_VERSION = 1


# Generic dataclass <-> JSON helpers


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/enums into JSON-serialisable values."""

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name.startswith("_"):
                continue
            result[f.name] = _to_jsonable(getattr(obj, f.name))
        return result
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _from_jsonable(cls: type, data: dict[str, Any]) -> Any:
    """Reconstruct a dataclass ``cls`` from a plain dict, ignoring extras.

    Missing fields fall back to their dataclass defaults; unknown keys are
    silently dropped so a newer on-disk file can be read by older code and
    vice versa.
    """

    if not dataclasses.is_dataclass(cls):
        return data

    kwargs: dict[str, Any] = {}
    type_hints = {f.name: f.type for f in dataclasses.fields(cls)}
    for f in dataclasses.fields(cls):
        if f.name.startswith("_") or f.name not in data:
            continue
        raw = data[f.name]
        kwargs[f.name] = _coerce(type_hints[f.name], raw)
    return cls(**kwargs)


def _coerce(field_type: Any, value: Any) -> Any:
    """Best-effort coercion of a raw JSON value to the declared field type."""

    # dataclasses.field.type may be a string under ``from __future__ import
    # annotations``; resolve against this module's namespace.
    resolved = _resolve_type(field_type)

    if resolved is None:
        return value

    # Nested dataclass.
    if dataclasses.is_dataclass(resolved) and isinstance(value, dict):
        return _from_jsonable(resolved, value)

    # Enum.
    if isinstance(resolved, type) and issubclass(resolved, Enum):
        try:
            return resolved(value)
        except ValueError:
            # Unknown enum value on disk -> keep default by returning the raw
            # value would break the ctor; instead pick the first member's
            # class default is unavailable here, so return the raw and let the
            # dataclass validate. We fall back to the raw value.
            return value

    return value


_TYPE_NAMESPACE = {
    "AudioSettings": AudioSettings,
    "VadSettings": VadSettings,
    "ASRSettings": ASRSettings,
    "RewriteSettings": RewriteSettings,
    "HotkeySettings": HotkeySettings,
    "PasteSettings": PasteSettings,
    "FormattingSettings": FormattingSettings,
    "HistorySettings": HistorySettings,
    "GeneralSettings": GeneralSettings,
    "Settings": Settings,
    "ASRBackendKind": ASRBackendKind,
    "ModelProfile": ModelProfile,
    "LanguageMode": LanguageMode,
    "HotkeyMode": HotkeyMode,
    "PasteMode": PasteMode,
    "AppState": AppState,
    "PlatformKind": PlatformKind,
}


def _resolve_type(field_type: Any) -> Any:
    """Resolve a possibly-stringified annotation to a concrete type.

    We only care about the top-level dataclass/enum types nested in Settings.
    Anything we do not recognise (``int``, ``str | None``, ``list[str]`` ...)
    returns ``None`` meaning "leave the value as-is".
    """

    if isinstance(field_type, type):
        if dataclasses.is_dataclass(field_type) or issubclass(field_type, Enum):
            return field_type
        return None

    if isinstance(field_type, str):
        # Strip Optional / union noise; look for a known type name inside.
        for name, tp in _TYPE_NAMESPACE.items():
            if name in field_type and (
                dataclasses.is_dataclass(tp) or issubclass(tp, Enum)
            ):
                return tp
        return None

    return None
