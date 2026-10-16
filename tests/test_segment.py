import json
from pathlib import Path

from narration.clients.asr import parse_timestamped
from narration.config import Segment as SegmentConfig
from narration.stages.segment import build_units, join_words
from narration.types import Word

FIXTURES = Path(__file__).parent / "fixtures"


def W(text: str, start: float, end: float) -> Word:
    return Word(text, start, end)


def seq(text: str, start: float = 0.0, step: float = 0.3) -> list[Word]:
    """Contiguous words, as Whisper returns them inside a segment."""
    out = []
    for i, t in enumerate(text.split()):
        out.append(W(t, round(start + i * step, 3), round(start + (i + 1) * step, 3)))
    return out


def test_join_words_reattaches_split_tokens():
    tokens = ["LEED", "5", ".0", "and", "instances", "/workspaces", "it", "'s", "(beta)", "ok."]
    words = [W(t, i, i + 1) for i, t in enumerate(tokens)]
    assert join_words(words) == "LEED 5.0 and instances/workspaces it's (beta) ok."


def test_splits_on_sentence_end_and_pause():
    words = seq("One two three. Four five six") + seq("seven eight nine", start=5.0)
    units = build_units(words, 10.0, SegmentConfig())
    assert [u.raw_text for u in units] == ["One two three.", "Four five six", "seven eight nine"]
    assert [u.id for u in units] == ["u0001", "u0002", "u0003"]
    assert units[1].slot_end == units[2].start == 5.0
    assert units[-1].slot_end == 10.0
    assert [(u.first_word, u.word_count) for u in units] == [(0, 3), (3, 3), (6, 3)]


def test_short_unit_merges_forward_when_pause_is_short():
    words = (seq("This is the new release of Acme Cloud.") + seq("date.", start=3.0)
             + seq("October 12th, 2026.", start=3.8))
    units = build_units(words, 6.0, SegmentConfig())
    assert [u.raw_text for u in units] == [
        "This is the new release of Acme Cloud.", "date. October 12th, 2026."]


def test_short_unit_kept_when_pause_is_long():
    words = seq("Okay.") + seq("Now open the settings panel.", start=4.0)
    units = build_units(words, 8.0, SegmentConfig())
    assert [u.raw_text for u in units] == ["Okay.", "Now open the settings panel."]


def test_short_tail_joins_previous():
    words = seq("Open the settings panel") + seq("now", start=1.5)
    units = build_units(words, 3.0, SegmentConfig(pause_split_s=0.2))
    assert [u.raw_text for u in units] == ["Open the settings panel now"]


def test_long_unit_split_prefers_pause_then_comma():
    text = " ".join(f"w{i}" for i in range(40))
    words = seq(text, step=0.6)  # 24 s with no punctuation or pauses
    words[19] = W("w19,", words[19].start, words[19].end)
    units = build_units(words, 30.0, SegmentConfig(max_unit_s=20))
    assert all(u.end - u.start <= 20 for u in units)
    assert units[0].raw_text.endswith("w19,")


def test_units_are_ordered_and_cover_all_words():
    raw = json.loads((FIXTURES / "whisper_timestamped_word.json").read_text())
    words = parse_timestamped(raw)
    units = build_units(words, 12.755, SegmentConfig())
    assert sum(u.word_count for u in units) == len(words)
    assert all(a.slot_end == b.start for a, b in zip(units, units[1:], strict=False))
    assert all(u.start < u.end <= u.slot_end for u in units)
    assert units[0].raw_text == "So, today I want to show you the deployment dashboard."


def test_empty():
    assert build_units([], 5.0, SegmentConfig()) == []
