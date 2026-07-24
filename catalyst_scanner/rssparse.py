"""Minimal RSS 2.0 / Atom 1.0 parser built on the standard library.

Avoids a feedparser dependency and keeps behaviour predictable: it returns
plain dicts with normalised keys no matter which dialect the wire uses.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITY_RE = re.compile(r"&(#?\w+);")

_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "rsquo": "\u2019",
    "lsquo": "\u2018",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
    "mdash": "\u2014",
    "ndash": "\u2013",
    "hellip": "\u2026",
    "#39": "'",
    "#34": '"',
}


def strip_html(text: str) -> str:
    """Turn a description blob into flat, single-spaced text."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in _ENTITIES:
            return _ENTITIES[key]
        if key.startswith("#"):
            try:
                return chr(int(key[1:], 16 if key[1:2].lower() == "x" else 10))
            except ValueError:
                return " "
        return " "

    text = _ENTITY_RE.sub(_sub, text)
    return _WS_RE.sub(" ", text).strip()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(elem: ET.Element, *names: str) -> ET.Element | None:
    wanted = {n.lower() for n in names}
    for child in elem:
        if _localname(child.tag).lower() in wanted:
            return child
    return None


def _text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def parse_date(raw: str) -> float:
    """Parse RFC-822 or ISO-8601 timestamps to a UTC epoch float."""
    raw = (raw or "").strip()
    if not raw:
        return time.time()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = raw.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = (
                datetime.fromisoformat(cleaned)
                if fmt is None
                else datetime.strptime(cleaned, fmt)
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return time.time()


def _entry_link(entry: ET.Element) -> str:
    link = _find(entry, "link")
    if link is not None:
        if link.get("href"):
            return link.get("href", "")
        if _text(link):
            return _text(link)
    for child in entry:
        if _localname(child.tag).lower() == "link" and child.get("href"):
            return child.get("href", "")
    guid = _find(entry, "guid", "id")
    text = _text(guid)
    return text if text.startswith("http") else ""


def parse(xml_bytes: bytes) -> list[dict]:
    """Parse feed bytes into a list of normalised entry dicts.

    Each entry: {title, link, summary, published, raw_id}
    """
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    # RSS nests items under <channel>; Atom puts <entry> at the root.
    containers = [root]
    channel = _find(root, "channel")
    if channel is not None:
        containers.append(channel)

    entries: list[ET.Element] = []
    for container in containers:
        for child in container:
            if _localname(child.tag).lower() in ("item", "entry"):
                entries.append(child)

    out: list[dict] = []
    for entry in entries:
        title = strip_html(_text(_find(entry, "title")))
        if not title:
            continue
        summary = strip_html(_text(_find(entry, "description", "summary", "content")))
        date_el = _find(entry, "pubdate", "published", "updated", "date")
        out.append(
            {
                "title": title,
                "link": _entry_link(entry),
                "summary": summary[:1200],
                "published": parse_date(_text(date_el)),
                "raw_id": _text(_find(entry, "guid", "id")),
            }
        )
    return out
