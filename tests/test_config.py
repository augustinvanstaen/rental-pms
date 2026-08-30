"""Config parsing tests. These touch no network and no real .env."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rental_pms.config import (  # noqa: E402
    DEFAULT_LABELS,
    ConfigError,
    _selection,
    _split_labels,
)


def test_selection_defaults_to_subject():
    assert _selection(None) == "subject"
    assert _selection("") == "subject"


def test_selection_accepts_both_modes_case_insensitively():
    assert _selection("subject") == "subject"
    assert _selection("labels") == "labels"
    assert _selection("  LABELS  ") == "labels"


def test_selection_rejects_unknown_mode():
    # A typo here would otherwise silently fall back to a default and change
    # which emails get processed.
    with pytest.raises(ConfigError):
        _selection("label")
    with pytest.raises(ConfigError):
        _selection("sender")


def test_labels_default_when_unset():
    assert _split_labels(None) == DEFAULT_LABELS
    assert _split_labels("") == DEFAULT_LABELS


def test_labels_split_and_trimmed():
    assert _split_labels("A, B ,C") == ("A", "B", "C")


def test_labels_ignore_empty_entries():
    assert _split_labels("A,,B,") == ("A", "B")
    # All-empty falls back rather than selecting nothing at all.
    assert _split_labels(" , , ") == DEFAULT_LABELS
