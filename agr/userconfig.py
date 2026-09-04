"""User-level reviewer configuration — the persistence behind ``agr config``.

One small JSON file (default ``~/.agr/config.json``, overridable via
``AGR_CONFIG``) records the model-reviewer settings a user configured once, so
``agr review`` and ``agr review --all`` work without re-typing flags.

Precedence everywhere: CLI flag > environment variable > this file > the
provider's built-in default. ``base_url`` additionally falls back to
``OPENAI_BASE_URL`` inside the OpenAI adapter itself.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

CONFIG_ENV_VAR = "AGR_CONFIG"
CONFIG_FILENAME = "config.json"


def config_path() -> Path:
    """Where the settings live (``$AGR_CONFIG`` wins, else ``~/.agr/config.json``)."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".agr" / CONFIG_FILENAME


def load_config() -> dict:
    """Read the saved settings; missing or corrupt file -> ``{}`` (never raises)."""
    path = config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(provider: Optional[str] = None, model: Optional[str] = None,
                base_url: Optional[str] = None) -> dict:
    """Merge the given fields into the saved settings and persist them."""
    cfg = load_config()
    if provider is not None:
        cfg["provider"] = provider
    if model is not None:
        cfg["model"] = model
    if base_url is not None:
        cfg["base_url"] = base_url
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    return cfg


def clear_config() -> bool:
    """Delete the saved settings. Returns whether a file was removed."""
    path = config_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def resolve_review_settings(provider: Optional[str] = None,
                            model: Optional[str] = None,
                            base_url: Optional[str] = None) -> dict:
    """Effective reviewer settings with the full precedence chain applied.

    ``provider``: flag > saved config > ``"anthropic"``.
    ``model``:    flag > ``$AGR_REVIEW_MODEL`` > saved config > ``None``
    (the provider class then applies its own default).
    ``base_url``: flag > saved config > ``None`` (the OpenAI adapter falls
    back to ``$OPENAI_BASE_URL`` itself, which is provider-specific).
    """
    cfg = load_config()
    return {
        "provider": provider or cfg.get("provider") or "anthropic",
        "model": model or os.environ.get("AGR_REVIEW_MODEL") or cfg.get("model") or None,
        "base_url": base_url or cfg.get("base_url") or None,
    }
