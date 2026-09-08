"""
Small immutable value objects shared across the domain layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mnemosyne.domain.exceptions import ValidationError


class _UnsetType:
    """Sentinel distinguishing 'field omitted' from 'field set to a falsy value'.

    Used throughout MemoryUpdateData so PATCH semantics are unambiguous:
    a field left as UNSET means "the caller did not mention this field",
    which is different from a field explicitly set to None, "", or [].
    """

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _UnsetType()


@dataclass(frozen=True)
class UnitScore:
    """A float clamped to the closed interval [0.0, 1.0].

    This is a "smart constructor": validation happens once, here, at
    construction time. Callers extract `.value` to get a plain float
    back out for storage in entities/ORM rows — we do not carry the
    wrapper type through the rest of the system, to avoid `.value`
    unwrapping noise in every mapper and schema.
    """

    value: float
    label: str = "score"

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise ValidationError(
                f"{self.label} must be numeric, got {type(self.value).__name__}"
            )
        if not (0.0 <= float(self.value) <= 1.0):
            raise ValidationError(f"{self.label} must be between 0.0 and 1.0, got {self.value}")

    @classmethod
    def confidence(cls, value: float) -> "UnitScore":
        return cls(value=float(value), label="confidence_score")

    @classmethod
    def importance(cls, value: float) -> "UnitScore":
        return cls(value=float(value), label="importance_score")
