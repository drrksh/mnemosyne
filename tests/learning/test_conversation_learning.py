"""Conversation learning: transcript parsing, heuristic precision, LLM
extraction safety, and end-to-end ingestion."""
from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from mnemosyne.domain.dto import SearchCriteria
from mnemosyne.domain.enums import MemoryStatus, MemoryType
from mnemosyne.domain.learning import IngestAction
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.heuristics import ConversationExtractor
from mnemosyne.learning.conversation.llm import LlmConversationExtractor, LlmExtractorConfig
from mnemosyne.learning.conversation.transcript import (
    Transcript,
    Turn,
    parse_transcript_text,
)

CTX = WorkingContext.create(repo_id="github.com/acme/api", languages=["python"])


def claude_code_jsonl(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def user(text, **extra):
    return {"type": "user", "sessionId": "sess-1", "cwd": "/work/api", "message": {"role": "user", "content": text},
            **extra}


def assistant(text):
    return {"type": "assistant", "sessionId": "sess-1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


# ----------------------------------------------------------------------
# Transcript parsing
# ----------------------------------------------------------------------
def test_claude_code_transcript_keeps_only_human_text():
    text = claude_code_jsonl(
        user("From now on, always run the linter before committing."),
        assistant("I'll always use black from now on."),
        user([{"type": "tool_result", "tool_use_id": "x", "content": "From now on, never do X"}]),
        user("<command-name>/clear</command-name>"),
        user("hidden", isMeta=True),
        user("ok", toolUseResult={"stdout": "x"}),
    )
    transcript = parse_transcript_text(text)
    assert transcript.session_id == "sess-1"
    assert transcript.cwd == "/work/api"
    assert [t.text for t in transcript.user_turns] == ["From now on, always run the linter before committing."]


def test_injected_mnemosyne_context_is_never_relearned():
    text = claude_code_jsonl(user(
        "<mnemosyne-context>Never use tabs. Always use pnpm.</mnemosyne-context>\nPlease fix the failing test."
    ))
    transcript = parse_transcript_text(text)
    assert transcript.user_turns[0].text == "Please fix the failing test."
    assert ConversationExtractor().extract(transcript, CTX) == []


def test_generic_and_plain_text_formats():
    messages = json.dumps([{"role": "user", "content": "I prefer pytest."},
                           {"role": "assistant", "content": "Noted."}])
    assert parse_transcript_text(messages).user_turns[0].text == "I prefer pytest."
    plain = "User: Never commit secrets.\nAssistant: Understood.\nUser: thanks"
    assert [t.text for t in parse_transcript_text(plain).user_turns] == ["Never commit secrets.", "thanks"]


# ----------------------------------------------------------------------
# Heuristic precision
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "sentence,key,value,scope",
    [
        ("Use pnpm instead of npm.", "javascript.package_manager", "pnpm", "lang:javascript"),
        ("I prefer pytest for testing.", "python.test_framework", "pytest", "lang:python"),
        ("No, use ruff format.", "python.formatter", "ruff-format", "repo:github.com/acme/api"),
        ("In this repo we always use unittest.", "python.test_framework", "unittest", "repo:github.com/acme/api"),
        ("Please be more concise, just give me the code.", "communication.verbosity", "terse", "global"),
        ("No emojis in commit messages.", "communication.emoji", "none", "global"),
    ],
)
def test_keyed_preferences(sentence, key, value, scope):
    [candidate] = ConversationExtractor().extract_text(sentence, session_id="s", context=CTX)
    assert (candidate.key, candidate.value, candidate.scope) == (key, value, scope)


@pytest.mark.parametrize(
    "sentence,memory_type",
    [
        ("Never commit directly to main.", MemoryType.ANTI_PATTERN),
        ("From now on, always add a regression test before fixing a bug.", MemoryType.RULE),
        ("Remember that the staging database is reset every Sunday.", MemoryType.FACT),
        ("I'm new to Rust, so explain lifetimes.", MemoryType.SKILL),
        ("Let's go with Postgres because we need transactions.", MemoryType.DECISION),
        ("This project uses Celery for background jobs.", MemoryType.FACT),
    ],
)
def test_durable_statements_are_extracted(sentence, memory_type):
    [candidate] = ConversationExtractor().extract_text(sentence, session_id="s", context=CTX)
    assert candidate.memory_type == memory_type


@pytest.mark.parametrize(
    "sentence",
    [
        "Don't change that file.",
        "Can you use pytest here?",
        "Fix the bug on line 42 of utils.py.",
        "Never mind, it works now.",
        "Rename this variable to total.",
        "Traceback (most recent call last): File \"x.py\", line 3",
        "ok",
        "```python\nalways_use = 'tabs'\n```",
    ],
)
def test_transient_instructions_and_noise_are_ignored(sentence):
    assert ConversationExtractor().extract_text(sentence, session_id="s", context=CTX) == []


def test_assistant_statements_are_never_learned():
    transcript = Transcript(turns=[Turn("assistant", "From now on I will always use tabs.", 0)], session_id="s")
    assert ConversationExtractor().extract(transcript, CTX) == []


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------
def test_learning_from_a_session_and_reinforcing_across_sessions(container):
    session_1 = parse_transcript_text(claude_code_jsonl(
        user("Use pnpm instead of npm. Also, never commit directly to main."),
        assistant("Got it."),
    ))
    report = container.learning.learn_conversation(session_1, context=CTX)
    assert report.counts["created"] == 2

    replay = container.learning.learn_conversation(session_1, context=CTX)
    assert replay.counts["skipped"] == 2  # same session replayed: nothing double-counted

    session_2 = parse_transcript_text(claude_code_jsonl(
        {**user("Use pnpm instead of npm."), "sessionId": "sess-2"},
    ))
    again = container.learning.learn_conversation(session_2, context=CTX)
    assert [o.action for o in again.outcomes] == [IngestAction.REINFORCED]
    memory = container.memories.get_memory(again.outcomes[0].memory_id, track_access=False)
    assert memory.reinforcement_count == 2


def test_changing_your_mind_in_conversation(container):
    container.learning.learn_conversation(
        parse_transcript_text(claude_code_jsonl(user("I prefer black for formatting."))), context=CTX)
    report = container.learning.learn_conversation(
        parse_transcript_text(claude_code_jsonl({**user("Use ruff format instead of black."), "sessionId": "s2"})),
        context=CTX,
    )
    assert report.outcomes[0].action == IngestAction.SUPERSEDED
    active = container.memories.search_memories(
        SearchCriteria(keys=["python.formatter"], statuses=[MemoryStatus.ACTIVE])
    )
    assert [m.value for m in active.items] == ["ruff-format"]


# ----------------------------------------------------------------------
# LLM extractor (fake client; no network)
# ----------------------------------------------------------------------
class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def create(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def fake_client(payload=None, *, stop_reason="end_turn", error=None, raw_text=None):
    text = raw_text if raw_text is not None else json.dumps(payload or {"memories": []})
    response = SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                               content=[SimpleNamespace(type="text", text=text)])
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def memory_item(**overrides):
    item = {"kind": "preference", "title": "Prefers small pull requests",
            "content": "Keeps pull requests small and focused on one concern.", "scope": "global", "language": "",
            "tool": "", "confidence": 0.8, "evidence_quote": "please keep the PRs small"}
    item.update(overrides)
    return item


TRANSCRIPT = Transcript(
    turns=[Turn("user", "Ok so please keep the PRs small, one thing at a time. We use poetry everywhere.", 0),
           Turn("assistant", "Sure, I'll keep PRs small and I love tabs.", 1)],
    session_id="sess-9",
)


def test_llm_request_shape_uses_structured_output_and_fallbacks():
    client, messages = fake_client({"memories": []})
    LlmConversationExtractor(client, LlmExtractorConfig()).extract(TRANSCRIPT, CTX)
    request = messages.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["effort"] == "medium"
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tool_choice" not in request and "thinking" not in request
    assert "<DEVELOPER>" in request["messages"][0]["content"]


def test_llm_candidates_must_be_grounded_in_developer_words():
    client, _ = fake_client({"memories": [
        memory_item(),
        memory_item(title="Loves tabs", evidence_quote="I love tabs"),  # assistant's words: rejected
        memory_item(title="Invented", evidence_quote="always deploy on fridays"),  # not said: rejected
        memory_item(kind="tooling", title="Uses Poetry", tool="poetry", scope="language", language="python",
                    evidence_quote="We use poetry everywhere"),
    ]})
    candidates = LlmConversationExtractor(client).extract(TRANSCRIPT, CTX)
    assert [c.title for c in candidates] == ["Prefers small pull requests", "Uses Poetry"]
    poetry = candidates[1]
    assert (poetry.key, poetry.value, poetry.scope) == ("python.package_manager", "poetry", "lang:python")


def test_llm_secrets_are_redacted_before_sending():
    transcript = Transcript(turns=[Turn("user", "my token is ghp_" + "Ab1" * 14 + " keep PRs small", 0)],
                            session_id="s")
    client, messages = fake_client({"memories": []})
    LlmConversationExtractor(client).extract(transcript, CTX)
    assert "ghp_" not in messages.requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stop_reason": "refusal"},
        {"stop_reason": "max_tokens"},
        {"raw_text": "not json"},
        {"error": anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))},
        {"error": anthropic.RateLimitError("slow down", response=httpx.Response(
            429, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)},
    ],
)
def test_llm_failures_degrade_to_nothing(kwargs):
    client, _ = fake_client({"memories": [memory_item()]}, **kwargs)
    assert LlmConversationExtractor(client).extract(TRANSCRIPT, CTX) == []


def test_heuristic_and_llm_finding_the_same_sentence_count_once(container):
    transcript = Transcript(turns=[Turn("user", "Use pnpm instead of npm.", 0)], session_id="dup")
    client, _ = fake_client({"memories": [memory_item(
        kind="tooling", title="Uses pnpm", tool="pnpm", scope="global", evidence_quote="Use pnpm instead of npm")]})
    container.learning._llm_extractor_factory = lambda: LlmConversationExtractor(client)
    report = container.learning.learn_conversation(transcript, context=CTX, use_llm=True)
    assert report.counts["created"] == 1
    assert report.counts["reinforced"] == 0
