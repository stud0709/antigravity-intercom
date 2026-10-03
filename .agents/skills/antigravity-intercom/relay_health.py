"""Bounded NIP-11 discovery and persistent, endpoint-local relay cooldowns.

Only normalized categories and public relay limits are stored. Relay notices
are untrusted and must never be copied into logs, tool results or prompts.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
import urllib.parse
import urllib.request

import runtime_adapter as runtime

MAX_DOCUMENT_BYTES = 128 * 1024
CACHE_SECONDS = 900
MAX_COOLDOWN = 300
_deadlines = {}


class PublishError(RuntimeError):
    def __init__(self, code, *, outcome="not_sent", retry_after=0):
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.retry_after = max(0, math.ceil(retry_after))

    def result(self):
        return {"status": "error", "code": self.code, "publication": self.outcome,
                "retry_after_seconds": self.retry_after, "automatic_retry": False}


def _path():
    return runtime.get_state_dir() / "relay_health.json"


def _key(url):
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parsed._replace(path="" if parsed.path == "/" else parsed.path))


def _load():
    path = _path()
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value) > 128:
        raise RuntimeError("Invalid local relay health state")
    return value


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not let metadata discovery leave the explicitly configured relay.
        if urllib.parse.urlsplit(newurl) != urllib.parse.urlsplit(req.full_url):
            raise ValueError("Relay metadata redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_information(url):
    import nostr_relay as relay
    relay._validate_relay_urls([url])
    parsed = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit(parsed._replace(scheme="https"))
    request = urllib.request.Request(endpoint, headers={"Accept": "application/nostr+json"})
    opener = urllib.request.build_opener(_Redirect())
    with opener.open(request, timeout=3) as response:
        raw = response.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Relay metadata exceeds limit")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Invalid relay metadata")
    limitation = document.get("limitation", {})
    if not isinstance(limitation, dict):
        raise ValueError("Invalid relay limits")
    limits = {}
    for key in ("max_message_length", "max_content_length", "max_subscriptions", "min_pow_difficulty"):
        value = limitation.get(key)
        if type(value) is int and 0 < value <= 2**31 - 1:
            limits[key] = value
    for key in ("auth_required", "payment_required", "restricted_writes"):
        if type(limitation.get(key)) is bool:
            limits[key] = limitation[key]
    return limits


def _discover(url):
    url = _key(url)
    with runtime.registry_lock():
        state = _load()
        cached = state.get(url, {})
        if time.time() - cached.get("checked_at", 0) < CACHE_SECONDS:
            return cached.get("limits", {})
    try:
        limits = fetch_information(url)
        metadata = "available"
    except Exception:
        limits, metadata = {}, "unknown"
    with runtime.registry_lock():
        state = _load()
        if url not in state and len(state) >= 128:
            raise RuntimeError("Relay health capacity reached")
        entry = state.setdefault(url, {})
        entry.update(limits=limits, metadata=metadata, checked_at=time.time())
        runtime.atomic_write_json(_path(), state)
    return limits


async def discover(urls):
    return dict(zip(urls, await asyncio.gather(*(asyncio.to_thread(_discover, url) for url in urls))))


def classify(reason):
    if not isinstance(reason, str):
        return "unknown"
    value = reason[:512].strip().lower()
    prefix = value.partition(":")[0]
    categories = {"rate-limited": "rate_limited", "blocked": "blocked", "restricted": "restricted",
                  "auth-required": "auth_required", "pow": "pow_required", "invalid": "invalid",
                  "duplicate": "duplicate", "mute": "no_listener", "error": "relay_error"}
    if prefix in categories:
        return categories[prefix]
    if any(word in value for word in ("rate-limit", "rate_limit", "rate limit", "quota exceeded")):
        return "rate_limited"
    if "banned" in value:
        return "blocked"
    return "unknown"


def remaining(url, entry):
    until = entry.get("cooldown_until", 0)
    key = (str(_path()), url)
    deadline = _deadlines.get(key)
    if deadline is None or deadline[0] != until:
        if len(_deadlines) >= 1024:
            _deadlines.clear()
        deadline = (until, time.monotonic() + min(MAX_COOLDOWN, max(0, until - time.time())))
        _deadlines[key] = deadline
    return max(0, deadline[1] - time.monotonic())


def observe(url, category, *, accepted=False):
    url = _key(url)
    with runtime.registry_lock():
        state = _load()
        if url not in state and len(state) >= 128:
            return
        entry = state.setdefault(url, {})
        if accepted:
            entry.update(last_category="accepted", strikes=0, cooldown_until=0, last_result_at=time.time())
        else:
            entry.update(last_category=category, last_result_at=time.time())
            if category in ("rate_limited", "blocked", "restricted", "auth_required", "pow_required"):
                strikes = min(6, entry.get("strikes", 0) + 1)
                base = 10 if category == "rate_limited" else 60
                seconds = min(MAX_COOLDOWN, base * 2**(strikes - 1) + random.uniform(0, 1))
                entry.update(strikes=strikes, cooldown_until=time.time() + seconds)
        runtime.atomic_write_json(_path(), state)


def snapshot(urls):
    with runtime.registry_lock():
        state = _load()
        return [{"relay": urllib.parse.urlsplit(url).hostname,
                 "metadata": state.get(_key(url), {}).get("metadata", "unknown"),
                 "limits": state.get(_key(url), {}).get("limits", {}),
                 "last_category": state.get(_key(url), {}).get("last_category"),
                 "retry_after_seconds": math.ceil(remaining(_key(url), state.get(_key(url), {})))} for url in urls]


def eligible(urls, *, content=None, frame_bytes=None):
    with runtime.registry_lock():
        state = _load()
        selected, cooldowns, oversized, access_required = [], [], False, False
        for url in urls:
            entry = state.get(_key(url), {})
            seconds = remaining(_key(url), entry)
            if seconds:
                cooldowns.append(seconds)
                continue
            limits = entry.get("limits", {})
            if limits.get("auth_required") or limits.get("payment_required") or limits.get("min_pow_difficulty", 0):
                access_required = True
                continue
            if ((content is not None and len(content) > limits.get("max_content_length", len(content)))
                    or (frame_bytes is not None and frame_bytes > limits.get("max_message_length", frame_bytes))):
                oversized = True
                continue
            selected.append(url)
        if selected:
            return selected
        if cooldowns:
            raise PublishError("relay_cooldown", retry_after=min(cooldowns))
        if access_required:
            raise PublishError("relay_access_required")
        raise PublishError("message_too_large" if oversized else "relay_unavailable")


def observe_message(url, message):
    """Only protocol warnings reach this method, never EVENT bodies."""
    import nostr_relay as relay
    try:
        relay._validate_relay_urls([url])
        value = message.as_enum()
        # The SDK supplies typed variants; avoid serializing arbitrary EVENT data.
        import nostr_sdk
        if isinstance(value, nostr_sdk.RelayMessageEnum.OK):
            observe(url, classify(value.message), accepted=value.status)
        elif isinstance(value, nostr_sdk.RelayMessageEnum.CLOSED):
            observe(url, classify(value.message))
        elif isinstance(value, nostr_sdk.RelayMessageEnum.NOTICE):
            observe(url, classify(value.message))
    except Exception:
        return
