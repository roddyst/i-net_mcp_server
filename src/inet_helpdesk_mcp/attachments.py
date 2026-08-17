"""Attachment handling for ``create_ticket`` and ``apply_ticket_action``."""

from __future__ import annotations

import base64
import binascii
import os
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .client import AttachmentUpload
from .errors import HelpdeskError

#: 25 MB - large enough for normal HelpDesk attachments, small enough to keep a
#: single tool call from exhausting memory.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

AttachmentType = Literal["Attachment", "EmbeddedImage", "Signature", "Unknown"]


class Attachment(BaseModel):
    """One file to upload with a ticket or an editing step.

    Provide the content either inline as base64 (``content_base64``) or, when
    the server runs locally next to the agent (stdio transport), as a ``path``
    on the server's file system.
    """

    name: str | None = Field(
        default=None,
        description="File name shown in the HelpDesk. Defaults to the file name of 'path'.",
    )
    content_base64: str | None = Field(
        default=None, description="Base64 encoded file content."
    )
    path: str | None = Field(
        default=None,
        description=(
            "Path to a local file, read by the MCP server. Only available for the "
            "stdio transport (see INET_ALLOW_LOCAL_FILES)."
        ),
    )
    attachment_type: AttachmentType = Field(
        default="Attachment",
        description="One of Attachment, EmbeddedImage, Signature, Unknown.",
    )
    last_modified: int = Field(
        default=0, description="Last modification timestamp in milliseconds, 0 if unknown."
    )


def _read_local_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            content = handle.read(MAX_ATTACHMENT_BYTES + 1)
    except OSError as exc:
        raise HelpdeskError(f"Could not read attachment {path!r}: {exc}") from exc
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HelpdeskError(
            f"Attachment {path!r} is larger than the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit."
        )
    return content


def prepare_attachments(
    attachments: Sequence[Attachment] | None, *, allow_local_files: bool
) -> list[AttachmentUpload]:
    """Turn the tool input into ``(description, content)`` pairs for the Web-API."""
    prepared: list[AttachmentUpload] = []
    for index, attachment in enumerate(attachments or []):
        if attachment.content_base64 and attachment.path:
            raise HelpdeskError(
                f"Attachment {index}: provide either 'content_base64' or 'path', not both."
            )

        if attachment.content_base64:
            try:
                content = base64.b64decode(attachment.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HelpdeskError(
                    f"Attachment {index}: 'content_base64' is not valid base64 ({exc})."
                ) from exc
            if len(content) > MAX_ATTACHMENT_BYTES:
                raise HelpdeskError(
                    f"Attachment {index} is larger than the "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit."
                )
            name = attachment.name
        elif attachment.path:
            if not allow_local_files:
                raise HelpdeskError(
                    f"Attachment {index}: reading local files is disabled for this "
                    "transport. Send the file inline as 'content_base64' instead."
                )
            content = _read_local_file(attachment.path)
            name = attachment.name or os.path.basename(attachment.path)
        else:
            raise HelpdeskError(
                f"Attachment {index}: either 'content_base64' or 'path' is required."
            )

        if not name:
            raise HelpdeskError(f"Attachment {index}: 'name' is required for inline content.")

        prepared.append(
            (
                {
                    "name": name,
                    "lastModified": attachment.last_modified,
                    "attachmentType": attachment.attachment_type,
                },
                content,
            )
        )
    return prepared
