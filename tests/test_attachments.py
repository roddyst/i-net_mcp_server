from __future__ import annotations

import base64
from pathlib import Path

import pytest

from inet_helpdesk_mcp.attachments import Attachment, prepare_attachments
from inet_helpdesk_mcp.errors import HelpdeskError


def test_inline_base64_attachment() -> None:
    content = b"hello bytes"
    attachment = Attachment(
        name="note.txt", content_base64=base64.b64encode(content).decode()
    )

    prepared = prepare_attachments([attachment], allow_local_files=False)

    assert prepared == [
        (
            {"name": "note.txt", "lastModified": 0, "attachmentType": "Attachment"},
            content,
        )
    ]


def test_local_file_attachment_defaults_name_to_file_name(tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")

    (description, content), = prepare_attachments(
        [Attachment(path=str(path))], allow_local_files=True
    )

    assert description["name"] == "report.pdf"
    assert content == b"%PDF-1.4"


def test_local_file_attachment_rejected_when_disabled(tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"x")

    with pytest.raises(HelpdeskError, match="disabled for this transport"):
        prepare_attachments([Attachment(path=str(path))], allow_local_files=False)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(HelpdeskError, match="Could not read attachment"):
        prepare_attachments(
            [Attachment(path=str(tmp_path / "nope.txt"))], allow_local_files=True
        )


def test_both_sources_rejected() -> None:
    with pytest.raises(HelpdeskError, match="not both"):
        prepare_attachments(
            [Attachment(name="a", path="/tmp/a", content_base64="QQ==")],
            allow_local_files=True,
        )


def test_no_source_rejected() -> None:
    with pytest.raises(HelpdeskError, match="is required"):
        prepare_attachments([Attachment(name="a")], allow_local_files=True)


def test_invalid_base64_rejected() -> None:
    with pytest.raises(HelpdeskError, match="not valid base64"):
        prepare_attachments(
            [Attachment(name="a", content_base64="not base64!!")], allow_local_files=True
        )


def test_inline_attachment_requires_name() -> None:
    with pytest.raises(HelpdeskError, match="'name' is required"):
        prepare_attachments(
            [Attachment(content_base64=base64.b64encode(b"x").decode())],
            allow_local_files=True,
        )


def test_attachment_type_is_validated() -> None:
    with pytest.raises(ValueError):
        Attachment(name="a", content_base64="QQ==", attachment_type="Something")  # type: ignore[arg-type]


def test_empty_list_is_fine() -> None:
    assert prepare_attachments(None, allow_local_files=True) == []
