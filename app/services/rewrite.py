"""Optional local LLM rewrite pass (grammar/punctuation cleanup only).

TalkPaste can run a small local GGUF model through ``llama_cpp`` to tidy the
grammar and punctuation of a raw transcript while preserving the speaker's
meaning and tone. This is strictly a *best-effort* enhancement: the dependency
is optional, the model is heavyweight, and inference can be slow. Because of
that, :class:`RewriteEngine` is designed so that **transcription never fails or
stalls because of rewrite**:

* ``llama_cpp`` is imported lazily, inside methods, so importing this module
  never requires the dependency to be installed.
* :meth:`RewriteEngine.rewrite` runs inference on a worker thread bounded by
  ``settings.timeout_seconds``. On timeout or any runtime error it logs a
  warning and returns the *original* text unchanged. It never raises for
  runtime issues.
* When the feature is disabled, unavailable, or the input is blank, the input
  is returned verbatim.

Only :meth:`RewriteEngine.load` raises (``RewriteError``) — and only when the
caller has explicitly asked to load a model that cannot be loaded.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:  # avoid importing the heavy optional dep at module import time
    from app.models import RewriteSettings

log = get_logger("rewrite")


class RewriteError(RuntimeError):
    """Raised when a rewrite model is explicitly requested but cannot load.

    Runtime failures during :meth:`RewriteEngine.rewrite` never raise this —
    they fall back to the original text. This is reserved for
    :meth:`RewriteEngine.load`, where the caller has asked for a model that is
    genuinely unavailable (missing dependency, missing/invalid model file).
    """


class RewriteEngine:
    """Wrap a local ``llama_cpp`` model for grammar/punctuation cleanup.

    The engine is constructed cheaply; the model is only loaded on demand via
    :meth:`load` (or implicitly on the first :meth:`rewrite`). All heavy work is
    guarded so a slow or broken rewrite can never break dictation.
    """

    def __init__(self, settings: RewriteSettings) -> None:
        """Store settings; do not import or load anything yet.

        Args:
            settings: The :class:`~app.models.RewriteSettings` controlling
                whether rewrite is enabled, the model path, and inference
                budgets (context size, threads, tokens, temperature, timeout).
        """

        self.settings = settings
        self._model: Any = None
        self._lock = threading.Lock()
        # Serialises native inference: llama_cpp's Llama is not safe to drive
        # from two threads at once (e.g. an abandoned timed-out worker plus a
        # new call). A new rewrite skips rather than racing a busy model.
        self._infer_lock = threading.Lock()


    def is_available(self) -> bool:
        """Return whether a rewrite model *could* be loaded right now.

        This is a cheap probe: it is ``True`` iff the ``llama_cpp`` package is
        importable **and** ``settings.model_path`` is set and points at an
        existing file. It does not load the model.

        Returns:
            ``True`` if loading is expected to succeed, ``False`` otherwise.
        """

        model_path = self.settings.model_path
        if not model_path:
            log.debug("Rewrite unavailable: no model_path configured")
            return False
        if not Path(model_path).is_file():
            log.debug("Rewrite unavailable: model_path does not exist: %s", model_path)
            return False
        try:
            import importlib.util

            if importlib.util.find_spec("llama_cpp") is None:
                log.debug("Rewrite unavailable: llama_cpp is not installed")
                return False
        except (ImportError, ValueError):  # pragma: no cover - defensive
            log.debug("Rewrite unavailable: could not probe for llama_cpp")
            return False
        return True

    def load(self) -> None:
        """Load the GGUF model into memory. Idempotent.

        Lazily imports ``llama_cpp`` and constructs a ``Llama`` instance from
        ``settings.model_path`` with the configured context size and thread
        count. Safe to call repeatedly — subsequent calls are no-ops once the
        model is loaded.

        Raises:
            RewriteError: If ``llama_cpp`` is not installed, no valid
                ``model_path`` is configured, or the model fails to construct.
                The message includes remediation guidance.
        """

        with self._lock:
            if self._model is not None:
                return

            model_path = self.settings.model_path
            if not model_path:
                raise RewriteError(
                    "No rewrite model configured. Set rewrite.model_path to a "
                    "GGUF model file (a ~0.6B-1.7B instruct model works well)."
                )
            if not Path(model_path).is_file():
                raise RewriteError(
                    f"Rewrite model file not found: {model_path!r}. "
                    "Download a small GGUF instruct model and point "
                    "rewrite.model_path at it."
                )

            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise RewriteError(
                    "The 'llama-cpp-python' package is required for the rewrite "
                    "feature but is not installed. Install it with "
                    "`pip install llama-cpp-python`, or disable rewrite in "
                    "settings."
                ) from exc

            log.info(
                "Loading rewrite model %s (n_ctx=%d, n_threads=%d)",
                model_path,
                self.settings.n_ctx,
                self.settings.n_threads,
            )
            try:
                self._model = Llama(
                    model_path=model_path,
                    n_ctx=self.settings.n_ctx,
                    n_threads=self.settings.n_threads,
                    verbose=False,
                )
            except Exception as exc:  # noqa: BLE001 - surface as a typed error
                self._model = None
                raise RewriteError(
                    f"Failed to load rewrite model {model_path!r}: {exc}. "
                    "Verify the file is a valid GGUF model and that "
                    "'llama-cpp-python' is installed correctly."
                ) from exc
            log.info("Rewrite model loaded")

    def is_loaded(self) -> bool:
        """Return whether the model is currently loaded in memory."""

        return self._model is not None

    def unload(self) -> None:
        """Release the model and free its memory. Safe to call when unloaded."""

        with self._lock:
            if self._model is None:
                return
            log.info("Unloading rewrite model")
            try:
                close = getattr(self._model, "close", None)
                if callable(close):
                    close()
            except Exception as exc:  # noqa: BLE001 - never fail on cleanup
                log.debug("Ignoring error while closing rewrite model: %s", exc)
            finally:
                self._model = None


    def rewrite(self, text: str) -> str:
        """Clean up grammar/punctuation of ``text``, preserving meaning.

        Inference runs on a worker thread bounded by
        ``settings.timeout_seconds``. If the model is disabled, unavailable,
        not loaded (and cannot be loaded), the input is blank, or anything goes
        wrong (timeout or error), the **original text is returned unchanged**
        and a warning is logged. This method never raises for runtime issues.

        Args:
            text: The raw transcript to tidy.

        Returns:
            The cleaned text, or the original ``text`` verbatim on any
            skip/timeout/failure.
        """

        if not text or not text.strip():
            return text

        if not self.settings.enabled:
            log.debug("Rewrite skipped: feature disabled")
            return text

        # A non-positive timeout cannot bound inference, and the contract
        # forbids blocking indefinitely — so treat it as "skip rewrite".
        timeout = self.settings.timeout_seconds
        if not timeout or timeout <= 0:
            log.debug("Rewrite skipped: timeout_seconds=%s is not a positive bound", timeout)
            return text

        # Availability is a cheap probe; when no model is loaded yet and none
        # can be loaded, skip without raising.
        if self._model is None and not self.is_available():
            log.debug("Rewrite skipped: no usable model available")
            return text

        # If a previous (possibly timed-out) inference is still running, do not
        # race the non-thread-safe model — fall back to the original text.
        if self._infer_lock.locked():
            log.warning("Rewrite skipped: a previous inference is still running")
            return text

        result: dict[str, str] = {}

        def _run() -> None:
            # Both load AND inference run here so a slow first-load is bounded by
            # the same timeout and never stalls the caller indefinitely.
            with self._infer_lock:
                try:
                    if self._model is None:
                        self.load()
                    result["text"] = self._infer(text)
                except Exception as exc:  # noqa: BLE001 - contained; falls back
                    log.warning("Rewrite failed (%s); using original text", exc)

        worker = threading.Thread(target=_run, name="rewrite-infer", daemon=True)
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            # The daemon thread keeps running but we stop waiting for it; the
            # transcript proceeds with the original, un-rewritten text.
            log.warning("Rewrite timed out after %.1fs; using original text", timeout)
            return text

        cleaned = result.get("text")
        if not cleaned or not cleaned.strip():
            # Inference failed or produced nothing usable.
            return text

        # Stripping model wrappers can itself collapse output to empty (e.g. a
        # bare `Output:` label or lone quotes); never return empty — that would
        # silently discard the user's transcript.
        final = _strip_model_wrapping(cleaned)
        return final if final.strip() else text


    def _infer(self, text: str) -> str:
        """Run a single blocking inference on the loaded model.

        Prefers ``create_chat_completion`` with a system+user message; falls
        back to a plain-prompt completion if the chat API is unavailable.

        Args:
            text: The transcript to rewrite.

        Returns:
            The model's raw output text (not yet stripped of wrapping).
        """

        model = self._model
        if model is None:  # pragma: no cover - guarded by caller
            return text

        system_prompt = self.settings.prompt
        temperature = self.settings.temperature
        max_tokens = self.settings.max_tokens

        # Preferred path: chat completion.
        chat = getattr(model, "create_chat_completion", None)
        if callable(chat):
            try:
                response = chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content = _extract_chat_content(response)
                if content is not None:
                    return content
                log.debug("Chat completion returned no content; falling back")
            except Exception as exc:  # noqa: BLE001 - fall back to plain prompt
                log.debug("Chat completion failed (%s); falling back to prompt", exc)

        # Fallback path: plain prompt completion.
        prompt = f"{system_prompt}\n\n{text}\n"
        response = model(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = _extract_completion_content(response)
        return content if content is not None else text


# Response parsing / output sanitising


def _extract_chat_content(response: Any) -> str | None:
    """Extract the assistant message text from a chat-completion response.

    Args:
        response: The object returned by ``create_chat_completion``.

    Returns:
        The message content, or ``None`` if it cannot be found.
    """

    try:
        choice = response["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


def _extract_completion_content(response: Any) -> str | None:
    """Extract the generated text from a plain completion response.

    Args:
        response: The object returned by calling the ``Llama`` instance.

    Returns:
        The completion text, or ``None`` if it cannot be found.
    """

    try:
        choice = response["choices"][0]
        text = choice.get("text")
        if isinstance(text, str):
            return text
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


def _strip_model_wrapping(text: str) -> str:
    """Strip common wrapping the model may add around the final text.

    Small instruct models frequently prepend a label ("Output:", "Corrected
    text:") or wrap the whole answer in matching quotes. This removes such
    decoration so the returned string is just the cleaned sentence.

    Args:
        text: Raw model output.

    Returns:
        The de-wrapped, trimmed text.
    """

    cleaned = text.strip()

    # Remove a leading label such as "Output:" / "Corrected text:" on the first
    # line, but only when it is a short prefix (avoid eating real content).
    prefixes = (
        "output:",
        "corrected text:",
        "corrected:",
        "result:",
        "final text:",
        "final:",
        "answer:",
        "here is the corrected text:",
        "here's the corrected text:",
        "rewritten text:",
        "rewrite:",
    )
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip()
            break

    # Strip matching surrounding quotes, once, if they wrap the whole string.
    cleaned = _strip_matching_quotes(cleaned)

    return cleaned.strip()


def _strip_matching_quotes(text: str) -> str:
    """Remove one layer of matching surrounding quotes, if present.

    Args:
        text: The candidate text.

    Returns:
        ``text`` without a single enclosing pair of straight or curly quotes.
    """

    if len(text) < 2:
        return text
    pairs = {
        '"': '"',
        "'": "'",
        "“": "”",  # curly double quotes
        "‘": "’",  # curly single quotes
        "`": "`",
    }
    first = text[0]
    last = text[-1]
    if first in pairs and pairs[first] == last:
        inner = text[1:-1]
        # Only strip if there is no unbalanced closing quote inside that would
        # indicate the quotes are meaningful content rather than wrapping.
        if first not in inner:
            return inner.strip()
    return text
