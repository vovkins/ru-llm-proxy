"""Placeholder restoration for OpenAI Responses API objects and stream events."""

from __future__ import annotations

import json
from typing import Any, Optional


_MAX_DEPTH = 32
_TEXT_FIELDS = frozenset(
    {
        "arguments",
        "content",
        "input",
        "output",
        "refusal",
        "summary",
        "text",
    }
)
_OPAQUE_FIELDS = frozenset({"encrypted_content"})
_DELTA_EVENTS: dict[str, tuple[str, bool]] = {
    "response.output_text.delta": ("delta", False),
    "response.reasoning_summary_text.delta": ("delta", False),
    "response.reasoning_text.delta": ("delta", False),
    "response.refusal.delta": ("delta", False),
    "response.function_call_arguments.delta": ("delta", True),
    "response.custom_tool_call_input.delta": ("delta", True),
}
_DONE_EVENTS: dict[str, tuple[str, str]] = {
    "response.output_text.done": ("text", "response.output_text.delta"),
    "response.reasoning_summary_text.done": (
        "text",
        "response.reasoning_summary_text.delta",
    ),
    "response.reasoning_text.done": ("text", "response.reasoning_text.delta"),
    "response.refusal.done": ("refusal", "response.refusal.delta"),
    "response.function_call_arguments.done": (
        "arguments",
        "response.function_call_arguments.delta",
    ),
    "response.custom_tool_call_input.done": (
        "input",
        "response.custom_tool_call_input.delta",
    ),
}
_TERMINAL_EVENTS = frozenset(
    {
        "response.completed",
        "response.incomplete",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
    }
)
_EVENT_ID_FIELDS = (
    "item_id",
    "output_index",
    "content_index",
    "summary_index",
    "sequence_number",
)


def _replace(text: str, mapping: dict[str, str]) -> str:
    for placeholder in sorted(mapping, key=len, reverse=True):
        text = text.replace(placeholder, mapping[placeholder])
    return text


def _restore_copy(
    value: Any,
    mapping: dict[str, str],
    *,
    field: Optional[str] = None,
    depth: int = 0,
) -> tuple[Any, int]:
    if depth > _MAX_DEPTH or field in _OPAQUE_FIELDS:
        return value, 0
    if isinstance(value, str):
        if field not in _TEXT_FIELDS:
            return value, 0
        restored = _replace(value, mapping)
        return restored, int(restored != value)
    if isinstance(value, list):
        restored_items = []
        count = 0
        for item in value:
            restored, item_count = _restore_copy(
                item,
                mapping,
                field=field,
                depth=depth + 1,
            )
            restored_items.append(restored)
            count += item_count
        return restored_items, count
    if isinstance(value, dict):
        restored_dict: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            restored, item_count = _restore_copy(
                item,
                mapping,
                field=str(key),
                depth=depth + 1,
            )
            restored_dict[key] = restored
            count += item_count
        return restored_dict, count

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            payload = model_dump(mode="python", exclude_none=False)
        except TypeError:
            payload = model_dump(exclude_none=False)
        if isinstance(payload, dict):
            restored_payload, count = _restore_copy(
                payload,
                mapping,
                field=field,
                depth=depth + 1,
            )
            validator = getattr(type(value), "model_validate", None)
            if callable(validator):
                try:
                    return validator(restored_payload), count
                except Exception:
                    pass
            copier = getattr(value, "model_copy", None)
            if callable(copier):
                return copier(update=restored_payload, deep=True), count
    return value, 0


def is_responses_response(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("object") == "response" and isinstance(value.get("output"), list)
    return (
        getattr(value, "object", None) == "response"
        and isinstance(getattr(value, "output", None), list)
    )


def restore_responses_response(value: Any, mapping: dict[str, str]) -> int:
    """Restore a unary Responses object in place and return changed field count."""
    restored, count = _restore_copy(value, mapping)
    if not count:
        return 0
    if isinstance(value, dict) and isinstance(restored, dict):
        value.clear()
        value.update(restored)
        return count
    fields = getattr(type(value), "model_fields", {})
    for field in fields:
        try:
            setattr(value, field, getattr(restored, field))
        except (AttributeError, TypeError, ValueError):
            continue
    return count


def _event_payload(chunk: Any) -> Optional[dict[str, Any]]:
    if isinstance(chunk, dict):
        return dict(chunk)
    model_dump = getattr(chunk, "model_dump", None)
    if callable(model_dump):
        try:
            payload = model_dump(mode="python", exclude_none=False)
        except TypeError:
            payload = model_dump(exclude_none=False)
        return payload if isinstance(payload, dict) else None
    return None


def is_responses_event(chunk: Any) -> bool:
    payload = _event_payload(chunk)
    return bool(
        payload
        and isinstance(payload.get("type"), str)
        and payload["type"].startswith("response.")
    )


def _restore_event(original: Any, payload: dict[str, Any]) -> Any:
    if isinstance(original, dict):
        return payload
    validator = getattr(type(original), "model_validate", None)
    if callable(validator):
        try:
            return validator(payload)
        except Exception:
            pass
    copier = getattr(original, "model_copy", None)
    if callable(copier):
        return copier(update=payload, deep=True)
    return payload


class _DeltaBuffer:
    def __init__(self, mapping: dict[str, str], *, json_escape: bool = False):
        self._mapping = mapping
        self._placeholders = sorted(mapping, key=len, reverse=True)
        self._pending = ""
        self._json_escape = json_escape

    def feed(self, text: str) -> str:
        combined = self._pending + text
        keep = 0
        for length in range(1, len(combined) + 1):
            suffix = combined[-length:]
            if any(
                suffix != placeholder and placeholder.startswith(suffix)
                for placeholder in self._placeholders
            ):
                keep = length
        if keep:
            emitted, self._pending = combined[:-keep], combined[-keep:]
        else:
            emitted, self._pending = combined, ""
        return self._restore(emitted)

    def flush(self) -> str:
        pending, self._pending = self._pending, ""
        return self._restore(pending)

    def _restore(self, text: str) -> str:
        restored = _replace(text, self._mapping)
        return json.dumps(restored)[1:-1] if self._json_escape else restored


class ResponsesStreamRestorer:
    """Restore placeholders across typed Responses API delta boundaries."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping
        self._streams: dict[tuple[Any, ...], _DeltaBuffer] = {}
        self._metadata: dict[tuple[Any, ...], dict[str, Any]] = {}

    @staticmethod
    def _metadata_for(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            field: payload[field]
            for field in _EVENT_ID_FIELDS
            if field in payload
        }

    @staticmethod
    def _stream_key(event_type: str, payload: dict[str, Any]) -> tuple[Any, ...]:
        return (
            event_type,
            *(payload.get(field) for field in _EVENT_ID_FIELDS[:-1]),
        )

    def feed(self, chunk: Any) -> tuple[list[Any], int]:
        payload = _event_payload(chunk)
        if payload is None or not isinstance(payload.get("type"), str):
            return [chunk], 0
        event_type = payload["type"]

        delta_spec = _DELTA_EVENTS.get(event_type)
        if delta_spec is not None:
            field, json_escape = delta_spec
            value = payload.get(field)
            if not isinstance(value, str):
                return [chunk], 0
            key = self._stream_key(event_type, payload)
            buffer = self._streams.get(key)
            if buffer is None:
                buffer = _DeltaBuffer(self._mapping, json_escape=json_escape)
                self._streams[key] = buffer
                self._metadata[key] = self._metadata_for(payload)
            restored = buffer.feed(value)
            if not restored:
                return [], 0
            return [
                _restore_event(chunk, {**payload, field: restored})
            ], int(restored != value)

        done_spec = _DONE_EVENTS.get(event_type)
        if done_spec is not None:
            _, delta_type = done_spec
            output: list[Any] = []
            changed = 0
            key = self._stream_key(delta_type, payload)
            buffer = self._streams.pop(key, None)
            metadata = self._metadata.pop(key, self._metadata_for(payload))
            if buffer is not None:
                tail = buffer.flush()
                if tail:
                    output.append(
                        json.dumps(
                            {"type": delta_type, **metadata, "delta": tail},
                            ensure_ascii=False,
                        )
                    )
                    changed += 1
            restored_payload, payload_count = _restore_copy(payload, self._mapping)
            output.append(_restore_event(chunk, restored_payload))
            return output, changed + payload_count

        if event_type in _TERMINAL_EVENTS:
            restored_payload, count = _restore_copy(payload, self._mapping)
            return [_restore_event(chunk, restored_payload)], count
        return [chunk], 0

    def flush(self) -> tuple[list[str], int]:
        output: list[str] = []
        for key, buffer in list(self._streams.items()):
            tail = buffer.flush()
            if tail:
                output.append(
                    json.dumps(
                        {
                            "type": str(key[0]),
                            **self._metadata.get(key, {}),
                            "delta": tail,
                        },
                        ensure_ascii=False,
                    )
                )
        self._streams.clear()
        self._metadata.clear()
        return output, len(output)
