"""Make Web-API responses readable for a language model.

The i-net Web-API answers in the shape its own UI needs: identifiers instead of
labels (with the label next to it under a ``_display`` key), timestamps as
milliseconds since the epoch and editing step texts as raw HTML. All three cost
the agent either tokens or accuracy, so the tools reshape them - unless the
caller asks for ``raw`` output or the server runs with ``--no-normalize``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping

#: Suffix of the sibling key that carries the label of a value.
DISPLAY_SUFFIX = "_display"

#: Suffix appended to a timestamp key for its ISO-8601 twin.
ISO_SUFFIX = "_iso"

#: Only keys ending in one of these are treated as timestamps, so that plain
#: counters such as ``sumtime`` or ``reastepbunid`` keep their value.
TIMESTAMP_KEY_SUFFIXES = ("date", "changed", "modified", "time", "start", "end")

#: Milliseconds between 2001-09-09 and 2096-10-02. Values outside are much more
#: likely a counter than a timestamp.
MIN_TIMESTAMP_MS = 1_000_000_000_000
MAX_TIMESTAMP_MS = 4_000_000_000_000

#: Tags that end a line, and how many newlines they are worth: a paragraph is
#: separated by a blank line, a list item by a single break.
_BLOCK_BREAKS = {
    "address": 2, "article": 2, "aside": 2, "blockquote": 2, "div": 2,
    "footer": 2, "h1": 2, "h2": 2, "h3": 2, "h4": 2, "h5": 2, "h6": 2,
    "header": 2, "hr": 2, "ol": 2, "p": 2, "pre": 2, "section": 2,
    "table": 2, "ul": 2,
    "br": 1, "li": 1, "td": 1, "th": 1, "tr": 1,
}
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title"})


def is_timestamp(key: str, value: Any) -> bool:
    """True if *value* looks like a millisecond timestamp stored under *key*."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if not MIN_TIMESTAMP_MS <= value <= MAX_TIMESTAMP_MS:
        return False
    return key.lower().endswith(TIMESTAMP_KEY_SUFFIXES)


def to_iso(milliseconds: int) -> str:
    """A millisecond timestamp as UTC ISO-8601, e.g. ``2020-09-25T06:49:14Z``."""
    moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``*_display`` twins and add ``*_iso`` twins, recursively.

    ``{"statusid": 400, "statusid_display": "closed"}`` becomes
    ``{"statusid": {"value": 400, "display": "closed"}}``; a ``closeddate`` of
    ``1601294709690`` keeps its value and gains ``closeddate_iso``. Keys without
    a twin and values of any other type are passed through unchanged.
    """
    displays = {
        key[: -len(DISPLAY_SUFFIX)]: value
        for key, value in mapping.items()
        if key.endswith(DISPLAY_SUFFIX) and key[: -len(DISPLAY_SUFFIX)] in mapping
    }

    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if key.endswith(DISPLAY_SUFFIX) and key[: -len(DISPLAY_SUFFIX)] in mapping:
            continue  # folded into the entry it describes
        normalized = _normalize_value(value)
        if key in displays:
            result[key] = {"value": normalized, "display": displays[key]}
        else:
            result[key] = normalized
        if is_timestamp(key, value):
            result[key + ISO_SUFFIX] = to_iso(value)
    return result


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return normalize_mapping(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def normalize_record(record: Any) -> Any:
    """Normalise the ``fields`` and ``attributes`` maps of a ticket or step.

    Everything else in the record stays untouched, apart from timestamps on the
    top level (``lastModified``), which get their ISO twin as well.
    """
    if not isinstance(record, Mapping):
        return record
    result: dict[str, Any] = {}
    for key, value in record.items():
        if key in ("fields", "attributes") and isinstance(value, Mapping):
            result[key] = normalize_mapping(value)
        else:
            result[key] = value
            if is_timestamp(key, value):
                result[key + ISO_SUFFIX] = to_iso(value)
    return result


class _TextExtractor(HTMLParser):
    """Collects the readable text of an HTML fragment.

    Every attribute is prefixed with ``_text_``: HTMLParser keeps its own state
    on the instance, and which names that are differs between Python versions -
    3.13 has a ``_pending`` of its own, which a plainer name would silently
    overwrite until ``close()`` fails.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._text_parts: list[str] = []
        self._text_skip = 0
        #: Line breaks owed to the text that follows. Collected instead of
        #: written so that </p><ul> does not pile up three empty lines.
        self._text_breaks = 0

    def _text_break(self, tag: str) -> None:
        if self._text_parts:  # never start the text with empty lines
            self._text_breaks = max(self._text_breaks, _BLOCK_BREAKS[tag])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._text_skip += 1
        elif tag in _BLOCK_BREAKS:
            self._text_break(tag)
            if tag == "li":
                self._text_flush()
                self._text_parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._text_skip = max(0, self._text_skip - 1)
        elif tag in _BLOCK_BREAKS:
            self._text_break(tag)

    def _text_flush(self) -> None:
        if self._text_breaks:
            self._text_parts.append("\n" * self._text_breaks)
            self._text_breaks = 0

    def handle_data(self, data: str) -> None:
        if self._text_skip:
            return
        if not data.strip():
            # Whitespace between inline elements still separates two words;
            # whitespace around a block element is swallowed by its break.
            if self._text_parts and not self._text_breaks:
                self._text_parts.append(" ")
            return
        self._text_flush()
        self._text_parts.append(data)


def html_to_text(html: str) -> str:
    """The readable text of an HTML editing step.

    Scripts and styles are dropped, block elements become line breaks and runs
    of whitespace are collapsed - what remains is what a supporter would read.
    """
    if not html:
        return ""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    text = "".join(extractor._text_parts)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text: str, max_chars: int | None) -> str:
    """Cut *text* to *max_chars* and say so, so the model sees the gap."""
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n… [truncated, {max_chars} of {len(text)} characters]"
    )
