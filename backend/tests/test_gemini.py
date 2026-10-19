import json

import httpx
import pytest
from pydantic import BaseModel

from app.graphs.common import LLMError
from app.services.gemini import EMBED_BATCH_PAUSE_S, EMBED_BATCH_SIZE, GeminiClient, GeminiEmbedder


class Out(BaseModel):
    answer: str
    score: int | None = None


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _gemini_ok(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_gemini_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers["x-goog-api-key"], json.loads(req.content)
        return httpx.Response(200, json=_gemini_ok('{"answer": "hi", "score": 3}'))

    llm = GeminiClient("KEY123", "gemini-test", http=_http(handler), sleep=lambda s: None)
    out = llm.structured(schema=Out, system="Be brief.", user="hello", prompt_version="v1")
    assert out == Out(answer="hi", score=3)
    assert seen["url"].endswith("/v1beta/models/gemini-test:generateContent")
    assert seen["key"] == "KEY123" and "KEY123" not in seen["url"]
    cfg = seen["body"]["generationConfig"]
    assert cfg["temperature"] == 0 and cfg["responseMimeType"] == "application/json"
    assert "responseSchema" not in cfg
    schema = cfg["responseJsonSchema"]
    assert schema["type"] == "object" and schema["properties"]["answer"]["type"] == "string"
    assert cfg["maxOutputTokens"] == 2048
    assert "thinkingConfig" not in cfg
    assert "default" not in json.dumps(schema)
    # The system text is the cacheable prefix: unchanged, and not mixed with the schema or the user text.
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "hello"


def test_flash_lite_asks_for_minimal_thinking():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_gemini_ok('{"answer":"ok"}'))

    llm = GeminiClient("K", "gemini-3.5-flash-lite", http=_http(handler), sleep=lambda s: None)
    llm.structured(schema=Out, system="Label it.", user="extract this", prompt_version="v")
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert "thinkingBudget" not in json.dumps(seen["body"])


def test_a_large_reference_is_cached_once_and_not_sent_again():
    from app.services.gemini import MIN_CACHE_TOKENS
    calls = []
    reference = "policy " * (MIN_CACHE_TOKENS * 2)

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((str(req.url), body))
        if str(req.url).endswith("/cachedContents"):
            assert body["systemInstruction"]["parts"][0]["text"] == "Same rules."
            assert body["contents"][0]["parts"][0]["text"] == reference
            return httpx.Response(200, json={"name": "cachedContents/abc"})
        return httpx.Response(200, json=_gemini_ok('{"answer":"ok"}'))

    llm = GeminiClient("K", "m", http=_http(handler), sleep=lambda s: None)
    llm.structured(schema=Out, system="Same rules.", user="profile one", prompt_version="v", context=reference)
    llm.structured(schema=Out, system="Same rules.", user="profile two", prompt_version="v", context=reference)
    creates = [c for c in calls if c[0].endswith("/cachedContents")]
    answers = [c for c in calls if c[0].endswith(":generateContent")]
    assert len(creates) == 1 and len(answers) == 2
    assert answers[0][1]["cachedContent"] == "cachedContents/abc"
    assert "systemInstruction" not in answers[0][1]
    assert answers[0][1]["contents"][0]["parts"][0]["text"] == "profile one"
    assert reference not in answers[0][1]["contents"][0]["parts"][0]["text"]
    assert answers[1][1]["contents"][0]["parts"][0]["text"] == "profile two"
    assert answers[0][1]["generationConfig"]["maxOutputTokens"] == 2048


def test_a_short_reference_stays_inline_ahead_of_the_question():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_gemini_ok('{"answer":"ok"}'))

    llm = GeminiClient("K", "m", http=_http(handler), sleep=lambda s: None)
    llm.structured(schema=Out, system="Rules.", user="the profile", prompt_version="v", context="short policy")
    assert seen["url"].endswith(":generateContent")
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "Rules."
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "short policy\nthe profile"


def test_chat_history_keeps_only_the_newest_turns():
    from app.graphs.job_post_graph import HISTORY_LIMIT, trim_history
    messages = [{"role": "user", "content": str(i)} for i in range(HISTORY_LIMIT + 5)]
    kept = trim_history(messages)
    assert len(kept) == HISTORY_LIMIT and kept[0]["content"] == "5" and kept[-1]["content"] == str(HISTORY_LIMIT + 4)


def test_system_instruction_stays_byte_identical_across_calls():
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body["systemInstruction"]["parts"][0]["text"])
        return httpx.Response(200, json=_gemini_ok('{"answer":"ok"}'))

    llm = GeminiClient("K", "m", http=_http(handler), sleep=lambda s: None)
    llm.structured(schema=Out, system="Same rules.", user="cv one", prompt_version="v")
    llm.structured(schema=Out, system="Same rules.", user="cv two", prompt_version="v")
    assert seen[0] == seen[1] == "Same rules."
    assert "cv one" not in seen[0] and "cv two" not in seen[1]


def test_gemini_retries_rate_limits_but_not_client_errors():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json=_gemini_ok('{"answer":"ok"}'))

    sleeps = []
    llm = GeminiClient("K", "m", http=_http(flaky), sleep=sleeps.append)
    assert llm.structured(schema=Out, system="s", user="u", prompt_version="v").answer == "ok"
    assert calls["n"] == 3 and sleeps == [5.0, 10.0]   # a few seconds between 429s, not an immediate failure

    calls["n"] = 0
    bad = GeminiClient("K", "m", http=_http(lambda r: (calls.__setitem__("n", calls["n"] + 1), httpx.Response(400))[1]), sleep=lambda s: None)
    with pytest.raises(LLMError):
        bad.structured(schema=Out, system="s", user="u", prompt_version="v")
    assert calls["n"] == 1


@pytest.mark.parametrize("payload", [{"candidates": []}, _gemini_ok("not json"), _gemini_ok('{"wrong": 1}')])
def test_gemini_bad_output_becomes_llm_error_without_leaking_content(payload):
    llm = GeminiClient("SECRETKEY", "m", http=_http(lambda r: httpx.Response(200, json=payload)))
    with pytest.raises(LLMError) as e:
        llm.structured(schema=Out, system="s", user="private text", prompt_version="v")
    assert "SECRETKEY" not in str(e.value) and "private text" not in str(e.value)


def test_429_uses_retry_after_when_google_sends_it():
    sleeps = []

    def limited(req):
        return httpx.Response(429, headers={"retry-after": "7"})

    llm = GeminiClient("K", "m", http=_http(limited), max_attempts=2, sleep=sleeps.append)
    with pytest.raises(LLMError, match="provider unavailable"):
        llm.structured(schema=Out, system="s", user="u", prompt_version="v")
    assert sleeps == [7.0]


def test_gemini_embeddings_batch_and_dimensions():
    sizes = []

    def handler(req):
        body = json.loads(req.content)
        sizes.append(len(body["requests"]))
        assert body["requests"][0]["outputDimensionality"] == 8 and body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 8} for _ in body["requests"]]})

    emb = GeminiEmbedder("K", "gemini-embedding-001", 8, http=_http(handler), sleep=lambda _s: None)
    vecs = emb.embed([f"t{i}" for i in range(12)], "RETRIEVAL_QUERY")
    assert len(vecs) == 12 and sizes == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 2]


def test_embedding_batches_pause_so_the_per_minute_window_can_clear():
    sleeps = []

    def handler(req):
        n = len(json.loads(req.content)["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 8} for _ in range(n)]})

    emb = GeminiEmbedder("K", "gemini-embedding-001", 8, http=_http(handler), sleep=sleeps.append)
    emb.embed([f"t{i}" for i in range(11)])
    assert sleeps == [EMBED_BATCH_PAUSE_S, EMBED_BATCH_PAUSE_S]
    assert 10 <= EMBED_BATCH_PAUSE_S <= 15 and 3 <= EMBED_BATCH_SIZE <= 5
