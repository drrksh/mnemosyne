"""
Rendering compiled context for a model.

Three formats: Markdown (default; what hooks inject and what rule files
contain), XML (some prompts prefer tag-delimited structure), and JSON
(for programmatic consumers). The Markdown and XML forms are wrapped in
a `<mnemosyne-context>` tag when injected into a conversation, which is
how the transcript parser recognises — and refuses to re-learn — text
Mnemosyne itself put there.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal, Optional

from mnemosyne.core.text import truncate
from mnemosyne.domain.entities import Memory
from mnemosyne.domain.enums import MemoryOrigin, MemoryType
from mnemosyne.domain.working_context import WorkingContext

RenderFormat = Literal["markdown", "xml", "json"]

#: Section order and membership. Rules come first: they are the
#: instructions most costly to violate.
SECTIONS: list[tuple[str, str, frozenset[MemoryType]]] = [
    ("rules", "Rules", frozenset({MemoryType.RULE, MemoryType.ANTI_PATTERN})),
    ("preferences", "Preferences", frozenset({MemoryType.PREFERENCE})),
    ("conventions", "Conventions", frozenset({MemoryType.CONVENTION, MemoryType.PATTERN})),
    ("tooling", "Tooling and commands", frozenset({MemoryType.TOOLING, MemoryType.PROCEDURE})),
    ("project", "Project knowledge", frozenset({
        MemoryType.ARCHITECTURE, MemoryType.FACT, MemoryType.DECISION, MemoryType.GOAL, MemoryType.OBSERVATION,
        MemoryType.RELATIONSHIP, MemoryType.CONVERSATION_SUMMARY, MemoryType.UNKNOWN,
    })),
    ("lessons", "Lessons learned", frozenset({MemoryType.FAILURE})),
    ("expertise", "About the developer", frozenset({MemoryType.SKILL})),
]
SECTION_TITLES = {key: title for key, title, _ in SECTIONS}
RELEVANT = "relevant"
SECTION_TITLES[RELEVANT] = "Relevant to this task"

CONTEXT_TAG = "mnemosyne-context"
INSTRUCTION = (
    "This is what the developer's memory says about how they work. Follow it unless they say otherwise in this "
    "conversation, and do not ask them to restate it."
)


def section_for(memory: Memory) -> str:
    for key, _, types in SECTIONS:
        if memory.memory_type in types:
            return key
    return "project"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~3.6 characters per token for mixed
    prose and identifiers). Used only for budgeting, never billing."""
    return max(1, math.ceil(len(text) / 3.6)) if text else 0


@dataclass
class ContextItem:
    memory: Memory
    section: str
    reason: str  # "relevant" | "profile" | "pinned"
    score: float
    detail: bool  # render with content, not just the title
    breakdown: Optional[dict[str, float]] = None

    def line(self) -> str:
        text = self.memory.title.rstrip(".")
        if self.detail:
            body = (self.memory.summary or self.memory.content or "").strip()
            if body and body.lower().rstrip(".") != text.lower():
                text = f"{text} — {truncate(' '.join(body.split()), 280)}"
        if self.memory.origin in (MemoryOrigin.INFERRED, MemoryOrigin.SYSTEM) and self.memory.confidence_score < 0.75:
            text += " (probably)"
        return text


@dataclass
class RenderPlan:
    context: WorkingContext
    task: Optional[str]
    items: list[ContextItem] = field(default_factory=list)


def _header(context: WorkingContext, intro: Optional[str] = None) -> str:
    where = []
    if context.repo_id:
        where.append(f"repository `{context.repo_name or context.repo_id}`")
    if context.languages:
        where.append(", ".join(context.languages))
    location = f" Working in {' — '.join(where)}." if where else ""
    return f"{intro or INSTRUCTION}{location}"


def grouped(items: list[ContextItem]) -> list[tuple[str, list[ContextItem]]]:
    order = [RELEVANT, *(key for key, _, _ in SECTIONS)]
    buckets: dict[str, list[ContextItem]] = {key: [] for key in order}
    for item in items:
        buckets[RELEVANT if item.reason == "relevant" else item.section].append(item)
    return [(key, buckets[key]) for key in order if buckets[key]]


def render_markdown(
    plan: RenderPlan, *, wrap: bool, title: str = "Developer memory (Mnemosyne)", intro: Optional[str] = None
) -> str:
    lines = [f"## {title}", _header(plan.context, intro), ""]
    for key, items in grouped(plan.items):
        lines.append(f"### {SECTION_TITLES[key]}")
        lines.extend(f"- {item.line()}" for item in items)
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    return f"<{CONTEXT_TAG}>\n{body}</{CONTEXT_TAG}>\n" if wrap else body


def render_xml(plan: RenderPlan, *, wrap: bool) -> str:
    attrs = []
    if plan.context.repo_id:
        attrs.append(f'repository="{escape(plan.context.repo_id)}"')
    if plan.context.languages:
        attrs.append(f'languages="{escape(",".join(plan.context.languages))}"')
    lines = [f"<developer_memory {' '.join(attrs)}>".replace(" >", ">"), f"  <instruction>{escape(INSTRUCTION)}"
             "</instruction>"]
    for key, items in grouped(plan.items):
        lines.append(f"  <{key}>")
        for item in items:
            m = item.memory
            lines.append(
                f'    <memory type="{m.memory_type.value}" scope="{escape(m.scope)}" '
                f'confidence="{m.confidence_score:.2f}">{escape(item.line())}</memory>'
            )
        lines.append(f"  </{key}>")
    lines.append("</developer_memory>")
    body = "\n".join(lines) + "\n"
    return f"<{CONTEXT_TAG}>\n{body}</{CONTEXT_TAG}>\n" if wrap else body


def render_json(plan: RenderPlan) -> str:
    payload: dict[str, Any] = {
        "instruction": INSTRUCTION,
        "repository": plan.context.repo_id,
        "languages": list(plan.context.languages),
        "sections": {
            key: [
                {
                    "id": str(item.memory.id),
                    "title": item.memory.title,
                    "content": item.memory.content if item.detail else None,
                    "type": item.memory.memory_type.value,
                    "scope": item.memory.scope,
                    "key": item.memory.key,
                    "value": item.memory.value,
                    "confidence": round(item.memory.confidence_score, 3),
                    "origin": item.memory.origin.value,
                    "reason": item.reason,
                }
                for item in items
            ]
            for key, items in grouped(plan.items)
        },
    }
    return json.dumps(payload, indent=2)


def render(plan: RenderPlan, fmt: RenderFormat, *, wrap: bool = False) -> str:
    if fmt == "xml":
        return render_xml(plan, wrap=wrap)
    if fmt == "json":
        return render_json(plan)
    return render_markdown(plan, wrap=wrap)
