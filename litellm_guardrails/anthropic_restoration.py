"""Restore native Anthropic Messages bodies and SSE without changing caching."""

import copy
import json
import re

import httpx

from litellm_guardrails.streaming_restoration import (
    _StreamingJSONRestorer, _StreamingPlaceholderReplacer,
    get_field, set_field, restore_arguments, restore_text,
)
from litellm_guardrails.sse_integrity import IncompleteUpstreamStream, MAX_SSE_EVENT_BYTES

MAX_TOOL_ARGUMENT_BYTES = 8 * 1024 * 1024
_NATIVE_EVENTS = frozenset({
    'message_start', 'content_block_start', 'content_block_delta',
    'content_block_stop', 'message_delta', 'message_stop',
})


class AnthropicToolInputTarget(dict):
    """Mutable JSON-text view that commits masked arguments back as an object.

    The Analyzer already validates full JSON arguments. Keep that path for native
    tool_use.input instead of leaving its nested values outside request analysis.
    No transient string-valued input is exposed to the provider.
    """

    def __init__(self, block):
        self.block = block
        super().__init__(arguments=json.dumps(block['input'], ensure_ascii=False, allow_nan=False))

    def __setitem__(self, key, value):
        if key == 'arguments':
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError('Anthropic tool input must remain a JSON object')
            self.block['input'] = parsed
        super().__setitem__(key, value)


def is_anthropic_message(value):
    return (get_field(value, 'type') == 'message'
            and get_field(value, 'role') == 'assistant'
            and isinstance(get_field(value, 'content'), list))


def message_targets(message):
    """Only user-visible text and tool input; never signed/opaque blocks or IDs."""
    targets = []
    for block in get_field(message, 'content') or []:
        kind = get_field(block, 'type')
        if kind == 'text' and isinstance(get_field(block, 'text'), str):
            targets.append((block, 'text'))
        elif kind in {'tool_use', 'server_tool_use'} and isinstance(get_field(block, 'input'), dict):
            targets.append((block, 'input'))
    return targets


def restore_tool_input(value, mapping):
    # JSON is already decoded at this boundary. Reuse the typed argument
    # restorer so escaped strings and numeric provenance have the same semantics
    # as Chat Completions/Responses. Keys remain protocol/schema keys.
    wire = json.dumps(value, ensure_ascii=False, allow_nan=False)
    return json.loads(restore_arguments(wire, mapping))


class AnthropicStreamRestorer:
    """Keep independent text/JSON state per block; flush only before block stop."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.text = _StreamingPlaceholderReplacer(mapping)
        self.blocks = {}
        self.seen = False
        self.terminated = False
        self.restored_fields = 0

    def accepts(self, event):
        kind = get_field(event, 'type')
        return kind in _NATIVE_EVENTS or (self.seen and kind in {'error', 'ping'})

    def _restore_message(self, message):
        for target, field in message_targets(message):
            old = get_field(target, field)
            new = restore_tool_input(old, self.mapping) if field == 'input' else restore_text(old, self.mapping)
            if new != old:
                self.restored_fields += 1
                set_field(target, field, new)

    def process(self, event):
        kind = get_field(event, 'type')
        if self.terminated and kind != 'ping':
            raise IncompleteUpstreamStream('anthropic_data_after_terminal')
        self.seen = True
        if kind == 'message_start':
            self._restore_message(get_field(event, 'message'))
        elif kind == 'content_block_start':
            index = get_field(event, 'index')
            if type(index) is not int or index < 0 or index in self.blocks:
                raise IncompleteUpstreamStream('anthropic_invalid_block_start')
            block = get_field(event, 'content_block')
            block_type = get_field(block, 'type')
            self.blocks[index] = {'type': block_type, 'json': None, 'raw': [], 'size': 0}
            if block_type == 'text':
                value = get_field(block, 'text')
                if isinstance(value, str):
                    restored = self.text.push(index, value)
                    self.restored_fields += int(restored != value)
                    set_field(block, 'text', restored)
            elif block_type in {'tool_use', 'server_tool_use'}:
                self._restore_message({'content': [block]})
        elif kind == 'content_block_delta':
            index = get_field(event, 'index')
            if index not in self.blocks:
                raise IncompleteUpstreamStream('anthropic_delta_without_block')
            state = self.blocks[index]
            delta = get_field(event, 'delta')
            delta_type = get_field(delta, 'type')
            if delta_type == 'text_delta' and state['type'] == 'text':
                value = get_field(delta, 'text')
                if not isinstance(value, str):
                    raise IncompleteUpstreamStream('anthropic_invalid_text_delta')
                restored = self.text.push(index, value)
                self.restored_fields += int(restored != value)
                set_field(delta, 'text', restored)
            elif delta_type == 'input_json_delta' and state['type'] in {'tool_use', 'server_tool_use'}:
                value = get_field(delta, 'partial_json')
                if not isinstance(value, str):
                    raise IncompleteUpstreamStream('anthropic_invalid_input_delta')
                state['size'] += len(value.encode('utf-8'))
                if state['size'] > MAX_TOOL_ARGUMENT_BYTES:
                    raise IncompleteUpstreamStream('anthropic_tool_input_too_large')
                state['raw'].append(value)
                if state['json'] is None:
                    state['json'] = _StreamingJSONRestorer(self.mapping)
                restored = state['json'].push(value)
                self.restored_fields += int(restored != value)
                set_field(delta, 'partial_json', restored)
        elif kind == 'content_block_stop':
            index = get_field(event, 'index')
            if index not in self.blocks:
                raise IncompleteUpstreamStream('anthropic_stop_without_block')
            state = self.blocks.pop(index)
            if state['json'] is not None:
                try:
                    value = json.loads(''.join(state['raw']))
                except (ValueError, TypeError):
                    raise IncompleteUpstreamStream('anthropic_incomplete_tool_input') from None
                if not isinstance(value, dict):
                    raise IncompleteUpstreamStream('anthropic_invalid_tool_input')
                tail = state['json'].flush()
                delta = {'type': 'input_json_delta', 'partial_json': tail}
            else:
                tail = self.text.flush(index) if state['type'] == 'text' else ''
                delta = {'type': 'text_delta', 'text': tail}
            if tail:
                self.restored_fields += 1
                return [{'type': 'content_block_delta', 'index': index, 'delta': delta}, event]
        elif kind == 'message_stop':
            if self.blocks:
                raise IncompleteUpstreamStream('anthropic_terminal_with_open_blocks')
            self.terminated = True
        elif kind == 'error':
            self.terminated = True
            self.finish()
        return [event]

    def finish(self):
        # No success event or buffered suffix is invented on EOF/cancellation.
        self.blocks.clear()
        self.text._pending.clear()


class SSEEventRestorer:
    """Decode complete SSE records, transform JSON events, preserve wire type.

    OpenAI SDK event objects keep their existing path; this adapter is used only
    for raw byte/string iterators such as /v1/messages. Non-SSE chunks pass through.
    """

    def __init__(self, process):
        self.process = process
        self.pending = bytearray()
        self.lines = []
        self.size = 0
        self.wire_type = None
        self.sse = None

    def _record(self):
        lines, self.lines = self.lines, []
        self.size = 0
        data = []
        for line in lines:
            if line.startswith(b'data:'):
                value = line[5:]
                data.append(value[1:] if value.startswith(b' ') else value)
        raw = b'\n'.join(lines) + b'\n\n'
        if not data:
            return [raw]
        payload = b'\n'.join(data)
        if payload == b'[DONE]':
            return [raw]
        try:
            event = json.loads(payload)
        except (ValueError, UnicodeError):
            raise IncompleteUpstreamStream('invalid_event_json') from None
        if not isinstance(event, dict):
            raise IncompleteUpstreamStream('invalid_event_shape')
        before = copy.deepcopy(event)
        output = self.process(event)
        if len(output) == 1 and output[0] == before:
            return [raw]
        records = []
        for transformed in output:
            # A synthesized flush delta must not inherit a stop event's ID.
            prefix = [line for line in lines if not line.startswith((b'data:', b'event:'))] if transformed is event else []
            kind = get_field(transformed, 'type')
            if isinstance(kind, str):
                prefix.append(b'event: ' + kind.encode())
            prefix.append(b'data: ' + json.dumps(transformed, ensure_ascii=False).encode('utf-8'))
            records.append(b'\n'.join(prefix) + b'\n\n')
        return records

    def feed(self, chunk):
        if self.wire_type is None:
            self.wire_type = bytes if isinstance(chunk, (bytes, bytearray)) else str
        raw = chunk.encode('utf-8') if isinstance(chunk, str) else bytes(chunk)
        if self.sse is False:
            return [chunk]
        self.pending.extend(raw)
        if self.sse is None:
            value = bytes(self.pending).lstrip(b'\r\n')
            prefixes = (b'event:', b'data:', b':', b'id:', b'retry:')
            if not any(value.startswith(prefix) or prefix.startswith(value) for prefix in prefixes):
                self.sse = False
                result = bytes(self.pending)
                self.pending.clear()
                return [result if self.wire_type is bytes else result.decode('utf-8')]
            if not any(value.startswith(prefix) for prefix in prefixes):
                return []
            self.sse = True
        result = []
        while True:
            match = re.search(b'\r\n|\r|\n', self.pending)
            if match is None or (match.group() == b'\r' and match.end() == len(self.pending)):
                break
            line = bytes(self.pending[:match.start()])
            del self.pending[:match.end()]
            self.size += len(line) + 1
            if self.size > MAX_SSE_EVENT_BYTES:
                raise IncompleteUpstreamStream('event_too_large')
            if line:
                self.lines.append(line)
            else:
                result.extend(self._record())
        if self.size + len(self.pending) > MAX_SSE_EVENT_BYTES:
            raise IncompleteUpstreamStream('event_too_large')
        return [value if self.wire_type is bytes else value.decode('utf-8') for value in result]

    def finish(self):
        output = []
        if self.sse is None and self.pending:
            output = [bytes(self.pending)]
            self.pending.clear()
        elif self.sse and self.pending == b'\r':
            self.pending.clear()
            output = self._record()
        if self.sse and (self.pending or self.lines):
            raise IncompleteUpstreamStream('truncated_record')
        return [item if self.wire_type is bytes else item.decode('utf-8') for item in output]


def normalize_messages_bridge_output(request_data, response, call_type):
    """Keep OpenAI Responses -> Anthropic conversion from dropping SDK objects.

    LiteLLM 1.98's chat bridge emits GenericResponseOutputItem objects, while
    its Messages adapter recognizes OpenAI SDK items or dictionaries. Normalize
    only this proxy route, via the public deployment callback, before conversion.
    No originals are restored here; the serialized payload stays identical.
    """
    from urllib.parse import urlsplit
    from openai.types.responses import ResponseOutputMessage, ResponseFunctionToolCall

    kind = getattr(call_type, 'value', call_type)
    if kind not in {'aresponses', 'acompletion'}:
        return
    metadata = request_data.get('litellm_metadata') or {}
    endpoint = metadata.get('endpoint')
    if not isinstance(endpoint, str) or urlsplit(endpoint).path.rstrip('/') not in {
        '/v1/messages', '/anthropic/v1/messages',
    }:
        return
    def validate_arguments(arguments):
        if arguments in (None, ''):
            return
        try:
            parsed = json.loads(arguments)
            valid = isinstance(parsed, dict)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            from litellm.exceptions import APIError
            raise APIError(
                status_code=502, message="Provider returned invalid Messages tool argument JSON.",
                llm_provider=str(request_data.get('custom_llm_provider') or 'unknown'),
                model=str(get_field(response, 'model') or 'unknown'),
            )

    if kind == 'acompletion':
        for choice in get_field(response, 'choices') or []:
            message = get_field(choice, 'message')
            for tool in get_field(message, 'tool_calls') or []:
                validate_arguments(get_field(get_field(tool, 'function'), 'arguments'))
            legacy = get_field(message, 'function_call')
            if legacy is not None:
                validate_arguments(get_field(legacy, 'arguments'))
        return
    output = getattr(response, 'output', None)
    if not isinstance(output, list):
        return
    normalized = []
    changed = False
    for item in output:
        kind = get_field(item, 'type')
        if kind == 'function_call':
            validate_arguments(get_field(item, 'arguments'))
        target = {'message': ResponseOutputMessage, 'function_call': ResponseFunctionToolCall}.get(kind)
        if target is not None and not isinstance(item, (dict, target)) and hasattr(item, 'model_dump'):
            # Validate the exact provider payload; do not fabricate missing text.
            try:
                payload = item.model_dump(mode='json')
                target.model_validate(payload)
                # The adapter explicitly supports dictionaries. Keep the exact
                # serialized fields instead of adding SDK default/null fields.
                item = payload
            except (ValueError, TypeError):
                raise IncompleteUpstreamStream('invalid_messages_bridge_output') from None
            changed = True
        normalized.append(item)
    if changed:
        response.output = normalized


class SequentialMessagesToolDeltas:
    """Keep the single-active-block Messages adapter from joining parallel JSON.

    The first tool keeps streaming unchanged. Other tools are collected per
    OpenAI index and emitted individually before the real finish chunk. Text,
    usage, IDs and the final payload are preserved; EOF never invents a finish.
    """

    def __init__(self):
        self.active = None
        self.pending = {}
        self.buffered_bytes = 0

    def process(self, event):
        choices = event.get('choices')
        if not isinstance(choices, list) or not choices:
            return [event]
        if len(choices) != 1:
            raise IncompleteUpstreamStream('messages_multiple_choices_unsupported')
        choice = choices[0]
        delta = choice.get('delta') or {}
        tools = delta.get('tool_calls')
        if isinstance(tools, list):
            keep = []
            for tool in tools:
                index = tool.get('index', 0)
                if type(index) is not int or index < 0:
                    raise IncompleteUpstreamStream('invalid_messages_tool_index')
                if self.active is None:
                    self.active = index
                if index == self.active:
                    keep.append(tool)
                    continue
                if index not in self.pending and len(self.pending) >= 127:
                    raise IncompleteUpstreamStream('messages_too_many_tools')
                state = self.pending.setdefault(index, {'id': None, 'name': '', 'parts': [], 'size': 0})
                identifier = tool.get('id')
                if identifier:
                    if state['id'] not in (None, identifier):
                        raise IncompleteUpstreamStream('messages_tool_id_changed')
                    state['id'] = identifier
                function = tool.get('function') or {}
                name = function.get('name')
                if name and name != state['name']:
                    state['name'] += name
                arguments = function.get('arguments')
                if arguments is not None:
                    if not isinstance(arguments, str):
                        raise IncompleteUpstreamStream('invalid_messages_tool_arguments')
                    size = len(arguments.encode('utf-8'))
                    state['size'] += size
                    self.buffered_bytes += size
                    if state['size'] > MAX_TOOL_ARGUMENT_BYTES or self.buffered_bytes > 2 * MAX_TOOL_ARGUMENT_BYTES:
                        raise IncompleteUpstreamStream('messages_buffered_tools_too_large')
                    state['parts'].append(arguments)
            delta['tool_calls'] = keep
            choice['delta'] = delta
        if choice.get('finish_reason') is None or not self.pending:
            return [event]
        if choice.get('finish_reason') not in {'tool_calls', 'function_call'}:
            raise IncompleteUpstreamStream('messages_incomplete_parallel_tools')
        buffered = []
        for index, state in self.pending.items():
            arguments = ''.join(state['parts'])
            try:
                valid = isinstance(json.loads(arguments), dict)
            except (ValueError, TypeError):
                valid = False
            if not valid or not state['id'] or not state['name']:
                raise IncompleteUpstreamStream('invalid_messages_buffered_tool')
            tool = {'index': index, 'id': state['id'], 'type': 'function',
                    'function': {'name': state['name'], 'arguments': arguments}}
            frame = {key: copy.deepcopy(value) for key, value in event.items() if key not in {'choices', 'usage'}}
            frame['choices'] = [{'index': choice.get('index', 0), 'delta': {'tool_calls': [tool]}, 'finish_reason': None}]
            buffered.append(frame)
        self.pending.clear()
        self.buffered_bytes = 0
        output = []
        if any(value for value in delta.values()):
            tail = copy.deepcopy(event)
            tail.pop('usage', None)
            tail['choices'][0]['finish_reason'] = None
            output.append(tail)
        choice['delta'] = {}
        return [*output, *buffered, event]


class MessagesChatToolStream(httpx.AsyncByteStream):
    """Public HTTPX byte-stream wrapper, request-owned and cancellable."""

    def __init__(self, response, on_failure=None):
        self.response = response
        self.on_failure = on_failure
        self.order = SequentialMessagesToolDeltas()
        self.wire = SSEEventRestorer(self.order.process)

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_bytes():
                for record in self.wire.feed(chunk):
                    yield record
            for record in self.wire.finish():
                yield record
        except Exception as error:
            if self.on_failure is not None:
                self.on_failure(getattr(error, 'reason', 'messages_tool_stream_processing_failed'))
            raise

    async def aclose(self):
        self.order.pending.clear()
        self.wire.pending.clear()
        self.wire.lines.clear()
        await self.response.aclose()


def messages_chat_response(response, on_failure=None):
    import httpx
    headers = response.headers.copy()
    headers.pop('content-length', None)
    return httpx.Response(response.status_code, headers=headers,
        stream=MessagesChatToolStream(response, on_failure), request=response.request,
        extensions=response.extensions)


def is_messages_route(kwargs):
    from urllib.parse import urlsplit
    endpoint = (kwargs.get('litellm_metadata') or {}).get('endpoint')
    return isinstance(endpoint, str) and urlsplit(endpoint).path.rstrip('/') in {
        '/v1/messages', '/anthropic/v1/messages',
    }
