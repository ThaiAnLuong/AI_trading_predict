"""Small HTTPS helper. Errors are returned as text that never includes a URL or key."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .market.base import DataUnavailable

USER_AGENT = "PaperTrader/1.0 (local paper-trading experiment)"
MAX_BYTES = 5_000_000


def fetch(url: str, params: dict | None = None, timeout: float = 20) -> bytes:
    if not url.lower().startswith("https://"):
        raise DataUnavailable("Only https:// sources are allowed.")
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read(20_000)).get("message", "")
        except (ValueError, AttributeError, OSError):
            pass
        text = f"HTTP {exc.code}" + (f": {detail}" if detail else "")
        secret = (params or {}).get("apikey") or (params or {}).get("token")
        raise DataUnavailable(text.replace(secret, "***") if secret else text) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DataUnavailable(f"Network error: {type(exc).__name__}") from None
    if len(body) > MAX_BYTES:
        raise DataUnavailable("Response was too large.")
    return body


def fetch_json(url: str, params: dict | None = None, timeout: float = 20):
    try:
        return json.loads(fetch(url, params, timeout))
    except ValueError:
        raise DataUnavailable("The response was not valid JSON.") from None
