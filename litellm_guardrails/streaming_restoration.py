"""Request-local restoration of streaming text and JSON string values."""

import copy
import json
import re
from typing import Any, Hashable

import litellm
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
)


def get_field(container: Any, field: str) -> Any:
    return (
        container.get(field)
        if isinstance(container, dict)
        else getattr(container, field, None)
    )


def set_field(container: Any, field: str, value: Any) -> None:
    if isinstance(container, dict):
        container[field] = value
    else:
        setattr(container, field, value)


class _StreamingPlaceholderReplacer:
    """Keep only suffixes which could become a known replacement key."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = {key: value for key, value in mapping.items() if key}
        self._placeholders = sorted(self._mapping, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(key) for key in self._placeholders))
            if self._placeholders
            else None
        )
        self._max_prefix = max((len(key) - 1 for key in self._placeholders), default=0)
        self._pending: dict[Hashable, str] = {}

    def push(self, key: Hashable, text: str) -> str:
        combined = self._pending.get(key, "") + text
        candidates = []
        for length in range(1, min(len(combined), self._max_prefix) + 1):
            suffix = combined[-length:]
            if any(
                value != suffix and value.startswith(suffix)
                for value in self._placeholders
            ):
                candidates.append(len(combined) - length)
        if candidates and self._pattern is not None:
            # A suffix inside an earlier complete match is not a new key prefix.
            for match in self._pattern.finditer(combined):
                candidates = [
                    index
                    for index in candidates
                    if not match.start() < index < match.end()
                ]
        keep = len(combined) - min(candidates) if candidates else 0
        if keep:
            self._pending[key] = combined[-keep:]
            return self._replace(combined[:-keep])
        self._pending.pop(key, None)
        return self._replace(combined)

    def flush(self, key: Hashable) -> str:
        return self._replace(self._pending.pop(key, ""))

    def _replace(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda match: self._mapping[match.group()], text)


def restore_text(text: str, mapping: dict[str, str]) -> str:
    return _StreamingPlaceholderReplacer(mapping)._replace(text)


class _StreamingJSONRestorer:
    """Decode string-value fragments, restore them, and JSON-escape the result."""

    def __init__(self, mapping: dict[str, str]):
        self._replacer = _StreamingPlaceholderReplacer(mapping)
        self._containers: list[list[Any]] = []
        self._in_string = False
        self._is_key = False
        self._escape = ""
        self._surrogate = ""

    @staticmethod
    def _encode(text: str) -> str:
        return json.dumps(text, ensure_ascii=True)[1:-1]

    def _decode_unit(self, value: str, decoded: list[str]) -> None:
        if self._surrogate:
            high = ord(self._surrogate)
            self._surrogate = ""
            if len(value) == 1 and 0xDC00 <= ord(value) <= 0xDFFF:
                decoded.append(
                    chr(0x10000 + ((high - 0xD800) << 10) + ord(value) - 0xDC00)
                )
                return
            decoded.append(chr(high))
        if len(value) == 1 and 0xD800 <= ord(value) <= 0xDBFF:
            self._surrogate = value
        else:
            decoded.append(value)

    def push(self, text: str) -> str:
        output: list[str] = []
        decoded: list[str] = []

        def emit_decoded() -> None:
            if decoded:
                output.append(
                    self._encode(self._replacer.push("value", "".join(decoded)))
                )
                decoded.clear()

        for char in text:
            if not self._in_string:
                output.append(char)
                if char == '"':
                    self._in_string = True
                    self._is_key = bool(
                        self._containers and self._containers[-1] == ["{", True]
                    )
                elif char in "{[":
                    self._containers.append([char, char == "{"])
                elif char in "}]":
                    if self._containers:
                        self._containers.pop()
                elif char == ":" and self._containers:
                    self._containers[-1][1] = False
                elif (
                    char == "," and self._containers and self._containers[-1][0] == "{"
                ):
                    self._containers[-1][1] = True
                continue

            if self._is_key:
                output.append(char)
                if self._escape:
                    self._escape = ""
                elif char == "\\":
                    self._escape = "\\"
                elif char == '"':
                    self._in_string = False
                continue

            if self._escape:
                self._escape += char
                if len(self._escape) == 2 and char == "u":
                    continue
                if self._escape.startswith("\\u") and len(self._escape) < 6:
                    continue
                # The standard JSON decoder owns escape validation, including Unicode.
                try:
                    value = json.loads('"' + self._escape + '"')
                except json.JSONDecodeError:
                    raise ValueError(
                        "Invalid JSON escape in streamed tool arguments"
                    ) from None
                self._escape = ""
                self._decode_unit(value, decoded)
            elif char == "\\":
                self._escape = "\\"
            elif char == '"':
                if self._surrogate:
                    decoded.append(self._surrogate)
                    self._surrogate = ""
                emit_decoded()
                output.append(self._encode(self._replacer.flush("value")))
                output.append(char)
                self._in_string = False
            else:
                self._decode_unit(char, decoded)
        emit_decoded()
        return "".join(output)

    def flush(self) -> str:
        output = ""
        if self._surrogate:
            output += self._encode(self._replacer.push("value", self._surrogate))
            self._surrogate = ""
        output += self._encode(self._replacer.flush("value"))
        if not self._is_key:
            output += self._escape
        self._escape = ""
        return output


def restore_arguments(text: str, mapping: dict[str, str]) -> str:
    restorer = _StreamingJSONRestorer(mapping)
    return restorer.push(text) + restorer.flush()


class StreamingResponseRestorer:
    """Maintain independent fields while preserving the upstream event protocol."""

    _TEXT_EVENTS = {
        "response.output_text": "text",
        "response.refusal": "refusal",
        "response.reasoning_text": "text",
        "response.reasoning_summary_text": "text",
    }
    _TERMINAL_EVENTS = {
        "response.completed",
        "response.incomplete",
        "response.failed",
        "error",
    }

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self._text = _StreamingPlaceholderReplacer(mapping)
        self._json: dict[tuple, _StreamingJSONRestorer] = {}
        self._templates: dict[tuple, Any] = {}
        self._sequence_offset = 0
        self._last_sequence = -1
        self.restored_fields = 0

    def _push(self, key: tuple, text: str, *, arguments=False) -> str:
        if arguments:
            if key not in self._json:
                self._json[key] = _StreamingJSONRestorer(self.mapping)
            result = self._json[key].push(text)
        else:
            result = self._text.push(key, text)
        if result != text:
            self.restored_fields += 1
        return result

    def _flush(self, key: tuple) -> str:
        arguments = self._json.pop(key, None)
        result = arguments.flush() if arguments is not None else self._text.flush(key)
        if result:
            self.restored_fields += 1
        return result

    def _remember(self, key: tuple, event: Any) -> None:
        template = copy.deepcopy(event)
        if key[0] == "chat":
            set_field(template, "choices", [])
            if get_field(template, "usage") is not None:
                set_field(template, "usage", None)
        else:
            set_field(template, "delta", "")
        self._templates[key] = template

    @staticmethod
    def _chat_target(delta: Any, key: tuple) -> tuple[Any, str]:
        if key[2] == "text":
            return delta, key[3]
        if key[2] == "legacy":
            function = get_field(delta, "function_call")
            if function is None:
                function = {"arguments": ""}
                set_field(delta, "function_call", function)
            return function, "arguments"
        calls = get_field(delta, "tool_calls")
        if calls is None:
            calls = []
            set_field(delta, "tool_calls", calls)
        for call in calls:
            if get_field(call, "index") == key[3]:
                return get_field(call, "function"), "arguments"
        call = litellm.Delta(
            tool_calls=[{"index": key[3], "function": {"arguments": ""}}]
        ).tool_calls[0]
        calls.append(call)
        return get_field(call, "function"), "arguments"

    def _chat(self, event: Any) -> list[Any]:
        for choice in get_field(event, "choices"):
            index = get_field(choice, "index") or 0
            delta = get_field(choice, "delta")
            if delta is None:
                delta = {} if isinstance(choice, dict) else litellm.Delta()
                set_field(choice, "delta", delta)
            targets = []
            for field in ("content", "reasoning_content"):
                if isinstance(get_field(delta, field), str):
                    targets.append(
                        (("chat", index, "text", field), delta, field, False)
                    )
            for call in get_field(delta, "tool_calls") or []:
                function = get_field(call, "function")
                if isinstance(get_field(function, "arguments"), str):
                    targets.append(
                        (
                            ("chat", index, "tool", get_field(call, "index") or 0),
                            function,
                            "arguments",
                            True,
                        )
                    )
            function = get_field(delta, "function_call")
            if isinstance(get_field(function, "arguments"), str):
                targets.append((("chat", index, "legacy"), function, "arguments", True))
            for key, target, field, arguments in targets:
                self._remember(key, event)
                set_field(
                    target,
                    field,
                    self._push(key, get_field(target, field), arguments=arguments),
                )
            if get_field(choice, "finish_reason") is not None:
                for key in list(self._templates):
                    if key[:2] != ("chat", index):
                        continue
                    text = self._flush(key)
                    self._templates.pop(key)
                    if text:
                        target, field = self._chat_target(delta, key)
                        set_field(
                            target, field, (get_field(target, field) or "") + text
                        )
        return [event]

    @staticmethod
    def _response_key(event: Any, kind: str) -> tuple:
        part = get_field(event, "summary_index")
        if part is None:
            part = get_field(event, "content_index") or 0
        return ("responses", get_field(event, "output_index") or 0, kind, part)

    def _restore_field(self, target: Any, field: str, *, arguments=False) -> None:
        value = get_field(target, field)
        if not isinstance(value, str):
            return
        result = (
            restore_arguments(value, self.mapping)
            if arguments
            else self._text._replace(value)
        )
        if result != value:
            self.restored_fields += 1
            set_field(target, field, result)

    def _seed(
        self, event: Any, target: Any, field: str, key: tuple, *, item_id=None
    ) -> None:
        value = get_field(target, field)
        if not isinstance(value, str) or not value:
            return
        arguments = key[2] == "response.function_call_arguments"
        if key in self._templates:
            self._restore_field(target, field, arguments=arguments)
            return
        data = {
            "type": key[2] + ".delta",
            "delta": "",
            "item_id": item_id or get_field(event, "item_id"),
            "output_index": key[1],
            "sequence_number": get_field(event, "sequence_number") or 0,
        }
        event_types = {
            "response.function_call_arguments": ResponseFunctionCallArgumentsDeltaEvent,
            "response.output_text": ResponseTextDeltaEvent,
            "response.refusal": ResponseRefusalDeltaEvent,
            "response.reasoning_summary_text": ResponseReasoningSummaryTextDeltaEvent,
        }
        if not arguments:
            data[
                (
                    "summary_index"
                    if key[2] == "response.reasoning_summary_text"
                    else "content_index"
                )
            ] = key[3]
        if key[2] == "response.output_text":
            data["logprobs"] = []
        self._templates[key] = (
            data if isinstance(event, dict) else event_types[key[2]](**data)
        )
        set_field(target, field, self._push(key, value, arguments=arguments))

    def _restore_part(
        self, part: Any, *, event=None, part_index=0, summary=False, item_id=None
    ) -> None:
        for field in ("text", "refusal", "stdout", "stderr"):
            if event is not None and field in ("text", "refusal"):
                kind = (
                    "response.reasoning_summary_text"
                    if summary
                    else (
                        "response.refusal"
                        if field == "refusal"
                        else "response.output_text"
                    )
                )
                key = (
                    "responses",
                    get_field(event, "output_index") or 0,
                    kind,
                    part_index,
                )
                self._seed(event, part, field, key, item_id=item_id)
            else:
                self._restore_field(part, field)

    def _restore_item(self, item: Any, *, event=None) -> None:
        for field in ("content", "summary"):
            for index, part in enumerate(get_field(item, field) or []):
                self._restore_part(
                    part,
                    event=event,
                    part_index=index,
                    summary=field == "summary",
                    item_id=get_field(item, "id"),
                )
        if event is not None:
            key = (
                "responses",
                get_field(event, "output_index") or 0,
                "response.function_call_arguments",
                0,
            )
            self._seed(event, item, "arguments", key, item_id=get_field(item, "id"))
        else:
            self._restore_field(item, "arguments", arguments=True)
        self._restore_field(item, "input")
        output = get_field(item, "output")
        if isinstance(output, str):
            self._restore_field(item, "output")
        elif isinstance(output, list):
            for part in output:
                self._restore_part(part)

    def _stamp(self, event: Any, *, inserted=False) -> Any:
        sequence = get_field(event, "sequence_number")
        if inserted:
            sequence = self._last_sequence + 1
            self._sequence_offset += 1
        elif isinstance(sequence, int):
            sequence += self._sequence_offset
        if isinstance(sequence, int):
            set_field(event, "sequence_number", sequence)
            self._last_sequence = sequence
        return event

    def _flush_responses(self, predicate) -> list[Any]:
        output = []
        for key in list(self._templates):
            if key[0] != "responses" or not predicate(key):
                continue
            template = self._templates.pop(key)
            text = self._flush(key)
            if text:
                set_field(template, "delta", text)
                output.append(self._stamp(template, inserted=True))
        return output

    def _responses(self, event: Any, kind: str) -> list[Any]:
        base, _, suffix = kind.rpartition(".")
        is_arguments = base == "response.function_call_arguments"
        if suffix == "delta" and (base in self._TEXT_EVENTS or is_arguments):
            text = get_field(event, "delta")
            if isinstance(text, str):
                key = self._response_key(event, base)
                self._remember(key, event)
                set_field(event, "delta", self._push(key, text, arguments=is_arguments))
            return [self._stamp(event)]

        output = []
        if suffix == "done" and (base in self._TEXT_EVENTS or is_arguments):
            key = self._response_key(event, base)
            output.extend(self._flush_responses(lambda candidate: candidate == key))
            self._restore_field(
                event,
                "arguments" if is_arguments else self._TEXT_EVENTS[base],
                arguments=is_arguments,
            )
        elif kind == "response.output_item.done":
            index = get_field(event, "output_index") or 0
            output.extend(self._flush_responses(lambda key: key[1] == index))
            self._restore_item(get_field(event, "item"))
        elif kind == "response.output_item.added":
            self._restore_item(get_field(event, "item"), event=event)
        elif kind in (
            "response.content_part.done",
            "response.reasoning_summary_part.done",
        ):
            index = get_field(event, "output_index") or 0
            part = get_field(event, "content_index")
            if part is None:
                part = get_field(event, "summary_index") or 0
            output.extend(
                self._flush_responses(
                    lambda key: key[1] == index
                    and key[3] == part
                    and key[2] != "response.function_call_arguments"
                )
            )
            self._restore_part(get_field(event, "part"))
        elif kind in (
            "response.content_part.added",
            "response.reasoning_summary_part.added",
        ):
            summary = kind == "response.reasoning_summary_part.added"
            part_index = (
                get_field(event, "summary_index" if summary else "content_index") or 0
            )
            self._restore_part(
                get_field(event, "part"),
                event=event,
                part_index=part_index,
                summary=summary,
            )
        elif kind in self._TERMINAL_EVENTS:
            output.extend(self._flush_responses(lambda _key: True))
            for item in get_field(get_field(event, "response"), "output") or []:
                self._restore_item(item)
        output.append(self._stamp(event))
        return output

    def process(self, event: Any) -> list[Any]:
        if isinstance(get_field(event, "choices"), list):
            return self._chat(event)
        kind = get_field(event, "type")
        if isinstance(kind, str):
            return self._responses(event, kind)
        return [event]

    def finish(self) -> list[Any]:
        output = self._flush_responses(lambda _key: True)
        for key in list(self._templates):
            template = self._templates.pop(key)
            text = self._flush(key)
            if not text:
                continue
            delta = litellm.Delta()
            target, field = self._chat_target(delta, key)
            set_field(target, field, text)
            choice = litellm.StreamingChoices(index=key[1], delta=delta)
            set_field(
                template,
                "choices",
                [choice.model_dump() if isinstance(template, dict) else choice],
            )
            output.append(template)
        return output
