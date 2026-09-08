"""Unit tests for domain value objects."""
from __future__ import annotations

import pytest

from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.value_objects import UNSET, UnitScore, _UnsetType


def test_unit_score_accepts_boundary_values():
    assert UnitScore(0.0).value == 0.0
    assert UnitScore(1.0).value == 1.0


@pytest.mark.parametrize("bad", [-0.01, 1.01, -5, 100])
def test_unit_score_rejects_out_of_range(bad):
    with pytest.raises(ValidationError):
        UnitScore(bad)


def test_unit_score_rejects_non_numeric():
    with pytest.raises(ValidationError):
        UnitScore("high")  # type: ignore[arg-type]


def test_unit_score_rejects_bool_masquerading_as_numeric():
    # bool is technically a subclass of int in Python; explicitly guard
    # against `True`/`False` being accepted as 1.0/0.0 by accident.
    with pytest.raises(ValidationError):
        UnitScore(True)  # type: ignore[arg-type]


def test_unit_score_factories_label_errors_usefully():
    with pytest.raises(ValidationError, match="confidence_score"):
        UnitScore.confidence(5.0)
    with pytest.raises(ValidationError, match="importance_score"):
        UnitScore.importance(5.0)


def test_unset_sentinel_is_falsy_and_singleton():
    assert bool(UNSET) is False
    assert _UnsetType() is UNSET
