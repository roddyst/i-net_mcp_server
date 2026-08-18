from __future__ import annotations

from html.parser import HTMLParser

from inet_helpdesk_mcp.normalize import (
    html_to_text,
    normalize_mapping,
    normalize_record,
    to_iso,
    truncate,
)


def test_display_twins_are_folded_into_the_value() -> None:
    result = normalize_mapping({"statusid": 400, "statusid_display": "Geschlossen"})

    assert result == {"statusid": {"value": 400, "display": "Geschlossen"}}


def test_a_display_key_without_its_twin_survives() -> None:
    result = normalize_mapping({"orphan_display": "no owner"})

    assert result == {"orphan_display": "no owner"}


def test_timestamps_get_an_iso_twin() -> None:
    result = normalize_mapping({"closeddate": 1601294709690})

    assert result["closeddate"] == 1601294709690
    assert result["closeddate_iso"] == "2020-09-28T12:05:09Z"


def test_counters_are_left_alone() -> None:
    # sumtime and reastepbunid are plain numbers, not milliseconds.
    result = normalize_mapping({"sumtime": 0, "reastepbunid": 1, "priorityid": 60})

    assert result == {"sumtime": 0, "reastepbunid": 1, "priorityid": 60}


def test_booleans_are_not_mistaken_for_timestamps() -> None:
    assert normalize_mapping({"autoescalated": False}) == {"autoescalated": False}


def test_nested_maps_are_normalised_too() -> None:
    result = normalize_mapping(
        {"reastepprocessingtime": {"start": 1601011754753, "end": 1601011754753}}
    )

    assert result["reastepprocessingtime"]["start_iso"] == "2020-09-25T05:29:14Z"


def test_normalize_record_touches_only_fields_and_attributes() -> None:
    record = normalize_record(
        {
            "id": 1,
            "lastModified": 1601294709690,
            "fields": {"subject": "Hi"},
            "attributes": {"statusid": 400, "statusid_display": "Geschlossen"},
        }
    )

    assert record["id"] == 1
    assert record["lastModified_iso"] == "2020-09-28T12:05:09Z"
    assert record["attributes"]["statusid"]["display"] == "Geschlossen"


def test_to_iso_is_utc() -> None:
    assert to_iso(0) == "1970-01-01T00:00:00Z"


def test_html_becomes_readable_text() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Der Drucker&nbsp;streikt.</p>"
        "<script>alert(1)</script>"
        "<ul><li>Fehler E5</li><li>seit heute</li></ul>"
        "</body></html>"
    )

    text = html_to_text(html)

    assert "alert(1)" not in text
    assert "color:red" not in text
    assert text == "Der Drucker streikt.\n\n- Fehler E5\n- seit heute"


def test_inline_elements_keep_their_word_boundaries() -> None:
    assert html_to_text("<span>zwei</span> <b>Wörter</b>") == "zwei Wörter"


def test_the_extractor_never_shadows_the_parsers_own_state() -> None:
    """HTMLParser keeps state on the instance and the names differ per Python
    version: 3.13 has a `_pending` of its own, and overwriting it broke close()."""
    from inet_helpdesk_mcp.normalize import _TextExtractor

    reserved = set(vars(HTMLParser())) | set(dir(HTMLParser))
    ours = {name for name in vars(_TextExtractor()) if name.startswith("_text_")}

    assert ours, "the extractor is expected to keep its state under _text_*"
    assert not ours & reserved


def test_html_to_text_handles_empty_input() -> None:
    assert html_to_text("") == ""


def test_truncate_marks_the_gap() -> None:
    result = truncate("abcdefghij", 4)

    assert result.startswith("abcd")
    assert "truncated, 4 of 10 characters" in result


def test_truncate_leaves_short_text_and_zero_limit_alone() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abc", 0) == "abc"
