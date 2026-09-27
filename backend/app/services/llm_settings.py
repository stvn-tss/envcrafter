"""The Claude API key: from the UI (a settings file) or from the environment (.env).

A key saved from the UI takes precedence over ENVCRAFTER_LLM_API_KEY; removing it falls
back to the environment key, if any. A new key is checked against the API before it is
stored or used, so a typo is reported at once instead of on the next AI request.

The settings file holds a credential, like `.env`: it is written atomically, 0600, in a
0700 directory, and read back as untrusted input (strict schema, size-capped). The key
itself never leaves the server again: the API only reports where it comes from and its
last characters.
"""

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from app.models.settings import ApiKey, KeySource, LLMKeyStatus
from app.services.history import HistoryStore
from app.translator.client import Translator

logger = logging.getLogger(__name__)

MAX_SETTINGS_BYTES = 16 * 1024
_KEY_HINT_LENGTH = 4

TranslatorFactory = Callable[[SecretStr], Translator]


class SettingsStorageError(RuntimeError):
    """The settings file cannot be written. The message is safe to show to users."""


class _SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    llm_api_key: ApiKey | None = None


class SettingsStore:
    """The UI settings file. Every method is blocking: call it through asyncio.to_thread()."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load_api_key(self) -> SecretStr | None:
        """The saved key, or None when there is none or the file cannot be trusted."""
        try:
            if self._path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError(f"{self._path.name} exceeds {MAX_SETTINGS_BYTES} bytes")
            data = _SettingsFile.model_validate_json(self._path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # ValidationError is a ValueError. Details (path, reason) stay server-side.
            logger.warning("Ignoring the unreadable settings file", exc_info=True)
            return None
        return SecretStr(data.llm_api_key) if data.llm_api_key else None

    def save_api_key(self, key: SecretStr) -> None:
        try:
            data = _SettingsFile(llm_api_key=key.get_secret_value())
        except ValidationError:
            raise ValueError("malformed API key") from None
        self._write(data)

    def clear_api_key(self) -> None:
        if self._path.exists():
            self._write(_SettingsFile())

    def _write(self, data: _SettingsFile) -> None:
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A temporary file, then an atomic rename: a crash never leaves half a file, and
        # O_EXCL never follows or reuses a file planted in advance.
        temporary = directory / f".{self._path.name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(data.model_dump(), indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class TranslatorTarget(Protocol):
    """What receives the translator in use (the orchestrator)."""

    def set_translator(self, translator: Translator | None) -> None: ...


class LLMSettings:
    def __init__(
        self,
        *,
        store: SettingsStore,
        environment_key: SecretStr | None,
        model: str,
        factory: TranslatorFactory,
        target: TranslatorTarget,
        history: HistoryStore | None = None,
    ) -> None:
        self._store = store
        self._environment_key = environment_key
        self._model = model
        self._factory = factory
        self._target = target
        self._history = history
        self._saved_key: SecretStr | None = None
        # Saving and removing are serialized: the file, the key in memory and the
        # translator in use always describe the same key.
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Read the saved key at startup and install the translator for the active key."""
        self._saved_key = await asyncio.to_thread(self._store.load_api_key)
        self._install()

    def status(self) -> LLMKeyStatus:
        source, key = self._active()
        secret = key.get_secret_value() if key is not None else ""
        return LLMKeyStatus(
            configured=key is not None,
            source=source,
            key_hint=f"…{secret[-_KEY_HINT_LENGTH:]}" if secret else None,
            model=self._model,
        )

    async def save_key(self, key: SecretStr) -> LLMKeyStatus:
        """Check the key with the API, then store and use it. Raises TranslatorError
        (KeyRejectedError when the key or the model is refused); nothing changes then."""
        async with self._lock:
            translator = self._factory(key)
            await translator.verify()
            await self._store_call(self._store.save_api_key, key)
            self._saved_key = key
            self._target.set_translator(translator)
        logger.info("Claude API key saved from the settings")
        status = self.status()
        if self._history is not None:  # the hint only: never the key itself
            message = f"Claude API key saved (ending {status.key_hint})"
            await self._history.audit("settings", message)
        return status

    async def remove_key(self) -> LLMKeyStatus:
        """Forget the saved key; the environment key, if any, takes over again."""
        async with self._lock:
            await self._store_call(self._store.clear_api_key)
            self._saved_key = None
            self._install()
        logger.info("Claude API key removed from the settings")
        if self._history is not None:
            await self._history.audit("settings", "Saved Claude API key removed")
        return self.status()

    @staticmethod
    async def _store_call(function: Callable[..., None], *args: SecretStr) -> None:
        try:
            await asyncio.to_thread(function, *args)
        except OSError:
            # The path and the reason stay in the server logs.
            logger.exception("The settings file cannot be written")
            raise SettingsStorageError(
                "The server cannot write its settings file (ENVCRAFTER_SETTINGS_FILE). "
                "Check that its directory is writable, or use ENVCRAFTER_LLM_API_KEY."
            ) from None

    def _active(self) -> tuple[KeySource | None, SecretStr | None]:
        if self._saved_key is not None:
            return "settings", self._saved_key
        if self._environment_key is not None:
            return "environment", self._environment_key
        return None, None

    def _install(self) -> None:
        _, key = self._active()
        self._target.set_translator(self._factory(key) if key is not None else None)
        if key is None:
            logger.warning(
                "No Claude API key: natural-language requests are disabled until one is "
                "saved in Settings (or ENVCRAFTER_LLM_API_KEY is set)"
            )
