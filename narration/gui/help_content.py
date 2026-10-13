"""Help topics: loading, table of contents and search. No wx imports."""

from __future__ import annotations

import functools
import html
import re
from dataclasses import dataclass
from importlib import resources

# Topic files in reading order.
TOPIC_FILES = (
    "index.html", "getting-started.html", "pipeline.html", "script-review.html",
    "timeline.html", "voice.html", "quality.html", "project-files.html", "costs.html",
    "keyboard.html", "cli.html", "troubleshooting.html",
)
_H1 = re.compile(r"<h1>(.*?)</h1>", re.S | re.I)
_H2 = re.compile(r'<h2><a name="([\w-]+)"></a>(.*?)</h2>', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Section:
    anchor: str
    title: str
    text: str  # plain text, for search


@dataclass(frozen=True)
class Topic:
    file: str
    title: str
    sections: tuple[Section, ...]
    intro: str  # plain text before the first anchored section


def _plain(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


@functools.cache
def source(file: str) -> str:
    return (resources.files("narration.gui") / "help" / file).read_text("utf-8")


@functools.cache
def topics() -> tuple[Topic, ...]:
    out = []
    for file in TOPIC_FILES:
        body = source(file)
        m = _H1.search(body)
        title = _plain(m.group(1)) if m else file
        heads = list(_H2.finditer(body))
        sections = []
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            sections.append(Section(h.group(1), _plain(h.group(2)), _plain(body[h.end():end])))
        intro = _plain(body[:heads[0].start()] if heads else body)
        out.append(Topic(file, title, tuple(sections), intro))
    return tuple(out)


def topic(file: str) -> Topic | None:
    return next((t for t in topics() if t.file == file), None)


def parse_ref(ref: str) -> tuple[str, str | None]:
    """'quality#pronunciations' or 'quality.html#x' -> ('quality.html', 'x')."""
    file, _, anchor = ref.partition("#")
    file = file or "index.html"
    if not file.endswith(".html"):
        file += ".html"
    return file, anchor or None


@dataclass(frozen=True)
class Hit:
    file: str
    anchor: str | None
    title: str     # "Topic › Section"
    score: int


def search(query: str, limit: int = 30) -> list[Hit]:
    """Sections containing every query word, best matches first."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    hits = []
    for t in topics():
        candidates = [(None, t.title, t.intro)] + [(s.anchor, s.title, s.text)
                                                   for s in t.sections]
        for anchor, title, text in candidates:
            hay = f"{title} {text}".lower()
            if all(w in hay for w in words):
                score = sum(hay.count(w) for w in words) + 5 * sum(w in title.lower()
                                                                   for w in words)
                label = t.title if anchor is None else f"{t.title} › {title}"
                hits.append(Hit(t.file, anchor, label, score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
