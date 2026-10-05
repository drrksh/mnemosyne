"""
Rule-based extraction of durable knowledge from conversations.

Works offline, costs nothing, and is predictable: every candidate can be
traced to the exact sentence and rule that produced it. It is tuned for
*precision*: a missed preference will be stated again; a wrongly learned
one pollutes every future session.

The central difficulty is separating standing preferences from one-off
instructions. "Never commit secrets" is durable; "don't change that
file" is about the current task. Deictic words in the first clause
("that", "this function", "now", "here") mark the latter and are
skipped, except when they point at the project itself ("in this repo").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from mnemosyne.core.text import normalize_whitespace, short_hash, truncate
from mnemosyne.core.vocabulary import Tool, category_label, match_tool_prefix
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.transcript import Transcript

SOURCE = "conversation:heuristic"

_ECOSYSTEM_LANGUAGE = {"python": "python", "javascript": "javascript", "go": "go", "rust": "rust", "ruby": "ruby",
                       "jvm": "java", "php": "php", "dotnet": "csharp"}

_CODE_FENCE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!;])\s+(?=[A-Z\"'(])")
_CODEY = re.compile(r"[{}();=<>\[\]]")
_LOGGY = re.compile(r"(Traceback|Error:|Exception:|\bat [\w.$]+\(|line \d+|File \"|^\s*\$ |npm ERR|warning:)")
_REPO_MARKER = re.compile(
    r"\b(?:in|for|on|across)\s+(?:this|our|the)\s+(?:repo|repository|project|codebase|code base|service|app|monorepo)\b"
    r"|\bhere\b(?=\s*(?:,|\.|$|we\b))",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(
    r"\b(?:that|these|those|it|now|today|yet|above|below|this time|for now|right now|"
    r"this (?:file|function|method|class|line|test|change|pr|bug|error|one|component|module|step|code|part))\b",
    re.IGNORECASE,
)
_LEADING_REPO = re.compile(
    r"^(?:in|for|on|across)\s+(?:this|our|the)\s+(?:repo|repository|project|codebase|code base|service|app|monorepo)"
    r"\s*[,:]?\s*",
    re.IGNORECASE,
)
#: Discourse markers that precede an otherwise standalone statement.
_LEADING_FILLER = re.compile(
    r"^(?:(?:also|and|and also|oh|btw|by the way|but|ok|okay|so|plus|one more thing|another thing|"
    r"actually also|right|well)\s*[,:]?\s+)+",
    re.IGNORECASE,
)
_LEADING_VERB = re.compile(r"^(?:to\s+)?(?:use|using|go with|stick with|switch to|prefer|choose|pick|adopt|run)\s+",
                           re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    memory_type: Optional[MemoryType]  # None = classify from the body
    origin: MemoryOrigin
    confidence: float
    importance: float
    negative: bool = False
    title_prefix: str = ""


def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


C, E = MemoryOrigin.CONVERSATION, MemoryOrigin.EXPLICIT
RULES: tuple[Rule, ...] = (
    Rule("remember", _r(r"^(?:please\s+)?(?:remember|note|keep in mind|fyi|for future reference|for the record|"
                        r"make a note)(?:\s+that)?\s*[:,-]?\s+(?P<body>.+)$"), None, E, 0.95, 0.75),
    Rule("standing", _r(r"^(?:from now on|going forward|in the future|moving forward|in general|as a rule|"
                        r"by default)\s*[,:]?\s+(?P<body>.+)$"), None, C, 0.9, 0.75),
    Rule("never", _r(r"^(?:please\s+)?(?:you should\s+|you must\s+)?(?:never|don't ever|do not ever|never ever)\s+"
                     r"(?!mind\b)(?P<body>.+)$"), MemoryType.ANTI_PATTERN, C, 0.85, 0.7, True, "Never "),
    Rule("always", _r(r"^(?:please\s+)?(?:you should\s+|you must\s+|make sure to\s+)?always\s+(?P<body>.+)$"),
         None, C, 0.85, 0.7, False, "Always "),
    Rule("avoid", _r(r"^(?:please\s+)?(?:avoid|stop|don't|do not)\s+(?P<body>(?:using|use|adding|add|writing|write|"
                     r"leaving|leave|putting|put|including|include|committing|commit|pushing|push|mocking|mock|"
                     r"hardcoding|hardcode|importing|import)\b.+)$"), MemoryType.ANTI_PATTERN, C, 0.7, 0.6, True,
         "Avoid "),
    Rule("prefer", _r(r"\bI\s+(?:really\s+|strongly\s+|generally\s+|usually\s+|much\s+)?(?:prefer|like|love|"
                      r"favou?r|tend to use|want you to use|'d rather use|would rather use)\s+(?P<body>.+)$"),
         MemoryType.PREFERENCE, C, 0.8, 0.65, False, "Prefers "),
    Rule("dislike", _r(r"\bI\s+(?:really\s+)?(?:don't like|do not like|hate|dislike|can't stand|cannot stand)\s+"
                       r"(?P<body>.+)$"), MemoryType.ANTI_PATTERN, C, 0.8, 0.6, True, "Dislikes "),
    Rule("we_use", _r(r"^(?:we|I)\s+(?:always\s+|usually\s+|generally\s+|only\s+)?(?:use|go with|stick with|"
                      r"standardi[sz]ed on|rely on)\s+(?P<body>.+)$"), MemoryType.PREFERENCE, C, 0.75, 0.6),
    Rule("correction", _r(r"^(?:no|nope|actually|wrong|not quite|that's wrong)\s*[,.!:]?\s+(?:we\s+|I\s+|you should\s+|"
                          r"please\s+)?(?:use|prefer|want)\s+(?P<body>.+)$"), MemoryType.PREFERENCE, C, 0.75, 0.6),
)

_INSTEAD = _r(r"\b(?:use|prefer|want)\s+(?P<new>[\w.+#@/-]+(?:\s[\w.+#@/-]+)?)\s+(?:instead of|rather than|over)\s+"
              r"(?P<old>[\w.+#@/-]+(?:\s[\w.+#@/-]+)?)")
_DECISION = _r(r"\b(?:let's|let us|we(?:'ll| will)|I(?:'ve| have)? decided to|we(?:'ve| have)? decided to|"
               r"decided to|we're going to|we are going to)\s+(?:go with|use|switch to|stick with|adopt|move to|"
               r"migrate to)\s+(?P<what>.+?)\s+(?:because|since|so that|as)\s+(?P<why>.+)$")
_SKILL_NEW = _r(r"\bI(?:'m| am)\s+(?:a\s+)?(?:new to|learning|not (?:very |that )?familiar with|unfamiliar with|"
                r"a beginner (?:in|with|at)|just (?:getting )?started with|still learning)\s+(?P<topic>[^,.;]+)")
_SKILL_EXPERT = _r(r"\bI(?:'m| am)\s+(?:an?\s+|very\s+)?(?:expert|experienced|senior|fluent|proficient|"
                   r"comfortable|very comfortable)\s+(?:in|with|at)\s+(?P<topic>[^,.;]+)")
_SKILL_YEARS = _r(r"\bI(?:'ve| have)\s+(?:been\s+)?(?:using|writing|working with|doing)\s+(?P<topic>[^,.;]+?)\s+"
                  r"for\s+(?P<years>\d+|many|several|a few|over \d+)\s+years")
_PROJECT_FACT = _r(r"\b(?:this|our|the)\s+(?:project|repo|repository|app|service|codebase|api|backend|frontend)\s+"
                   r"(?:uses|is built (?:on|with)|runs on|depends on|is written in|talks to|is deployed (?:on|to))\s+"
                   r"(?P<body>.+)$")
_TERSE = _r(r"\b(?:be|keep (?:it|answers|responses|replies|explanations))\s+(?:more\s+|very\s+)?"
            r"(?:concise|brief|short|terse|succinct)\b|\b(?:shorter|less verbose|more concise) (?:answers|responses|"
            r"replies|explanations)\b|\btoo (?:verbose|long|wordy)\b|\bjust (?:give|show) me the code\b|"
            r"\bskip the (?:explanations?|preamble)\b")
_DETAILED = _r(r"\b(?:more detailed|more thorough|longer) (?:answers|responses|explanations)\b|"
               r"\bexplain (?:things )?(?:in more detail|step by step)\b")
_NO_EMOJI = _r(r"\b(?:no|stop using|don't use|do not use|without)\s+emojis?\b")
_LANGUAGE_PREF = _r(r"\b(?:respond|reply|answer|write)\s+(?:to me\s+)?in\s+(?P<lang>english|german|french|spanish|"
                    r"portuguese|italian|dutch|japanese|chinese|korean|hindi|russian|polish|turkish)\b")


@dataclass
class _Match:
    rule: str
    title: str
    body: str
    memory_type: MemoryType
    origin: MemoryOrigin
    confidence: float
    importance: float
    negative: bool = False
    key: Optional[str] = None
    value: Optional[str] = None
    scope_hint: Optional[str] = None  # "repo" | "global" | None (infer)
    tool: Optional[Tool] = None
    rationale: Optional[str] = None


def split_sentences(text: str) -> list[str]:
    text = _CODE_FENCE.sub(" ", text)
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•>").strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT.split(line):
            part = part.strip().replace("`", "")
            if part:
                sentences.append(part)
    return sentences


def _usable(sentence: str) -> bool:
    words = sentence.split()
    if not 3 <= len(words) <= 60:
        return False
    if sentence.rstrip().endswith("?"):
        return False
    if _LOGGY.search(sentence):
        return False
    return len(_CODEY.findall(sentence)) / max(1, len(sentence)) <= 0.06


def _clean_body(body: str) -> str:
    body = normalize_whitespace(body).strip(" .!;:,")
    return re.sub(r"\s*(?:please|thanks|thank you|thx)\s*$", "", body, flags=re.IGNORECASE).strip(" .!;:,")


def _first_clause(body: str) -> str:
    return re.split(r"[,;]|\bbecause\b|\bsince\b|\bso that\b|\bunless\b", body, maxsplit=1)[0]


def _is_transient(body: str) -> bool:
    clause = _first_clause(body)
    clause = _REPO_MARKER.sub(" ", clause)
    return bool(_TRANSIENT.search(clause))


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _classify(body: str, default: MemoryType) -> MemoryType:
    lowered = body.lower()
    if re.search(r"\b(?:never|must not|mustn't|do not|don't)\b", lowered):
        return MemoryType.RULE
    if re.search(r"\bbecause\b|\bdecided\b", lowered):
        return MemoryType.DECISION
    if re.match(r"(?:we|our|this project|the project)\b", lowered):
        return MemoryType.FACT
    if re.match(r"(?:i|my)\b", lowered):
        return MemoryType.PREFERENCE
    return default


def _match_sentence(sentence: str) -> Optional[_Match]:
    """Match one sentence; a leading "In this repo, ..." scopes the result
    to the repository and is stripped before the rules run."""
    sentence = _LEADING_FILLER.sub("", sentence) or sentence
    stripped = _LEADING_REPO.sub("", sentence)
    match = _match_core(stripped[:1].upper() + stripped[1:] if stripped != sentence else sentence)
    if match is not None and stripped != sentence and match.scope_hint != "global":
        match.scope_hint = "repo"
    return match


def _match_core(sentence: str) -> Optional[_Match]:
    # Communication preferences are global and keyed (one active value).
    if _NO_EMOJI.search(sentence):
        return _Match("emoji", "No emojis", sentence, MemoryType.PREFERENCE, C, 0.85, 0.7, True,
                      key="communication.emoji", value="none", scope_hint="global")
    if _TERSE.search(sentence):
        return _Match("verbosity", "Prefers terse answers that lead with the code", sentence, MemoryType.PREFERENCE, C,
                      0.85, 0.8, key="communication.verbosity", value="terse", scope_hint="global")
    if _DETAILED.search(sentence) and re.search(r"\b(?:always|in general|prefer|from now on|please)\b", sentence,
                                                re.IGNORECASE):
        return _Match("verbosity", "Prefers detailed, step-by-step explanations", sentence, MemoryType.PREFERENCE, C,
                      0.8, 0.75, key="communication.verbosity", value="detailed", scope_hint="global")
    if match := _LANGUAGE_PREF.search(sentence):
        lang = match.group("lang").capitalize()
        return _Match("language", f"Respond in {lang}", sentence, MemoryType.PREFERENCE, C, 0.85, 0.8,
                      key="communication.language", value=lang.lower(), scope_hint="global")

    if match := _SKILL_NEW.search(sentence):
        topic = _clean_body(match.group("topic"))
        return _Match("skill", f"New to {topic}", sentence, MemoryType.SKILL, C, 0.85, 0.65,
                      key=f"skill.{short_hash(topic.lower(), 8)}", value="beginner", scope_hint="global")
    if match := _SKILL_YEARS.search(sentence):
        topic, years = _clean_body(match.group("topic")), match.group("years")
        return _Match("skill", f"Experienced with {topic} ({years} years)", sentence, MemoryType.SKILL, C, 0.85, 0.6,
                      key=f"skill.{short_hash(topic.lower(), 8)}", value="experienced", scope_hint="global")
    if match := _SKILL_EXPERT.search(sentence):
        topic = _clean_body(match.group("topic"))
        return _Match("skill", f"Experienced with {topic}", sentence, MemoryType.SKILL, C, 0.8, 0.6,
                      key=f"skill.{short_hash(topic.lower(), 8)}", value="experienced", scope_hint="global")

    if match := _INSTEAD.search(sentence):
        new_tool = match_tool_prefix(match.group("new"))
        old_tool = match_tool_prefix(match.group("old"))
        if new_tool and new_tool.slot_key:
            title = f"Prefers {new_tool.label} over {old_tool.label if old_tool else match.group('old')}"
            return _Match("instead", title, sentence, MemoryType.TOOLING, C, 0.8, 0.65, key=new_tool.slot_key,
                          value=new_tool.name, tool=new_tool)
        body = _clean_body(match.group(0))
        if not _is_transient(body):
            return _Match("instead", _capitalize(body), sentence, MemoryType.PREFERENCE, C, 0.75, 0.6)

    if match := _DECISION.search(sentence):
        what, why = _clean_body(match.group("what")), _clean_body(match.group("why"))
        if not _is_transient(what):
            tool = match_tool_prefix(what)
            return _Match("decision", f"Decision: {what}", f"{sentence}", MemoryType.DECISION, C, 0.75, 0.65,
                          key=tool.slot_key if tool else None, value=tool.name if tool and tool.slot_key else None,
                          tool=tool, rationale=why)

    if match := _PROJECT_FACT.search(sentence):
        body = _clean_body(match.group("body"))
        if not _is_transient(body):
            return _Match("project_fact", f"Project uses {body}", sentence, MemoryType.FACT, C, 0.8, 0.6,
                          scope_hint="repo")

    for rule in RULES:
        match = rule.pattern.search(sentence)
        if not match:
            continue
        body = _clean_body(match.group("body"))
        names_tool = match_tool_prefix(_LEADING_VERB.sub("", body)) is not None
        min_words = 1 if rule.name in ("prefer", "dislike") or names_tool else 2
        if len(body.split()) < min_words or _is_transient(body):
            return None
        if rule.name == "remember" and body.lower().startswith("to "):
            body = body[3:]  # "remember to run tests" is a standing instruction
            memory_type = MemoryType.RULE
        else:
            default = {"standing": MemoryType.RULE, "remember": MemoryType.FACT}.get(rule.name, MemoryType.PREFERENCE)
            memory_type = rule.memory_type or _classify(body, default)
        result = _Match(rule.name, "", body, memory_type, rule.origin, rule.confidence, rule.importance, rule.negative)
        object_phrase = _LEADING_VERB.sub("", body)
        tool = match_tool_prefix(object_phrase) if _LEADING_VERB.match(body) or rule.name in (
            "prefer", "we_use", "correction") else None
        if tool and tool.slot_key and not rule.negative:
            result.key, result.value, result.tool = tool.slot_key, tool.name, tool
            result.memory_type = MemoryType.TOOLING
            result.title = f"Prefers {tool.label} ({category_label(tool.category)})"
        else:
            result.title = truncate(rule.title_prefix + (body if rule.title_prefix else _capitalize(body)), 110)
            if rule.title_prefix and rule.name in ("never", "always", "avoid"):
                result.title = rule.title_prefix + truncate(body, 100)
        if rule.name == "correction":
            result.scope_hint = "repo"
        return result
    return None


class ConversationExtractor:
    """Turns a transcript into candidate memories (user turns only)."""

    def extract(self, transcript: Transcript, context: Optional[WorkingContext] = None) -> list[CandidateMemory]:
        context = context or WorkingContext()
        session = transcript.session_id or "unknown"
        seen: set[str] = set()
        candidates: list[CandidateMemory] = []
        for turn in transcript.user_turns:
            for sentence in split_sentences(turn.text):
                if not _usable(sentence):
                    continue
                normalized = normalize_whitespace(sentence.lower())
                if normalized in seen:
                    continue
                match = _match_sentence(sentence)
                if match is None:
                    continue
                seen.add(normalized)
                candidates.append(self._to_candidate(match, sentence, normalized, session, context))
        return candidates

    def extract_text(self, text: str, *, session_id: str, context: Optional[WorkingContext] = None) -> list[
            CandidateMemory]:
        from mnemosyne.learning.conversation.transcript import Turn, clean_user_text

        transcript = Transcript(turns=[Turn("user", clean_user_text(text), 0)], session_id=session_id)
        return self.extract(transcript, context)

    @staticmethod
    def _scope(match: _Match, sentence: str, context: WorkingContext) -> str:
        if match.scope_hint == "global":
            return "global"
        if (match.scope_hint == "repo" or _REPO_MARKER.search(sentence)) and context.repo_id:
            return f"repo:{context.repo_id}"
        if match.tool and match.tool.ecosystem in _ECOSYSTEM_LANGUAGE:
            return f"lang:{_ECOSYSTEM_LANGUAGE[match.tool.ecosystem]}"
        return "global"

    def _to_candidate(
        self, match: _Match, sentence: str, normalized: str, session: str, context: WorkingContext
    ) -> CandidateMemory:
        content = truncate(normalize_whitespace(sentence), 400)
        return CandidateMemory(
            title=truncate(match.title, 120),
            content=content,
            memory_type=match.memory_type,
            origin=match.origin,
            confidence=match.confidence,
            importance=match.importance,
            scope=self._scope(match, sentence, context),
            key=match.key,
            value=match.value,
            tags=[t for t in ("conversation", match.rule) if t],
            source=SOURCE,
            metadata={"rule": match.rule, **({"rationale": match.rationale} if match.rationale else {})},
            evidence=[
                EvidenceDraft(
                    kind=EvidenceKind.EXPLICIT if match.origin == MemoryOrigin.EXPLICIT else EvidenceKind.CONVERSATION,
                    source_ref=f"session:{session}#{short_hash(normalized, 16)}",
                    excerpt=content,
                )
            ],
        )
