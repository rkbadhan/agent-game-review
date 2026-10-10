"""Shared, validated reviewer settings for the CLI and local app."""
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .storage_io import atomic_json, file_lock

CONFIG_ENV_VAR = "AGR_CONFIG"
CONFIG_FILENAME = "config.json"
# The model is always the user's choice; there is no built-in model ID.
DEFAULT_ENDPOINTS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
DEFAULT_LIMITS = {"cost_budget_usd": 0.15, "time_budget_s": 90.0, "request_timeout_s": 600.0}
ENV_SETTINGS = {"provider": "AGR_REVIEW_PROVIDER", "model": "AGR_REVIEW_MODEL",
                "cost_budget_usd": "AGR_REVIEW_COST_BUDGET_USD",
                "time_budget_s": "AGR_REVIEW_TIME_BUDGET_S",
                "request_timeout_s": "AGR_REVIEW_REQUEST_TIMEOUT_S"}
FIELDS = {"provider", "model", "base_url", *DEFAULT_LIMITS}


class ConfigError(ValueError):
    """Invalid settings must never silently select a different paid destination."""


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR) or Path.home() / ".agr" / CONFIG_FILENAME)


def validate_settings(values: dict) -> dict:
    result = {}
    for field in FIELDS & values.keys():
        value = values[field]
        if value is None:
            continue
        if field in DEFAULT_LIMITS:
            if isinstance(value, bool):
                raise ConfigError(f"{field} must be a finite non-negative number.")
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"{field} must be a finite non-negative number.") from None
            if not math.isfinite(value) or value < 0 or (field == "request_timeout_s" and value == 0):
                raise ConfigError(f"{field} must be finite and " + ("positive." if field == "request_timeout_s" else "non-negative."))
        else:
            if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise ConfigError(f"Enter a valid {field}.")
            value = value.strip()
            if field == "provider" and value not in DEFAULT_ENDPOINTS:
                raise ConfigError("Choose anthropic or openai.")
            if field == "base_url" and value:
                try:
                    parsed = urlsplit(value)
                    parsed.port
                except ValueError:
                    raise ConfigError("Enter an HTTP(S) endpoint URL.") from None
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ConfigError("Use an HTTP(S) endpoint without credentials, query parameters, or a fragment.")
                value = value.rstrip("/")
        result[field] = value
    return result


def load_config() -> dict:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ConfigError(f"Cannot read reviewer settings at {path}. Repair the file or run agr config --clear.") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Reviewer settings at {path} must be a JSON object.")
    validated = validate_settings(data)
    if isinstance(data.get("updated_at"), str):
        validated["updated_at"] = data["updated_at"]
    return validated


def merged_config(cfg, changes):
    cfg = dict(cfg)
    if changes.get("provider") and changes["provider"] != cfg.get("provider", "anthropic"):
        cfg.pop("model", None)
        cfg.pop("base_url", None)
    for field, value in changes.items():
        if value == "":
            cfg.pop(field, None)
        else:
            cfg[field] = value
    return cfg


def save_config(provider: Optional[str] = None, model: Optional[str] = None,
                base_url: Optional[str] = None, **limits) -> dict:
    changes = validate_settings({"provider": provider, "model": model, "base_url": base_url, **limits})
    path = config_path()
    with file_lock(path.with_suffix(".lock")):
        cfg = load_config()
        cfg = merged_config(cfg, changes)
        # Only known, non-secret settings are persisted.
        cfg = {k: v for k, v in cfg.items() if k in FIELDS}
        cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_json(path, cfg)
    return cfg


def clear_config() -> bool:
    path = config_path()
    with file_lock(path.with_suffix(".lock")):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def effective_review_config(*, saved_config=None, **overrides) -> dict:
    cfg = load_config() if saved_config is None else saved_config
    overrides = validate_settings(overrides)
    origins = {}
    def choose(field, default, env_name=None, saved=None):
        if field in overrides:
            origins[field] = "flag"
            return overrides[field] if overrides[field] != "" else default
        env_name = env_name or ENV_SETTINGS.get(field)
        raw = os.environ.get(env_name, "") if env_name else ""
        if raw.strip():
            origins[field] = env_name
            return validate_settings({field: raw})[field]
        if saved is not None and saved.get(field) not in (None, ""):
            origins[field] = "saved"
            return saved[field]
        origins[field] = "default" if default != "" else "unset"
        return default
    provider = choose("provider", "anthropic", saved=cfg)
    # A one-off provider override cannot inherit a different provider's model/URL.
    matching = cfg if cfg.get("provider", "anthropic") == provider else {}
    result = {"provider": provider}
    result["model"] = choose("model", "", saved=matching)
    result["base_url"] = choose("base_url", DEFAULT_ENDPOINTS[provider],
                                env_name=provider.upper() + "_BASE_URL", saved=matching)
    for field, default in DEFAULT_LIMITS.items():
        result[field] = choose(field, default, saved=cfg)
    result["origins"] = origins
    result["configuration_id"] = configuration_id(result)
    return result


NO_MODEL_MESSAGE = ("No reviewer model is set. Choose one in Settings > AI review, run "
                    "'agr config --model MODEL_ID' ('agr config --list-models' shows the "
                    "endpoint's models), or set AGR_REVIEW_MODEL.")


def require_model(settings) -> None:
    if not settings.get("model"):
        raise ConfigError(NO_MODEL_MESSAGE)


def resolve_review_settings(provider=None, model=None, base_url=None) -> dict:
    settings = effective_review_config(provider=provider, model=model, base_url=base_url)
    return {k: settings[k] for k in ("provider", "model", "base_url")}


def configuration_id(settings) -> str:
    """Identity of the paid destination/model, never credentials or mutable limits."""
    provider = settings["provider"]
    endpoint = (settings.get("base_url") or os.environ.get(provider.upper() + "_BASE_URL")
                or DEFAULT_ENDPOINTS.get(provider, "")).rstrip("/")
    identity = {"provider": provider, "model": settings.get("model") or "",
                "base_url": endpoint}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def reviewer_key(settings) -> str:
    return f"model:{settings['model']}#{configuration_id(settings)}"
