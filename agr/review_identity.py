"""Match configuration-aware reviews without charging again for legacy slots."""
import re

from .userconfig import DEFAULT_ENDPOINTS, configuration_id


def reviewer_model(key):
    value = key.removeprefix("model:")
    return re.sub(r"#[a-f0-9]{12}$", "", value)


def _healthy(store, run_id, capture_id, key):
    if store.has_derived(run_id, capture_id, "review_errors.json"):
        errors = store.read_derived(run_id, capture_id, "review_errors.json") or []
        if any(e.get("reviewer_key") == key for e in errors):
            return False
    if store.has_derived(run_id, capture_id, "review_attempts.json"):
        attempts = store.read_derived(run_id, capture_id, "review_attempts.json") or []
        latest = next((a for a in reversed(attempts) if a.get("reviewer_key") == key), None)
        if latest and latest.get("outcome") != "ok":
            return False
    return True


def matching_review_key(store, run_id, capture_id, key):
    keys = store.list_reviews(run_id, capture_id)
    if key in keys:
        return key if _healthy(store, run_id, capture_id, key) else None
    if not key.startswith("model:") or not re.search(r"#[a-f0-9]{12}$", key):
        return None
    model = reviewer_model(key)
    legacy = "model:" + model
    if legacy not in keys or not _healthy(store, run_id, capture_id, legacy):
        return None
    # Old telemetry had provider/model but often no endpoint. In that case only
    # recognize the official destination; never reuse it for a custom gateway.
    settings = None
    attempts = store.read_derived(run_id, capture_id, "review_attempts.json") if store.has_derived(run_id, capture_id, "review_attempts.json") else []
    latest = next((a for a in reversed(attempts) if a.get("reviewer_key") == legacy), None)
    path = "review_telemetry/" + latest["attempt_id"] + ".json" if latest else "review_telemetry.json"
    if not store.has_derived(run_id, capture_id, path):
        path = "review_telemetry.json"
    if store.has_derived(run_id, capture_id, path):
        record = store.read_derived(run_id, capture_id, path) or {}
        if latest and record.get("attempt_id") != latest["attempt_id"]:
            record = {}
        config = record.get("review_configuration")
        if isinstance(config, dict) and config.get("model") == model:
            settings = config
        else:
            settings = next((r for r in record.get("provider_rounds", [])
                             if r.get("model") == model and r.get("provider")), None)
    provider = (settings or {}).get("provider") or ("anthropic" if model.startswith("claude-") else "openai")
    if provider not in DEFAULT_ENDPOINTS:
        return None
    settings = {"provider": provider, "model": model,
                "base_url": (settings or {}).get("base_url") or DEFAULT_ENDPOINTS[provider]}
    return legacy if key == legacy + "#" + configuration_id(settings) else None
