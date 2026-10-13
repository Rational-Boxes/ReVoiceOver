"""[2] segment: group words into narration units anchored to the video timeline.

A unit ends at a pause longer than ``pause_split_s`` or at sentence-final
punctuation. Very short units (``min_unit_words``) merge into the next one when
the pause between them is short, units longer than ``max_unit_s`` are split at
their best internal boundary, and each unit's slot runs to the next unit's
start (or the end of the video).
"""

from __future__ import annotations

from narration import artifacts
from narration.config import Segment as SegmentConfig
from narration.context import RunContext
from narration.types import Unit, Word

STAGE = "segment"
SENTENCE_END = (".", "?", "!", "…")
SOFT_BREAK = (",", ";", ":", "—", "–")
# Tokens Whisper splits off that belong to the previous word: "5" ".0", "a" "/b", "it" "'s"
ATTACH_LEFT = (".", ",", ";", ":", "!", "?", ")", "]", "}", "/", "%", "'", "’", "…", "-")
ATTACH_RIGHT = ("/", "(", "[", "{", "-", "$")


def join_words(words: list[Word]) -> str:
    out = ""
    for w in words:
        text = w.text.strip()
        if not text:
            continue
        if out and not text.startswith(ATTACH_LEFT) and not out.endswith(ATTACH_RIGHT):
            out += " "
        out += text
    return out


def _ends_sentence(word: Word) -> bool:
    return word.text.rstrip("\"'”’)").endswith(SENTENCE_END)


def _split_points(words: list[Word], pause_split_s: float) -> list[int]:
    """Indices where a new unit starts (always includes 0)."""
    starts = [0]
    for i in range(1, len(words)):
        gap = words[i].start - words[i - 1].end
        if gap > pause_split_s or _ends_sentence(words[i - 1]):
            starts.append(i)
    return starts


def _merge_short(groups: list[list[Word]], cfg: SegmentConfig) -> list[list[Word]]:
    out: list[list[Word]] = []
    pending: list[Word] = []
    for group in groups:
        if pending:
            gap = group[0].start - pending[-1].end
            fits = group[-1].end - pending[0].start <= cfg.max_unit_s
            if gap <= cfg.merge_gap_s and fits:
                group = pending + group
            else:
                out.append(pending)
            pending = []
        if len(group) < cfg.min_unit_words:
            pending = group
        else:
            out.append(group)
    if pending:
        # A short tail joins the previous unit when that is close enough.
        if out and pending[0].start - out[-1][-1].end <= cfg.merge_gap_s \
                and pending[-1].end - out[-1][0].start <= cfg.max_unit_s:
            out[-1] = out[-1] + pending
        else:
            out.append(pending)
    return out


def _split_long(group: list[Word], max_unit_s: float) -> list[list[Word]]:
    """Recursively split at the best boundary: big pauses, then commas, near the middle."""
    if len(group) < 2 or group[-1].end - group[0].start <= max_unit_s:
        return [group]
    start, end = group[0].start, group[-1].end
    mid = (start + end) / 2

    def score(k: int) -> float:
        gap = group[k].start - group[k - 1].end
        soft = 0.5 if group[k - 1].text.endswith(SOFT_BREAK) else 0.0
        centred = 1.0 - abs(group[k].start - mid) / (end - start)
        return gap * 2 + soft + centred

    k = max(range(1, len(group)), key=score)
    return _split_long(group[:k], max_unit_s) + _split_long(group[k:], max_unit_s)


def build_units(words: list[Word], duration: float, cfg: SegmentConfig) -> list[Unit]:
    if not words:
        return []
    starts = _split_points(words, cfg.pause_split_s)
    groups = [words[a:b] for a, b in zip(starts, [*starts[1:], len(words)], strict=True)]
    groups = _merge_short(groups, cfg)
    groups = [part for g in groups for part in _split_long(g, cfg.max_unit_s)]

    index = {id(w): i for i, w in enumerate(words)}
    units: list[Unit] = []
    for n, group in enumerate(groups):
        units.append(Unit(
            id=f"u{n + 1:04d}",
            start=group[0].start,
            end=group[-1].end,
            slot_end=0.0,  # filled below
            raw_text=join_words(group),
            first_word=index[id(group[0])],
            word_count=len(group),
        ))
    for unit, nxt in zip(units, [*units[1:], None], strict=True):
        unit.slot_end = nxt.start if nxt else max(duration, unit.end)
    return units


def run(ctx: RunContext) -> None:
    words = [Word.from_dict(w) for w in artifacts.read_json(ctx.path("words.json"))["words"]]
    probe = artifacts.read_json(ctx.path("probe.json"))
    duration = float(probe["format"]["duration"])
    units = build_units(words, duration, ctx.config.segment)
    artifacts.write_json(ctx.path("units.json"), [u.to_dict() for u in units])
    if units:
        lengths = sorted(u.end - u.start for u in units)
        ctx.info(f"{len(units)} units from {len(words)} words; "
                 f"median {lengths[len(lengths) // 2]:.1f}s, longest {lengths[-1]:.1f}s")
    else:
        ctx.warn("no words to segment; the narration is empty")
