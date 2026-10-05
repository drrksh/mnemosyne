"""
Optional LLM extraction of durable knowledge from a session transcript.

The rule-based extractor (`heuristics.py`) catches explicit statements
("from now on...", "use pnpm instead of npm"). A model also catches what
rules cannot: preferences implied by repeated corrections, decisions
whose rationale is spread over several messages, conventions described
in passing. It is off by default (`MNEMOSYNE_LLM_EXTRACTION=true` plus
the `mnemosyne[llm]` extra to enable) and runs out of band — after a
session ends — never in the prompt-latency path.

Safety properties, enforced in code rather than trusted to the prompt:

* **Grounding.** Every extracted memory must quote the developer's own
  words verbatim. A memory whose quote cannot be found in a *user* turn
  is discarded, so the model cannot invent preferences, and assistant
  output is never a source.
* **Redaction.** The transcript is redacted before it leaves the
  machine, and everything returned passes through ingestion's redaction
  again.
* **Degradation.** Any API failure, refusal or malformed response yields
  no candidates and a log line; heuristic extraction is unaffected.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from mnemosyne.core.redaction import redact
from mnemosyne.core.text import normalize_for_fingerprint, short_hash, truncate
from mnemosyne.core.vocabulary import match_tool_prefix
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.transcript import Transcript

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.config import Settings

logger = logging.getLogger("mnemosyne.learning.llm")

SOURCE = "conversation:llm"
_ECOSYSTEM_LANGUAGE = {"python": "python", "javascript": "javascript", "go": "go", "rust": "rust", "ruby": "ruby",
                       "jvm": "java", "php": "php", "dotnet": "csharp"}

KIND_TO_TYPE: dict[str, MemoryType] = {
    "preference": MemoryType.PREFERENCE,
    "rule": MemoryType.RULE,
    "anti_pattern": MemoryType.ANTI_PATTERN,
    "convention": MemoryType.CONVENTION,
    "pattern": MemoryType.PATTERN,
    "decision": MemoryType.DECISION,
    "fact": MemoryType.FACT,
    "skill": MemoryType.SKILL,
    "tooling": MemoryType.TOOLING,
    "procedure": MemoryType.PROCEDURE,
    "failure": MemoryType.FAILURE,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(KIND_TO_TYPE)},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "language", "repository"]},
                    "language": {"type": "string"},
                    "tool": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["kind", "title", "content", "scope", "language", "tool", "confidence",
                             "evidence_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You maintain the long-term memory of a software developer's AI coding assistant. From one coding \
session transcript, extract only knowledge about how this developer works that will still be true \
and useful in future, unrelated sessions — so the assistant never has to be told it again.

Extract:
- preferences and standing instructions ("keep answers short", "always add a regression test first")
- coding conventions and patterns the developer asks for or corrects the assistant towards
- tools they choose for a job (test framework, package manager, formatter...)
- decisions about their projects, with the rationale when given
- durable facts about their projects (what a service talks to, how it is deployed)
- their expertise level in a technology, when they reveal it
- lessons from failures that should not be repeated

Do not extract:
- one-off instructions about the current task ("rename this variable", "fix the test above")
- anything the assistant said; only the developer's own messages are evidence
- secrets, credentials, personal data about other people
- guesses: if the developer did not clearly express it, leave it out

For each memory:
- title: a short imperative or descriptive phrase (under 80 characters)
- content: one or two sentences, self-contained, written so it makes sense without the transcript
- scope: "global" if it applies everywhere, "language" if it applies to one programming language \
(set language, lowercase, e.g. "python"), "repository" if it is specific to the project being discussed
- tool: the tool's common name when the memory is about choosing a tool, otherwise ""
- confidence: 0.5 to 0.95 — how clearly the developer expressed it
- evidence_quote: an exact, verbatim excerpt (5 to 200 characters) from ONE developer message that \
supports the memory. Copy it character for character.

Return an empty list when nothing durable was said; that is the common case."""


@dataclass(frozen=True)
class LlmExtractorConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_transcript_chars: int = 60_000
    timeout_seconds: float = 60.0
    max_retries: int = 2
    max_tokens: int = 16000
    #: Server-side refusal fallback ("default" lets the API choose the
    #: model by refusal category). None disables it.
    fallbacks: Optional[str] = "default"


class LlmUnavailable(RuntimeError):
    """The optional LLM extractor cannot run (missing extra or credentials)."""


def build_client(settings: Settings) -> Any:
    """An Anthropic client, or LlmUnavailable. Credentials resolve the SDK's
    usual way (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, `ant auth login`
    profile...) unless MNEMOSYNE_ANTHROPIC_API_KEY is set explicitly."""
    try:
        import anthropic
    except ImportError as exc:
        raise LlmUnavailable("LLM extraction requires: pip install 'mnemosyne[llm]'") from exc
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_seconds, "max_retries": 2}
    if key:
        kwargs["api_key"] = key
    return anthropic.Anthropic(**kwargs)


def render_transcript(transcript: Transcript, max_chars: int) -> tuple[str, bool]:
    """Transcript as labeled turns, most recent first kept when it exceeds
    the budget. Returns (text, truncated)."""
    blocks: list[str] = []
    used = 0
    truncated = False
    for turn in reversed(transcript.turns):
        label = "DEVELOPER" if turn.role == "user" else "ASSISTANT"
        body = turn.text if turn.role == "user" else truncate(turn.text, 1500)
        block = f"<{label}>\n{redact(body).text}\n</{label}>"
        if used + len(block) > max_chars:
            truncated = True
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(reversed(blocks)), truncated


class LlmConversationExtractor:
    def __init__(self, client: Any, config: Optional[LlmExtractorConfig] = None) -> None:
        self._client = client
        self.config = config or LlmExtractorConfig()

    def extract(self, transcript: Transcript, context: Optional[WorkingContext] = None) -> list[CandidateMemory]:
        context = context or WorkingContext()
        user_text = normalize_for_fingerprint(" ".join(t.text for t in transcript.user_turns))
        if not user_text:
            return []
        rendered, truncated = render_transcript(transcript, self.config.max_transcript_chars)
        if truncated:
            logger.info("llm_transcript_truncated", extra={"kept_chars": len(rendered)})
        payload = self._call(rendered, context)
        if payload is None:
            return []
        session = transcript.session_id or "unknown"
        candidates: list[CandidateMemory] = []
        for item in payload.get("memories", []):
            candidate = self._to_candidate(item, user_text, session, context)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    def _call(self, rendered: str, context: WorkingContext) -> Optional[dict[str, Any]]:
        import anthropic

        where = f"Repository: {context.repo_id}" if context.repo_id else "Repository: unknown"
        if context.languages:
            where += f"\nLanguages: {', '.join(context.languages)}"
        request: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": f"{where}\n\n<transcript>\n{rendered}\n</transcript>"}],
            "output_config": {
                "effort": self.config.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        }
        if self.config.fallbacks:
            request["betas"] = ["server-side-fallback-2026-07-01"]
            request["fallbacks"] = self.config.fallbacks
        try:
            response = self._client.beta.messages.create(**request)
        except anthropic.AuthenticationError:
            logger.error("llm_extraction_auth_failed: check ANTHROPIC_API_KEY or `ant auth login`")
            return None
        except anthropic.PermissionDeniedError:
            logger.error("llm_extraction_permission_denied", extra={"model": self.config.model})
            return None
        except anthropic.BadRequestError as exc:
            logger.error("llm_extraction_bad_request", extra={"detail": str(exc)[:300]})
            return None
        except anthropic.RateLimitError:
            logger.warning("llm_extraction_rate_limited_after_retries")
            return None
        except anthropic.APIStatusError as exc:
            logger.warning("llm_extraction_api_error", extra={"status": exc.status_code})
            return None
        except anthropic.APIConnectionError:  # includes timeouts
            logger.warning("llm_extraction_connection_failed")
            return None

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            logger.warning("llm_extraction_refused", extra={"category": category})
            return None
        if response.stop_reason == "max_tokens":
            logger.warning("llm_extraction_truncated_output")
            return None
        text = next((block.text for block in response.content if getattr(block, "type", None) == "text"), None)
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("llm_extraction_invalid_json")
            return None
        return payload if isinstance(payload, dict) else None

    def _to_candidate(
        self, item: Any, user_text: str, session: str, context: WorkingContext
    ) -> Optional[CandidateMemory]:
        if not isinstance(item, dict):
            return None
        memory_type = KIND_TO_TYPE.get(str(item.get("kind", "")))
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        quote = str(item.get("evidence_quote", "")).strip()
        if memory_type is None or not title or not content or len(quote) < 5:
            return None
        normalized_quote = normalize_for_fingerprint(quote)
        if not normalized_quote or normalized_quote not in user_text:
            logger.info("llm_candidate_discarded_ungrounded", extra={"title": title[:80]})
            return None

        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = round(min(0.9, max(0.5, confidence)), 3)

        tool = match_tool_prefix(str(item.get("tool", ""))) if item.get("tool") else None
        key = value = None
        if tool is not None and tool.slot_key and memory_type in (
            MemoryType.TOOLING, MemoryType.PREFERENCE, MemoryType.DECISION, MemoryType.CONVENTION
        ):
            key, value = tool.slot_key, tool.name

        scope = "global"
        requested = item.get("scope")
        language = str(item.get("language", "")).strip().lower()
        if requested == "repository" and context.repo_id:
            scope = f"repo:{context.repo_id}"
        elif requested == "language" and language:
            scope = f"lang:{language}"
        elif tool is not None and tool.ecosystem in _ECOSYSTEM_LANGUAGE:
            scope = f"lang:{_ECOSYSTEM_LANGUAGE[tool.ecosystem]}"

        return CandidateMemory(
            title=truncate(title, 120),
            content=truncate(content, 600),
            memory_type=memory_type,
            origin=MemoryOrigin.CONVERSATION,
            confidence=confidence,
            importance=0.6,
            scope=scope,
            key=key,
            value=value,
            tags=["conversation", "llm"],
            source=SOURCE,
            metadata={"model": self.config.model},
            evidence=[
                EvidenceDraft(
                    kind=EvidenceKind.CONVERSATION,
                    source_ref=f"session:{session}#{short_hash(normalized_quote, 16)}",
                    excerpt=truncate(quote, 300),
                )
            ],
        )
