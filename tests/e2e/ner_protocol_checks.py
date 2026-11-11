"""Real Analyzer/Redis protocol checks, independent of strict NER-quality groups."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import urllib.error

from dkb_corpus_checks import capture, reset, payload_for
from ner_instruction_checks import _request, _restored_text

PHONE = "+79031234567"
BRANCH_PHONES = ("+79039876543", "+79030000001")
OPAQUE = "sealed:password=MustRemainOpaque42!;<PHONE_NUMBER_1>"
OVERLAP_TEXT = "Михаил Соколов mikhail.sokolov@outlook.com не могу войти в личный кабинет"
ENTRANCE_TEXT = "Дмитрий Соколов, адрес Московская 156 подъезд 2"
POSTFIX_TEXT = "Жду на Фрунзенская набережная 24"
BOUNDARY_TEXT = "Абонент Максим Лебедев, Новосибирск ул. Красный проспект 44 кв. 12"
CONTEXT_TEXT = "Адрес: москва, проспект вернадского 30"
EMAIL_TEXT = "марина.кузнецова@inbox.ru"
PASSPORT_FRAGMENT_TEXT = "Номер паспорта: 847291"
# The missing ')' leaves the candidate open. Keep the token count at 385
# so the negative case still reaches the unresolved Tiny2 boundary.
WINDOW_NUMBERED_INSERT = ("сведения " * 279 + "Сводка [12] о задании. "
                          + ("пример " * 54).rstrip() + ".\n"
                          "1: Начало.\n2:\n"
                          "3: Уточнение через `demo/tool`: (раздел Я. Полевой\n"
                          "4: Примеры: `demo.md`, `report.txt`)")
WINDOW_NUMBERED_UNCLOSED = "сведения " + WINDOW_NUMBERED_INSERT.removesuffix(")")
# Generated examples have 385–387 Tiny2 content tokens and require source
# bounds after ordinary and shifted prediction merging.
WINDOW_PROSE_CASES = (
    ("window_parenthetical", "сведения " * 309
     + "Сводка [12] по делу (К. Кратко, запись 407); "
     + ("пример " * 54).rstrip() + ". Проверка завершена."),
    ("window_wrapped", "сведения " * 285 + "Сводка [12] о задании. "
     + ("пример " * 54).rstrip() + ".\n"
     "8: Уточнение через `demo/tool` (раздел Я. Полевой\n"
     "9: Примеры: `demo.md`, `report.txt`)."),
    ("window_list", "сведения " * 289 + "Сводка по задаче.\n"
     "8: - **Начало.** Проверить инструкцию.\n"
     "9: - **Итог.** К. Кратко: " + ("пример " * 58).rstrip() + ". Проверка завершена."),
    ("window_numbered_paragraph", WINDOW_NUMBERED_INSERT + "\n5:\n"),
    ("window_numbered_parenthetical", WINDOW_NUMBERED_INSERT),
)

# Both independently authored texts reproduce an unresolved PERSON on v0.7.1.
WINDOW_PERSON_CONTROL_CASES = (
    ("window_person_inventory", "Морозов", "сведения " * 361
     + "[вложение]\n- Лист проверки (помета Морозов\n- см. `demo.txt` [черновик]\n"
     + "данные " * 150),
    ("window_person_agent", "Mira Meadow", "[agent]\n" + "сведения " * 375
     + "\nЗапись Mira Meadow\n[source]\n`artifact.md` " + "данные " * 150),
)
# Here the control window supplies no positive confirmation.
WINDOW_PERSON_CONTROL_UNCONFIRMED = ("[agent]\n" + "пример " * 376
                                     + "\nЗапись Коммит\n[source]\n`artifact.md` " + "данные " * 150)


def response_snapshot(body, stream):
    if not stream:
        return body
    return next(event["response"] for event in body if isinstance(event, dict)
                and event.get("type") == "response.completed")


def tool_values(body, api, stream):
    if api == "responses":
        snapshot = response_snapshot(body, stream)
        output = snapshot["output"][0]
        if stream:
            fragments = "".join(event.get("delta", "") for event in body if isinstance(event, dict)
                                and event.get("type") == "response.function_call_arguments.delta")
            assert json.loads(fragments) == json.loads(output["arguments"])
        return json.loads(output["arguments"])["values"]
    if not stream:
        return json.loads(body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["values"]
    arguments = "".join(call.get("function", {}).get("arguments", "")
                        for event in body if isinstance(event, dict)
                        for choice in event.get("choices", [])
                        for call in choice.get("delta", {}).get("tool_calls", []))
    assert "[DONE]" in body
    assert any(choice.get("finish_reason") == "tool_calls"
               for event in body if isinstance(event, dict) for choice in event.get("choices", []))
    return json.loads(arguments)["values"]


def assert_capture(url, *, calls=1):
    state = capture(url)
    assert state["provider_requests"] == calls, state
    assert not state["provider_saw_raw_phone"], state
    assert not state["provider_saw_e9_raw_phone"], state
    if calls:
        assert state["provider_saw_pii_placeholder"], state


def check_punctuation_passwords(proxy, blocked, provider):
    counts = {"punctuation_password_mask": 0, "punctuation_password_policy_block": 0,
              "punctuation_password_block": 0}
    # Standalone env assignments retain the separately accepted final-policy block.
    cases = [("Пароль: !?$", "!?$", False), ("DB_PASSWORD=??!", "??!", True),
             ("Пароль: ???", "???", False), ("PASSWORD=?", "?", True), ('Пароль: "!?$"', "!?$", False)]
    for text, value, policy_block in cases:
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                for tool in (False, True):
                    payload = payload_for(text, api, stream, tool)
                    payload["extra_body"] = {"k07_password_echo": True}
                    if tool:
                        function = {"name": "save", "parameters": {"type": "object", "properties": {
                            "values": {"type": "array", "items": {"type": "string"}},
                        }}}
                        payload["tools"] = ([{"type": "function", "function": function}]
                                            if api == "chat/completions" else [{"type": "function", **function}])
                    reset(provider)
                    try:
                        body = _request(proxy, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as exc:
                        detail = json.load(exc).get("error", {})
                        assert policy_block and exc.code == 422, (text, api, stream, tool, exc.code)
                        assert "confirmed raw leak marker" in detail.get("message", ""), detail
                        assert capture(provider)["provider_requests"] == 0
                        counts["punctuation_password_policy_block"] += 1
                    else:
                        assert not policy_block, (text, api, stream, tool, "Expected final-policy block")
                        if tool:
                            assert tool_values(body, api, stream) == [value], (text, api, stream)
                        else:
                            assert _restored_text(body, api, stream) == value, (text, api, stream)
                        state = capture(provider)
                        assert state["provider_requests"] == 1 and state["provider_saw_pii_placeholder"], state
                        # All question marks in this matrix belong to passwords,
                        # including those in source tool-argument JSON.
                        assert not state["provider_saw_question_mark"], (text, api, stream, tool, state)
                        counts["punctuation_password_mask"] += 1
                    reset(provider)
                    try:
                        _request(blocked, "/v1/" + api, payload, stream=stream)
                    except urllib.error.HTTPError as exc:
                        assert exc.code == 422
                    else:
                        raise AssertionError((text, "Punctuation password was not blocked"))
                    assert capture(provider)["provider_requests"] == 0
                    counts["punctuation_password_block"] += 1
    return counts


def native_json_result(body, api, stream, tool):
    if not tool:
        return _restored_text(body, api, stream)
    if api == 'responses':
        text = response_snapshot(body, stream)['output'][0]['arguments']
        if stream:
            deltas = ''.join(event.get('delta', '') for event in body if isinstance(event, dict)
                            and event.get('type') == 'response.function_call_arguments.delta')
            assert json.loads(deltas) == json.loads(text)
        return text
    if not stream:
        return body['choices'][0]['message']['tool_calls'][0]['function']['arguments']
    return ''.join(call.get('function', {}).get('arguments', '') for event in body
                   if isinstance(event, dict) for choice in event.get('choices', [])
                   for call in choice.get('delta', {}).get('tool_calls', []))


def check_native_json_strings(proxy, blocked, provider):
    counts = {'native_string_mask': 0, 'native_string_block': 0}
    for password in ('Alpha\nBeta"\\file', r'Literal\nValue\\path', 'Синтетический\tПароль😀'):
        values = {'password': password, 'ticket': 654321}
        source = json.dumps(values)
        raw = json.dumps(password)[1:-1]
        for api in ('chat/completions', 'responses'):
            for stream in (False, True):
                for tool in (False, True):
                    # Reuse the existing native-argument echo; it is not number-specific.
                    behavior = 'JSON_NUMBER_ECHO E9_FRAGMENTED_RESPONSE' + (' NATIVE_ARGUMENT_TOOL' if tool else '')
                    payload = payload_for(behavior, api, stream, True)
                    payload['metadata'] = {'test_behavior': behavior}
                    target = (payload['messages'][1]['tool_calls'][0]['function']
                              if api == 'chat/completions' else payload['input'][1])
                    target['arguments'] = source
                    if tool:
                        function = {'name': 'inspect', 'parameters': {'type': 'object', 'properties': {
                            'password': {'type': 'string'}, 'ticket': {'type': 'integer'},
                        }}}
                        payload['tools'] = ([{'type': 'function', 'function': function}] if api == 'chat/completions'
                                            else [{'type': 'function', **function}])
                    reset(provider)
                    body = _request(proxy, '/v1/' + api, payload, stream=stream)
                    state = capture(provider)
                    captured = state['provider_native_arguments']
                    assert len(captured) == 1 and state['provider_requests'] == 1, (api, stream, tool, state)
                    outgoing = json.loads(captured[0])
                    assert outgoing['ticket'] == 654321 and raw not in captured[0]
                    assert outgoing['password'].startswith('<PASSWORD_'), (api, stream, tool, outgoing)
                    text = native_json_result(body, api, stream, tool)
                    assert json.loads(text) == values, (api, stream, tool, text)
                    if not tool:
                        assert text == source
                    counts['native_string_mask'] += 1
                    reset(provider)
                    try:
                        _request(blocked, '/v1/' + api, payload, stream=stream)
                    except urllib.error.HTTPError as exc:
                        assert exc.code == 422
                    else:
                        raise AssertionError('Native JSON password was not blocked')
                    assert capture(provider)['provider_requests'] == 0
                    counts['native_string_block'] += 1
    return counts


def check_native_json_numbers(proxy, blocked, provider):
    counts = {'native_number_mask': 0, 'native_number_block': 0, 'native_number_history': 0}
    cases = [
        {'passport_number': 847291, 'passport': '001234', 'ticket': 654321},
        {'passport_number': '847291', 'passport': '001234', 'ticket': 654321},
        {'passport_number': 847291, 'passport': '847291', 'ticket': 654321},
    ]
    for values in cases:
        for api in ('chat/completions', 'responses'):
            for stream in (False, True):
                for tool in (False, True):
                    behavior = 'JSON_NUMBER_ECHO E9_FRAGMENTED_RESPONSE' + (' NATIVE_ARGUMENT_TOOL' if tool else '')
                    payload = payload_for(behavior, api, stream, True)
                    payload['metadata'] = {'test_behavior': behavior}
                    target = (payload['messages'][1]['tool_calls'][0]['function']
                              if api == 'chat/completions' else payload['input'][1])
                    target['arguments'] = json.dumps(values)
                    if tool:
                        function = {'name': 'inspect', 'parameters': {'type': 'object', 'properties': {
                            'passport_number': {}, 'passport': {}, 'ticket': {'type': 'integer'},
                        }}}
                        payload['tools'] = ([{'type': 'function', 'function': function}] if api == 'chat/completions'
                                            else [{'type': 'function', **function}])
                    reset(provider)
                    body = _request(proxy, '/v1/' + api, payload, stream=stream)
                    state = capture(provider)
                    captured = state['provider_native_arguments']
                    assert len(captured) == 1, (api, stream, tool, state)
                    outgoing = json.loads(captured[0])
                    assert outgoing['ticket'] == 654321
                    assert str(values['passport_number']) not in captured[0] and '001234' not in captured[0]
                    assert outgoing['passport_number'].startswith('<RU_PASSPORT_')
                    assert outgoing['passport'].startswith('<RU_PASSPORT_')
                    assert outgoing['passport_number'] != outgoing['passport']
                    text = native_json_result(body, api, stream, tool)
                    actual = json.loads(text)
                    assert actual == values, (api, stream, tool, actual, values)
                    assert type(actual['passport_number']) is type(values['passport_number'])
                    counts['native_number_mask'] += 1
                    reset(provider)
                    try:
                        _request(blocked, '/v1/' + api, payload, stream=stream)
                    except urllib.error.HTTPError as exc:
                        assert exc.code == 422
                    else:
                        raise AssertionError('Native passport number was not blocked')
                    assert capture(provider)['provider_requests'] == 0
                    counts['native_number_block'] += 1
    for stream in (False, True):
        reset(provider)
        first = payload_for('ok', 'responses', stream, True)
        first['input'][1]['arguments'] = json.dumps({'passport_number': 847291, 'passport': '001234'})
        previous = None
        for index in range(3):
            payload = first if index == 0 else {'model': 'mock-responses', 'input': [], 'stream': stream}
            payload['store'] = False
            payload['metadata'] = {'test_behavior': 'STATEFUL_HISTORY HISTORY_TOOL E9_FRAGMENTED_RESPONSE'}
            if previous:
                payload['previous_response_id'] = previous
            body = _request(proxy, '/v1/responses', payload, stream=stream)
            assert tool_values(body, 'responses', stream) == [847291, '001234']
            previous = response_snapshot(body, stream)['id']
            counts['native_number_history'] += 1
        assert capture(provider)['provider_requests'] == 3
        assert not capture(provider)['provider_saw_canary']
    return counts


def check(proxy, blocked, provider):
    counts = {"matrix": 0, "blocked": 0, "history": 0, "encrypted": 0}
    text_cases = (("overlap", OVERLAP_TEXT), ("entrance", ENTRANCE_TEXT),
                  ("postfix", POSTFIX_TEXT), ("boundary", BOUNDARY_TEXT),
                  ("context", CONTEXT_TEXT), ("email", EMAIL_TEXT),
                  ("passport", PASSPORT_FRAGMENT_TEXT))
    control_cases = tuple((case, text) for case, _, text in WINDOW_PERSON_CONTROL_CASES)
    for case, text in text_cases + WINDOW_PROSE_CASES + control_cases:
        counts[case + "_mask"] = counts[case + "_block"] = 0
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                payload = {"model": "mock-chat" if api == "chat/completions" else "mock-responses",
                           "stream": stream}
                payload.update({"messages": [{"role": "user", "content": text}]}
                               if api == "chat/completions" else {"input": text})
                reset(provider)
                body = _request(proxy, "/v1/" + api, payload, stream=stream)
                assert _restored_text(body, api, stream) == text, (case, api, stream, "restoration")
                state = capture(provider)
                assert state["provider_requests"] == 1 and state["provider_saw_pii_placeholder"], state
                assert not state["provider_saw_canary"], (case, api, stream, "value reached provider")
                counts[case + "_mask"] += 1
                reset(provider)
                try:
                    _request(blocked, "/v1/" + api, payload, stream=stream)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 422
                else:
                    raise AssertionError((case, "Sensitive values were not blocked"))
                assert capture(provider)["provider_requests"] == 0
                counts[case + "_block"] += 1

    for case, text in (("window_numbered_unclosed", WINDOW_NUMBERED_UNCLOSED),
                       ("window_person_control_unconfirmed", WINDOW_PERSON_CONTROL_UNCONFIRMED)):
        counts[case + "_no_egress"] = 0
        for api in ("chat/completions", "responses"):
            for stream in (False, True):
                payload = payload_for(text, api, stream, False)
                reset(provider)
                try:
                    _request(proxy, "/v1/" + api, payload, stream=stream)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 503, (case, api, stream, exc.code)
                    assert "window_boundary_unresolved" in exc.read().decode()
                else:
                    raise AssertionError((case, api, stream, "Unconfirmed boundary reached the provider"))
                assert capture(provider)["provider_requests"] == 0
                counts[case + "_no_egress"] += 1

    for api in ("chat/completions", "responses"):
        for stream in (False, True):
            for tool in (False, True):
                payload = {"model": "mock-chat" if api == "chat/completions" else "mock-responses", "stream": stream}
                if api == "chat/completions":
                    payload.update(messages=[{"role": "user", "content": "STOCK_BRIDGE_ECHO Телефон " + PHONE}])
                    if tool:
                        payload["tools"] = [{"type": "function", "function": {"name": "save",
                            "parameters": {"type": "object", "properties": {"values": {"type": "array", "items": {"type": "string"}}}}}}]
                else:
                    payload.update(input="Телефон " + PHONE, store=False,
                                   metadata={"test_behavior": "STATEFUL_HISTORY E9_FRAGMENTED_RESPONSE" + (" HISTORY_TOOL" if tool else "")})
                reset(provider)
                body = _request(proxy, "/v1/" + api, payload, stream=stream)
                if tool:
                    assert tool_values(body, api, stream) == [PHONE]
                else:
                    assert _restored_text(body, api, stream) == PHONE, (api, stream, "text echo differs")
                assert_capture(provider)
                counts["matrix"] += 1
                reset(provider)
                try:
                    _request(blocked, "/v1/" + api, payload, stream=stream)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 422
                else:
                    raise AssertionError("Sensitive block-mode request succeeded")
                assert_capture(provider, calls=0)
                counts["blocked"] += 1

    for stream in (False, True):
        for tool in (False, True):
            def turn(text, previous=None):
                payload = {"model": "mock-responses", "input": text, "stream": stream, "store": False,
                           "metadata": {"test_history": "STATEFUL_HISTORY E9_FRAGMENTED_RESPONSE" + (" HISTORY_TOOL" if tool else "")}}
                if previous:
                    payload["previous_response_id"] = previous
                return _request(proxy, "/v1/responses", payload, stream=stream)

            reset(provider)
            parent = turn("Телефон " + PHONE)
            response_id = response_snapshot(parent, stream)["id"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                children = list(pool.map(lambda phone: turn("Телефон " + phone, response_id), BRANCH_PHONES))
            for child, phone in zip(children, BRANCH_PHONES):
                if tool:
                    assert tool_values(child, "responses", stream) == [PHONE, phone]
                else:
                    assert _restored_text(child, "responses", stream) == PHONE + " / " + phone
            assert_capture(provider, calls=3)
            counts["history"] += 3

    for stream in (False, True):
        payload = {"model": "mock-responses", "stream": stream, "store": False,
                   "metadata": {"test_behavior": "E9_ECHO_ENCRYPTED E9_FRAGMENTED_RESPONSE"},
                   "input": [{"id": "rs_client", "type": "reasoning", "summary": [], "encrypted_content": OPAQUE},
                             {"role": "user", "content": [{"type": "input_text", "text": "Телефон " + PHONE}]}]}
        reset(provider)
        body = _request(proxy, "/v1/responses", payload, stream=stream)
        assert _restored_text(body, "responses", stream) == "Телефон " + PHONE
        snapshot = response_snapshot(body, stream)
        reasoning = [item for item in snapshot["output"] if item["type"] == "reasoning"]
        assert len(reasoning) == 1 and reasoning[0]["encrypted_content"] == OPAQUE
        assert capture(provider)["provider_encrypted_content_sha256"] == [hashlib.sha256(OPAQUE.encode()).hexdigest()]
        assert_capture(provider)
        counts["encrypted"] += 1
    counts.update(check_native_json_numbers(proxy, blocked, provider))
    counts.update(check_native_json_strings(proxy, blocked, provider))
    counts.update(check_punctuation_passwords(proxy, blocked, provider))
    print(json.dumps({"status": "passed", "real_ner_protocol_checks": counts}))
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--block-proxy-url", required=True)
    parser.add_argument("--capture-url", required=True)
    args = parser.parse_args()
    check(args.proxy_url, args.block_proxy_url, args.capture_url)
