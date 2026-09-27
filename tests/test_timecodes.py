import math

import pytest

from vidaudcont.timecodes import format_cuts, keep_segments, parse


def test_sheet_format_roundtrip():
    text = "start-0:13, 3:10-3:17, 6:35-end"
    cuts = parse(text, duration=405.7)
    assert cuts == [(0.0, 13.0), (190.0, 197.0), (395.0, 405.7)]
    assert format_cuts(cuts, 405.7) == text


def test_good_and_empty():
    assert parse("good") == [] and parse("") == [] and parse("  GOOD ") == []
    assert format_cuts([], 100.0) == "good"


def test_lenient_input():
    assert parse("start – 0:13; 1:05:18-end\n2:00—2:10", duration=4000) == [(0.0, 13.0), (120.0, 130.0), (3918.0, 4000)]
    assert parse("6:35-end") == [(395.0, math.inf)]


def test_overlaps_are_merged():
    assert parse("0:10-0:20, 0:15-0:30, 0:30-0:40") == [(10.0, 40.0)]


@pytest.mark.parametrize("bad", ["0:20-0:10", "abc", "1:2:3:4-5", "0:10"])
def test_errors(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_keep_segments():
    assert keep_segments([(0, 13), (190, 197), (395, 405.7)], 405.7) == [(13, 190), (197, 395)]
    assert keep_segments([], 10.0) == [(0.0, 10.0)]
