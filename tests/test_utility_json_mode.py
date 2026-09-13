"""A hosted route that refuses a strict JSON schema (SPEC §6.3, §22.1).

Live, 2026-09-13: DeepSeek answers `400 This response_format type is
unavailable now` to `{"type": "json_schema", "strict": true}` and parses fine
with `{"type": "json_object"}` — measured five runs each, both goal kinds. The
strict form had failed 69 times since 2026-09-10, and because the only caller
that asks for one is the promise review, she had filed no promise goal at all
and could therefore never decide to reach out. Offline here: litellm is faked.
"""
from __future__ import annotations

import pytest

from yurios.app.providers.openrouter import LiteLLMUtilityModel, _plain_json

SCHEMA = {"type": "json_schema",
          "json_schema": {"name": "promise_review", "strict": True,
                          "schema": {"type": "object"}}}


class Refusing:
    """First call raises the way the hosted route does; later ones answer."""

    def __init__(self, message="This response_format type is unavailable now"):
        self.calls: list[dict] = []
        self.message = message

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1 and self.message:
            raise RuntimeError(
                f"litellm.BadRequestError: OpenAIException - {self.message}")

        class Msg:
            content = '{"goal": null}'

        class Choice:
            message = Msg()
            finish_reason = "stop"

        class Response:
            choices = [Choice()]
            usage = None

        return Response()


def test_a_strict_schema_becomes_plain_json_mode():
    assert _plain_json(SCHEMA) == {"type": "json_object"}
    # anything else is handed on untouched
    assert _plain_json({"type": "json_object"}) == {"type": "json_object"}
    assert _plain_json(None) is None


async def test_a_route_that_refuses_the_schema_is_asked_again_in_json_mode(monkeypatch):
    fake = Refusing()
    monkeypatch.setattr("litellm.acompletion", fake)
    model = LiteLLMUtilityModel("openai/deepseek-flash", "k")
    out = await model.complete([{"role": "user", "content": "hi"}],
                               response_format=SCHEMA)
    assert out == '{"goal": null}'
    assert len(fake.calls) == 2
    assert fake.calls[0]["response_format"] == SCHEMA
    assert fake.calls[1]["response_format"] == {"type": "json_object"}


async def test_the_downgrade_is_remembered_so_it_costs_one_call(monkeypatch):
    fake = Refusing()
    monkeypatch.setattr("litellm.acompletion", fake)
    model = LiteLLMUtilityModel("openai/deepseek-flash", "k")
    for _ in range(3):
        await model.complete([{"role": "user", "content": "hi"}],
                             response_format=SCHEMA)
    # one refusal, then every later call goes out in json mode straight away
    assert len(fake.calls) == 4
    assert [c["response_format"] for c in fake.calls[1:]] ==         [{"type": "json_object"}] * 3


async def test_any_other_failure_is_still_a_failure(monkeypatch):
    fake = Refusing(message="rate limit exceeded")
    monkeypatch.setattr("litellm.acompletion", fake)
    model = LiteLLMUtilityModel("openai/deepseek-flash", "k")
    with pytest.raises(RuntimeError):
        await model.complete([{"role": "user", "content": "hi"}],
                             response_format=SCHEMA)
    assert len(fake.calls) == 1


async def test_a_route_that_takes_the_schema_never_sees_the_downgrade(monkeypatch):
    fake = Refusing(message="")
    monkeypatch.setattr("litellm.acompletion", fake)
    model = LiteLLMUtilityModel("lm_studio/whatever", "k")
    await model.complete([{"role": "user", "content": "hi"}],
                         response_format=SCHEMA)
    assert len(fake.calls) == 1
    assert fake.calls[0]["response_format"] == SCHEMA
