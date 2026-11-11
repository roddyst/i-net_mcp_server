"""Attachment handling for ``create_ticket`` and ``apply_ticket_action``."""

from __future__ import annotations

import base64
import binascii
import os
import stat
from typing import Literal, Sequence

from pydantic import BaseModel, Field

from .client import AttachmentUpload
from .errors import HelpdeskError

#: 25 MB - large enough for normal HelpDesk attachments, small enough to keep a
#: single tool call from exhausting memory.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Upper bounds for one tool call, so that a single request cannot make the
#: server hold an arbitrary amount of data in memory.
MAX_ATTACHMENTS = 20
MAX_TOTAL_ATTACHMENT_BYTES = 2 * MAX_ATTACHMENT_BYTES

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
            "stdio transport and only inside the directories the operator released "
            "(see INET_ATTACHMENT_DIR)."
        ),
    )
    attachment_type: AttachmentType = Field(
        default="Attachment",
        description="One of Attachment, EmbeddedImage, Signature, Unknown.",
    )
    last_modified: int = Field(
        default=0, description="Last modification timestamp in milliseconds, 0 if unknown."
    )


def _inside(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:  # different drives on Windows
        return False


def _read_local_file(path: str, allowed_dirs: Sequence[str] = ()) -> bytes:
    """Read an attachment from disk.

    The path comes from the agent. Symbolic links are resolved before the
    check against *allowed_dirs*, so a link inside a released directory cannot
    point out of it, and only regular files are read - no devices, no pipes.
    """
    resolved = os.path.realpath(path)
    if allowed_dirs:
        roots = [os.path.realpath(directory) for directory in allowed_dirs]
        if not any(_inside(resolved, root) for root in roots):
            raise HelpdeskError(
                f"Attachment {path!r} is outside the directories released for "
                f"attachments ({', '.join(allowed_dirs)}). Copy the file there or send "
                "it inline as 'content_base64'."
            )
    try:
        # O_NONBLOCK: opening a pipe must not hang the server while waiting
        # for a writer; the type is then checked on the opened file itself.
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise HelpdeskError(f"Attachment {path!r} is not a regular file.")
            content = handle.read(MAX_ATTACHMENT_BYTES + 1)
    except OSError as exc:
        raise HelpdeskError(f"Could not read attachment {path!r}: {exc}") from exc
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HelpdeskError(
            f"Attachment {path!r} is larger than the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit."
        )
    return content


def prepare_attachments(
    attachments: Sequence[Attachment] | None,
    *,
    allow_local_files: bool,
    allowed_dirs: Sequence[str] = (),
) -> list[AttachmentUpload]:
    """Turn the tool input into ``(description, content)`` pairs for the Web-API.

    With *allowed_dirs* a ``path`` has to lie inside one of these directories.
    """
    if attachments and len(attachments) > MAX_ATTACHMENTS:
        raise HelpdeskError(
            f"At most {MAX_ATTACHMENTS} attachments per call, got {len(attachments)}."
        )
    prepared: list[AttachmentUpload] = []
    total = 0
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
                    f"Attachment {index}: reading local files is disabled on this "
                    "server. Send the file inline as 'content_base64' instead."
                )
            content = _read_local_file(attachment.path, allowed_dirs)
            name = attachment.name or os.path.basename(attachment.path)
        else:
            raise HelpdeskError(
                f"Attachment {index}: either 'content_base64' or 'path' is required."
            )

        if not name:
            raise HelpdeskError(f"Attachment {index}: 'name' is required for inline content.")

        total += len(content)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise HelpdeskError(
                "The attachments of one call are limited to "
                f"{MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB in total."
            )

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
