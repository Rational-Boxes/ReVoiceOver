"""Help pages: every link and every help button target must exist."""

import re
from pathlib import Path

from narration.gui import help_content

GUI = Path(__file__).parents[2] / "narration" / "gui"
_LINK = re.compile(r'href="([^"]+)"')


def _anchors(file: str) -> set[str]:
    return set(re.findall(r'<a name="([\w-]+)">', help_content.source(file)))


def _assert_target(ref: str, where: str) -> None:
    file, anchor = help_content.parse_ref(ref)
    assert file in help_content.TOPIC_FILES, f"{where}: unknown help page {file}"
    if anchor:
        assert anchor in _anchors(file), f"{where}: {file} has no anchor #{anchor}"


def test_every_topic_parses():
    topics = help_content.topics()
    assert len(topics) == len(help_content.TOPIC_FILES)
    assert all(t.title and t.title != t.file for t in topics)
    assert set((GUI / "help").glob("*.html")) == {GUI / "help" / f
                                                 for f in help_content.TOPIC_FILES}


def test_internal_links_resolve():
    for file in help_content.TOPIC_FILES:
        for href in _LINK.findall(help_content.source(file)):
            if href.startswith(("http", "mailto:")):
                continue
            _assert_target(file + href if href.startswith("#") else href, file)


def test_help_buttons_and_f1_targets_resolve():
    refs = set()
    for path in GUI.rglob("*.py"):
        text = path.read_text()
        refs |= {(m, path.name) for m in re.findall(r'help_button\([^,]+,\s*"([^"]+)"', text)}
        refs |= {(m, path.name) for m in re.findall(r'show_help\("([^"]+)"\)', text)}
    refs |= {(t, "main_frame.help_topic") for t in
             ("getting-started", "timeline", "pipeline", "script-review", "voice", "quality",
              "troubleshooting", "index")}
    assert len(refs) >= 15
    for ref, where in refs:
        _assert_target(ref, where)


def test_search():
    hits = help_content.search("pronunciation acronym")
    assert hits and hits[0].file == "quality.html" and hits[0].anchor == "pronunciations"
    assert help_content.search("") == []
    assert help_content.search("zzzz-not-there") == []


def test_parse_ref():
    assert help_content.parse_ref("quality#wer") == ("quality.html", "wer")
    assert help_content.parse_ref("voice.html") == ("voice.html", None)
    assert help_content.parse_ref("") == ("index.html", None)
