"""
Findings: what a repository analyzer concluded, with the numbers behind it.

Analyzers report *counts*, and confidence is derived from them with the
Wilson lower bound, so "3 of 3 files use tabs" is reported with much
less confidence than "412 of 430 files use 4 spaces" — and neither is
reported at all below a minimum sample size. Every finding carries a
human-readable excerpt of its evidence, which is what `mnemosyne show`
and the API display when asked *why* Mnemosyne believes something.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from mnemosyne.domain.enums import EvidenceKind, MemoryType


def wilson_lower_bound(successes: float, total: float, z: float = 1.64) -> float:
    if total <= 0:
        return 0.0
    p = min(1.0, max(0.0, successes / total))
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


@dataclass
class Finding:
    analyzer: str
    title: str
    content: str
    memory_type: MemoryType
    confidence: float
    importance: float = 0.5
    key: Optional[str] = None
    value: Optional[str] = None
    evidence_kind: EvidenceKind = EvidenceKind.CODE
    excerpt: str = ""
    tags: list[str] = field(default_factory=list)
    samples: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def majority(counts: dict[str, int]) -> tuple[Optional[str], int, int]:
    """(winning label, its count, total) — ties broken alphabetically."""
    total = sum(counts.values())
    if not total:
        return None, 0, 0
    label, count = max(sorted(counts.items()), key=lambda item: item[1])
    return label, count, total


def proportion_confidence(count: int, total: int, *, min_total: int, min_bound: float = 0.65) -> Optional[float]:
    """Confidence for "most of the time" claims, or None when the sample is
    too small or the majority too weak to report."""
    if total < min_total:
        return None
    bound = wilson_lower_bound(count, total)
    if bound < min_bound:
        return None
    return round(min(0.95, bound), 3)


def pct(count: int, total: int) -> str:
    return f"{(100.0 * count / total):.0f}%" if total else "n/a"
