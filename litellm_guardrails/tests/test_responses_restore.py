"""Tests for reversible masking in the OpenAI Responses API."""

import json
from unittest.mock import AsyncMock

import pytest
from litellm.types.llms.openai import (
    OutputTextDeltaEvent,
    OutputTextDoneEvent,
    ResponsesAPIResponse,
    ResponsesAPIStreamEvents,
)

from litellm_guardrails.pii_guardrail import RuPIIGuardrail
from litellm_guardrails.responses_restore import (
    ResponsesStreamRestorer,
    restore_responses_response,
)


def _response(text: str) -> ResponsesAPIResponse:
    return ResponsesAPIResponse(
        id="resp_1",
        created_at=1,
        object="response",
        model="gpt-5.6-sol",
        status="completed",
        parallel_tool_calls=False,
        output=[
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": text, "annotations": []}
                ],
            }
        ],
    )


def test_restores_typed_unary_response():
    response = _response("Implemented CompanynameabcService")

    changed = restore_responses_response(
        response,
        {"Companynameabc": "Kdir"},
    )

    assert changed == 1
    assert response.output[0].content[0].text == "Implemented KdirService"


def test_does_not_restore_opaque_encrypted_content():
    response = {
        "object": "response",
        "output": [
            {
                "type": "reasoning",
                "encrypted_content": "Companynameabc",
                "summary": [{"type": "summary_text", "text": "Companynameabc"}],
            }
        ],
    }

    changed = restore_responses_response(response, {"Companynameabc": "Kdir"})

    assert changed == 1
    assert response["output"][0]["encrypted_content"] == "Companynameabc"
    assert response["output"][0]["summary"][0]["text"] == "Kdir"


def test_restores_placeholder_split_across_typed_stream_events():
    restorer = ResponsesStreamRestorer({"Companynameabc": "Kdir"})
    events = [
        OutputTextDeltaEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
            item_id="item_1",
            output_index=0,
            content_index=0,
            delta="Implemented Companyname",
        ),
        OutputTextDeltaEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
            item_id="item_1",
            output_index=0,
            content_index=0,
            delta="abcService",
        ),
        OutputTextDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            item_id="item_1",
            output_index=0,
            content_index=0,
            text="Implemented CompanynameabcService",
        ),
    ]

    output = []
    for event in events:
        restored, _ = restorer.feed(event)
        output.extend(restored)
    tails, _ = restorer.flush()
    output.extend(tails)

    payloads = [
        json.loads(item) if isinstance(item, str) else item.model_dump(mode="json")
        for item in output
    ]
    deltas = [
        item["delta"]
        for item in payloads
        if item["type"] == "response.output_text.delta"
    ]
    done = next(
        item["text"]
        for item in payloads
        if item["type"] == "response.output_text.done"
    )

    assert "".join(deltas) == "Implemented KdirService"
    assert done == "Implemented KdirService"
    assert "Companynameabc" not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_guardrail_restores_responses_using_litellm_internal_metadata():
    guardrail = RuPIIGuardrail()
    guardrail._load_mapping = AsyncMock(return_value={"Companynameabc": "Kdir"})
    guardrail._delete_mapping = AsyncMock()
    response = _response("Implemented CompanynameabcService")
    data = {"litellm_metadata": {"pii_request_id": "mapping-id"}}

    await guardrail.async_post_call_success_hook(data, None, response)

    assert response.output[0].content[0].text == "Implemented KdirService"
    guardrail._load_mapping.assert_awaited_once_with("mapping-id")
    guardrail._delete_mapping.assert_awaited_once_with("mapping-id")
