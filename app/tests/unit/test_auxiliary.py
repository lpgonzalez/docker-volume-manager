"""Unit tests for operations.auxiliary_methods."""

from __future__ import annotations

import re

from operations.auxiliary_methods import get_formatted_time


def test_default_format_matches_iso_like_pattern():
    result = get_formatted_time()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", result)


def test_custom_format_is_honoured():
    result = get_formatted_time("%Y%m%d_%H%M")
    assert re.match(r"^\d{8}_\d{4}$", result)


def test_format_handles_literal_text():
    result = get_formatted_time("year=%Y")
    assert re.match(r"^year=\d{4}$", result)
