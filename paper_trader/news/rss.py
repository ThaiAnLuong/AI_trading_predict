"""RSS 2.0, RSS 1.0 (RDF) and Atom feeds. Add only feeds whose terms allow this use."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..http import fetch
from ..market.base import DataUnavailable
from ..timeutil import to_iso
from .base import NewsItem, NewsSource

SUMMARY_CHARS = 400
_TAGS = re.compile(r"<[^>]+>")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(value: str | None) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", value or "")).split())


def _parse_date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return to_iso(parsed)


def parse_feed(body: bytes, source_name: str) -> list[NewsItem]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise DataUnavailable("The feed is not valid XML.") from None
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                if child.get("rel", "alternate") == "alternate":
                    fields["link"] = child.get("href")
            elif name not in fields:
                fields[name] = child.text or ""
        title = _text(fields.get("title"))
        link = fields.get("link", "").strip()
        published = None
        for key in ("pubdate", "date", "published", "updated"):
            published = published or _parse_date(fields.get(key, ""))
        # An item without a title, link and time cannot be used as evidence.
        if not (title and link and published):
            continue
        summary = _text(fields.get("description") or fields.get("summary") or fields.get("content"))
        items.append(NewsItem(title, source_name, published, link, summary[:SUMMARY_CHARS]))
    return items


class RssSource(NewsSource):
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url

    def fetch(self, tickers: list[str]) -> list[NewsItem]:
        return parse_feed(fetch(self.url), self.name)
