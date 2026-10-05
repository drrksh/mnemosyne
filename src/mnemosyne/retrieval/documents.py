"""
How a memory is presented to the retrieval layer.

A memory is indexed as a handful of weighted *fields* (title, tags,
slot, summary, content, concepts) for lexical search, and as one
concatenated text for embedding. Keeping both derivations here means
the lexical index and the vector index can never disagree about what a
memory "says".
"""
from __future__ import annotations

from dataclasses import dataclass

from mnemosyne.core.text import short_hash, tokenize
from mnemosyne.core.vocabulary import concepts_for_text
from mnemosyne.domain.entities import Memory

#: Content beyond this many characters adds little to ranking and a lot
#: to index time; titles and summaries carry the signal.
MAX_INDEXED_CONTENT_CHARS = 4000

FIELD_BOOSTS: dict[str, float] = {
    "title": 2.5,
    "tags": 2.0,
    "slot": 2.0,
    "summary": 1.5,
    "content": 1.0,
    "concepts": 0.6,
}

CONCEPT_PREFIX = "§"


@dataclass(frozen=True)
class IndexedFields:
    title: list[str]
    tags: list[str]
    slot: list[str]
    summary: list[str]
    content: list[str]
    concepts: list[str]

    def items(self) -> list[tuple[str, list[str]]]:
        return [
            ("title", self.title),
            ("tags", self.tags),
            ("slot", self.slot),
            ("summary", self.summary),
            ("content", self.content),
            ("concepts", self.concepts),
        ]


def slot_text(memory: Memory) -> str:
    parts: list[str] = []
    if memory.key:
        parts.append(memory.key.replace(".", " ").replace("_", " "))
    if memory.value:
        parts.append(memory.value)
    return " ".join(parts)


def memory_fields(memory: Memory) -> IndexedFields:
    title = tokenize(memory.title)
    tags = tokenize(" ".join(memory.tags))
    slot = tokenize(slot_text(memory))
    summary = tokenize(memory.summary)
    content = tokenize(memory.content[:MAX_INDEXED_CONTENT_CHARS])
    concepts = [CONCEPT_PREFIX + c for c in memory_concepts(memory)]
    return IndexedFields(title, tags, slot, summary, content, concepts)


def memory_concepts(memory: Memory) -> list[str]:
    text = " ".join(
        [memory.title, " ".join(memory.tags), slot_text(memory), memory.summary,
         memory.content[:MAX_INDEXED_CONTENT_CHARS]]
    )
    return concepts_for_text(text)


def embedding_text(memory: Memory) -> str:
    # Tags are sorted so the text (and therefore its hash) does not depend
    # on whether the memory came from a write path or a database read.
    parts = [memory.title, memory.summary, memory.content[:MAX_INDEXED_CONTENT_CHARS], " ".join(sorted(memory.tags))]
    slot = slot_text(memory)
    if slot:
        parts.append(slot)
    return "\n".join(p for p in parts if p)


def embedding_text_hash(memory: Memory, model_id: str) -> str:
    return short_hash(model_id + "\n" + embedding_text(memory), 32)


def query_concepts(query: str) -> list[str]:
    return [CONCEPT_PREFIX + c for c in concepts_for_text(query)]


def query_terms(query: str) -> list[str]:
    return tokenize(query) + query_concepts(query)
