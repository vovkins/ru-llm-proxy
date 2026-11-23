"""Bounded UTF-8 attachment targets for the request guardrail."""

import base64
import binascii
import json
import re
from pathlib import PurePosixPath

from starlette.responses import JSONResponse

from litellm_guardrails.dictionary_policy import (
    DictionaryOutputCollisionError,
    verify_unique_argument_keys,
)
from litellm_guardrails.restoration_mapping import json_atom_spans

MAX_ATTACHMENT_COUNT = 4
MAX_ATTACHMENT_BYTES = 64 * 1024
_MAX_BASE64_LENGTH = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4
_FILE_BLOCK_TYPES = {"file", "input_file", "document"}
_FILE_UPLOAD_PATH = re.compile(r"/(?:[^/]+/)?(?:v1/)?files/?")
_DATA_URL = re.compile(r"data:([a-z0-9.+/-]+)(?:;charset=utf-8)?;base64,", re.IGNORECASE)
_TEXT_EXTENSIONS = {".txt", ".text", ".log", ".md", ".csv", ".tsv", ".env"}
_ATTACHMENT_ERRORS = {
    "attachment_reference_unsupported": (422, "Send the JSON or text file inline. File references are unsupported."),
    "attachment_format_unsupported": (415, "Only inline UTF-8 JSON and text attachments are supported."),
    "attachment_invalid": (422, "The attachment must contain valid UTF-8 text or JSON."),
    "attachment_too_large": (413, "The inline attachment count or size exceeds the allowed limit."),
    "attachment_dlp_unavailable": (503, "The proxy could not complete the required attachment DLP checks."),
}


class AttachmentPolicyError(ValueError):
    """An attachment refusal with no input data in its message."""

    def __init__(self, code: str):
        self.code = code
        self.status, message = _ATTACHMENT_ERRORS[code]
        super().__init__(message)


def _check_size(size: int) -> None:
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentPolicyError("attachment_too_large")


def _reject_json_constant(_value: str):
    raise ValueError("Non-standard JSON constant")


def _validate_json(text: str) -> None:
    json.loads(text, parse_int=str, parse_float=str, parse_constant=_reject_json_constant)
    verify_unique_argument_keys(text)


def _normalize_json_strings(text: str) -> tuple[str, list[str]]:
    """Expose escaped Unicode to the dictionary without reprinting JSON numbers."""
    _, spans = json_atom_spans(text)
    parts = []
    multiline = []
    offset = 0
    for start, end in spans:
        if "\\" not in text[start:end]:
            continue
        value = json.loads(text[start - 1:end + 1])
        if "\n" in value or "\r" in value:
            multiline.append(value)
        parts.append(text[offset:start - 1])
        parts.append(json.dumps(value, ensure_ascii=False))
        offset = end + 1
    parts.append(text[offset:])
    return "".join(parts), multiline


class AttachmentTextTarget(dict):
    """Expose decoded text and commit validated changes back to the same block."""

    def __init__(self, container: dict, wire_field: str, text: str, size: int,
                 encoding_prefix: str | None, is_json: bool):
        self.container = container
        self.wire_field = wire_field
        self.original_wire = container[wire_field]
        self.encoding_prefix = encoding_prefix
        self.byte_count = size
        self.field = "arguments" if is_json else "content"
        self.policy_texts = [text]
        if is_json:
            try:
                _validate_json(text)
                normalized, multiline = _normalize_json_strings(text)
                _check_size(len(normalized.encode("utf-8")))
            except AttachmentPolicyError:
                raise
            except (ValueError, RecursionError):
                raise AttachmentPolicyError("attachment_invalid") from None
            if normalized != text:
                self.policy_texts.append(normalized)
            self.policy_texts.extend(multiline)
            text = normalized
        self.original_text = text
        super().__init__({self.field: text})

    def validate_update(self, text: str) -> None:
        try:
            _check_size(len(text.encode("utf-8")))
            if self.field == "arguments":
                _validate_json(text)
        except (AttachmentPolicyError, DictionaryOutputCollisionError):
            raise
        except (ValueError, RecursionError):
            raise AttachmentPolicyError("attachment_invalid") from None

    def __setitem__(self, key, value):
        if key == self.field:
            if value == self.original_text:
                wire = self.original_wire
            elif self.encoding_prefix is None:
                wire = value
            else:
                encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
                wire = self.encoding_prefix + encoded
            self.container[self.wire_field] = wire
        super().__setitem__(key, value)


def _format_is_json(media_type: str, filename: str) -> bool:
    media_type = media_type.lower()
    path = PurePosixPath(filename.lower())
    json_name = path.suffix == ".json"
    if media_type in {"application/json", "text/json"} or (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
        return True
    if media_type.startswith("text/"):
        return json_name
    if media_type in {"", "application/octet-stream"}:
        if json_name:
            return True
        if path.suffix in _TEXT_EXTENSIONS or path.name == ".env":
            return False
    raise AttachmentPolicyError("attachment_format_unsupported")


def _decode_attachment(
    container: dict, field: str, *, is_base64: bool, name: str = "",
) -> AttachmentTextTarget:
    wire = container.get(field)
    media_type = container.get("media_type") or ""
    filename = container.get("filename") or name
    if not all(isinstance(item, str) for item in (wire, media_type, filename)):
        raise AttachmentPolicyError("attachment_invalid")
    prefix = None
    try:
        if is_base64:
            prefix = ""
            if wire[:5].lower() == "data:":
                match = _DATA_URL.match(wire)
                if match is None:
                    raise AttachmentPolicyError("attachment_format_unsupported")
                prefix = match.group()
                media_type = match.group(1)
            if len(wire) - len(prefix) > _MAX_BASE64_LENGTH:
                raise AttachmentPolicyError("attachment_too_large")
            raw = base64.b64decode(wire[len(prefix):], validate=True)
        else:
            _check_size(len(wire))
            raw = wire.encode("utf-8")
        _check_size(len(raw))
        text = raw.decode("utf-8-sig")
    except (binascii.Error, UnicodeError):
        raise AttachmentPolicyError("attachment_invalid") from None
    if "\0" in text:
        raise AttachmentPolicyError("attachment_invalid")
    is_json = _format_is_json(media_type, filename)
    if not is_json and text.lstrip(" \t\r\n").startswith(("{", "[")):
        try:
            json.loads(text, parse_int=str, parse_float=str, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            pass
        else:
            is_json = True
    return AttachmentTextTarget(container, field, text, len(raw), prefix, is_json)


def _attachment_target(block: dict) -> AttachmentTextTarget:
    block_type = block["type"]
    if block_type == "document":
        source = block.get("source")
        if not isinstance(source, dict):
            raise AttachmentPolicyError("attachment_invalid")
        source_type = source.get("type")
        if source_type in ("file", "url") or any(source.get(key) is not None for key in ("file_id", "url", "file_url")):
            raise AttachmentPolicyError("attachment_reference_unsupported")
        if source_type not in ("text", "base64"):
            raise AttachmentPolicyError("attachment_format_unsupported")
        return _decode_attachment(
            source, "data", is_base64=source_type == "base64", name=block.get("title") or "",
        )
    file = block.get("file") if block_type == "file" else block
    if not isinstance(file, dict):
        raise AttachmentPolicyError("attachment_invalid")
    if any(file.get(key) is not None for key in ("file_id", "file_url", "url")):
        raise AttachmentPolicyError("attachment_reference_unsupported")
    return _decode_attachment(file, "file_data", is_base64=True)


def collect_attachment_targets(data: dict) -> list[AttachmentTextTarget]:
    """Visit content blocks, including native tool results and extra_body."""
    stack = []
    for envelope in (data, data.get("extra_body")):
        if isinstance(envelope, dict):
            for key in ("messages", "input", "instructions", "system"):
                items = envelope.get(key)
                if isinstance(items, list):
                    stack.extend(reversed(items))
    targets = []
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type in _FILE_BLOCK_TYPES:
            if len(targets) >= MAX_ATTACHMENT_COUNT:
                raise AttachmentPolicyError("attachment_too_large")
            targets.append(_attachment_target(item))
            continue
        for key in ("content", "output"):
            children = item.get(key)
            if isinstance(children, list):
                stack.extend(reversed(children))
    return targets


class RejectFileUploadsMiddleware:
    """Reject uninspected Files API uploads before reading or routing the body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and _FILE_UPLOAD_PATH.fullmatch(scope.get("path", ""))
        ):
            response = JSONResponse(
                {"error": {
                    "message": "File uploads are disabled. Send supported JSON or text files inline.",
                    "type": "attachment_upload_disabled",
                    "code": "attachment_upload_disabled",
                }},
                status_code=403,
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
