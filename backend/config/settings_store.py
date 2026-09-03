"""Read and write the user .env configuration file."""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Serializes writers to .env. Without this, two concurrent saves can interleave
# and leave a truncated file behind.
_WRITE_LOCK = threading.Lock()

# A .env entry is `KEY=value`; keys must be shell-safe identifiers.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_LLM_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "ZAI_API_KEY",
    "MOONSHOT_API_KEY",
    "MINIMAX_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "VOLCENGINE_API_KEY",
}


def get_env_path() -> Path:
    """Return the effective .env file path for dev or packaged mode."""
    if getattr(sys, "frozen", False):
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Application Support" / "AIHunter"
        elif system == "Windows":
            import os

            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "AIHunter"
        else:
            base = Path.home() / ".config" / "AIHunter"
        base.mkdir(parents=True, exist_ok=True)
        return base / ".env"
    return _BACKEND_ROOT / ".env"


def read_settings() -> dict[str, str]:
    """Parse the .env file into a KEY -> value mapping."""
    path = get_env_path()
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _serialize(data: dict[str, str]) -> str:
    """Serialize a mapping into .env content, rejecting anything that could
    escape its line.

    Values are written unquoted, so a value containing a newline could append
    arbitrary new settings (e.g. ``x\\nOPENAI_API_KEY=attacker-key``). Keys that
    are not shell-safe identifiers are rejected for the same reason.
    """
    lines: list[str] = []
    for key, value in data.items():
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid .env key: {key!r}")
        text = str(value)
        if any(char in text for char in ("\n", "\r", "\x00")):
            raise ValueError(f"Invalid .env value for {key}: contains a newline or NUL byte")
        lines.append(f"{key}={text}\n")
    return "".join(lines)


def write_settings(data: dict[str, str]) -> None:
    """Overwrite the .env file with the provided mapping (atomically)."""
    path = get_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _serialize(data)

    # Write to a temp file then os.replace, so a crash mid-write can never leave
    # a truncated .env behind (matches utils.storage._write_json).
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with _WRITE_LOCK:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)


def update_settings(updates: dict[str, str]) -> None:
    """Merge updates into the existing .env file."""
    existing = read_settings()
    existing.update(updates)
    write_settings(existing)
    # Force the cached settings singleton to re-read from .env on next use.
    # Without this, a running server keeps stale values until a manual restart.
    try:
        from config.settings import reload_settings

        reload_settings()
    except Exception as exc:  # pragma: no cover - never block a settings save
        # Silent failure here is dangerous: the user believes the new value is
        # live while the process keeps serving the old one.
        logger.warning("[settings_store] settings saved but reload failed; restart required: %s", exc)


def is_configured() -> bool:
    """Return True when at least one LLM key is configured."""
    settings = read_settings()
    return any(settings.get(key, "").strip() for key in _LLM_KEYS)
