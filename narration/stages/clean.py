"""[3] clean: LLM rewrite of each narration unit, validated id-for-id.

Writes script.json (units with clean_text + review flags) and script.md (a
raw-vs-clean word diff for reading). Units marked ``edited`` in an existing
script.json keep their hand-written text when the stage is re-run.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
from importlib import resources
from typing import Any

from narration import artifacts
from narration.cache import FileCache, make_key
from narration.clients.deepinfra import DeepInfraClient
from narration.clients.llm import ChatLLM, LLMResponseError
from narration.config import read_glossary
from narration.context import RunContext
from narration.types import Unit

STAGE = "clean"
RATIO_MIN, RATIO_MAX = 0.4, 1.1
FILLERS = frozenset({"um", "uh", "uhm", "umm", "erm", "er", "ah", "eh", "hmm", "mm", "mhm",
                     "uh-huh", "okay", "ok", "so", "like", "well", "yeah", "right"})
_WORD = re.compile(r"[\w'’.-]+")


class CleanValidationError(ValueError):
    pass


def system_prompt() -> str:
    return (resources.files("narration") / "prompts" / "clean_system.md").read_text("utf-8")


def normalize(text: str) -> list[str]:
    return [w.strip(".-").lower() for w in _WORD.findall(text) if w.strip(".-")]


def is_pure_filler(text: str) -> bool:
    words = normalize(text)
    return not words or all(w.strip("'’") in FILLERS for w in words)


def has_fillers(text: str) -> bool:
    return any(w in FILLERS - {"so", "like", "well", "right", "okay", "ok", "yeah"}
               for w in normalize(text))


def validate_batch(batch: list[Unit], reply: Any) -> dict[str, str]:
    """Map id -> cleaned text, or raise if the reply breaks the id contract."""
    items = reply.get("units") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise CleanValidationError('reply has no "units" list')
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise CleanValidationError(f"malformed unit in reply: {item!r:.80}")
        out[item["id"]] = str(item.get("text") or "").strip()
    expected = [u.id for u in batch]
    if sorted(out) != sorted(expected) or len(items) != len(expected):
        missing = sorted(set(expected) - set(out))
        extra = sorted(set(out) - set(expected))
        raise CleanValidationError(f"id mismatch: missing {missing}, unexpected {extra}")
    return out


def flags_for(raw: str, clean: str) -> list[str]:
    if not clean:
        # Dropping pure filler is expected; dropping real words needs a human look.
        return ["dropped"] if is_pure_filler(raw) else ["dropped-content"]
    ratio = len(clean) / max(1, len(raw))
    if ratio < RATIO_MIN:
        return ["short"]
    if ratio > RATIO_MAX:
        return ["long"]
    return []


def apply_clean(unit: Unit, clean: str) -> None:
    unit.clean_text = unit.llm_text = clean
    unit.edited = False
    unit.flags = flags_for(unit.raw_text, clean)
    unit.keep_original = normalize(clean) == normalize(unit.raw_text) \
        and not has_fillers(unit.raw_text)


def word_diff_markdown(raw: str, clean: str) -> str:
    """Raw text with deletions as ~~struck~~ and insertions in **bold**.

    Words compare case- and punctuation-insensitively; unchanged spans show
    the cleaned spelling.
    """
    a, b = raw.split(), clean.split()

    def key(word: str) -> str:
        return word.lower().strip(".,;:!?\"'“”‘’()…")

    matcher = difflib.SequenceMatcher(a=[key(w) for w in a], b=[key(w) for w in b],
                                      autojunk=False)
    parts: list[str] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("delete", "replace"):
            parts.append("~~" + " ".join(a[i1:i2]) + "~~")
        if op in ("insert", "replace"):
            parts.append("**" + " ".join(b[j1:j2]) + "**")
        if op == "equal":
            parts.append(" ".join(b[j1:j2]))
    return " ".join(parts)


def script_markdown(units: list[Unit], video_name: str) -> str:
    lines = [f"# Script review: {video_name}", "",
             "Edit `clean_text` in script.json (or use the GUI), then continue from `voice`.",
             "Deletions are ~~struck~~, insertions are **bold**.", ""]
    for u in units:
        minutes, seconds = divmod(u.start, 60)
        tags = " ".join(f"`{f}`" for f in [*u.flags, *(["edited"] if u.edited else [])])
        lines.append(f"### {u.id} · {int(minutes)}:{seconds:04.1f} · slot {u.slot:.1f}s {tags}")
        lines.append("")
        lines.append(word_diff_markdown(u.raw_text, u.clean_text or "") or "_(dropped)_")
        lines.append("")
    return "\n".join(lines)


def _previous_edits(ctx: RunContext) -> dict[str, Unit]:
    try:
        data = artifacts.read_json(ctx.path("script.json"))
    except (FileNotFoundError, artifacts.SchemaError):
        return {}
    return {u.id: u for u in (Unit.from_dict(d) for d in data["units"]) if u.edited}


async def _clean_batch(ctx: RunContext, llm: ChatLLM, cache: FileCache, system: str,
                       glossary: list[str], batch: list[Unit]) -> tuple[dict[str, str], float]:
    user = json.dumps({"glossary": glossary,
                       "units": [{"id": u.id, "text": u.raw_text} for u in batch]},
                      ensure_ascii=False)
    key = make_key(llm.model, llm.temperature, system, user)
    if (hit := cache.get_json(key)) is not None:
        return validate_batch(batch, hit), 0.0
    cost = 0.0
    for attempt in (1, 2):
        ctx.check_cancelled()
        try:
            result = await llm.complete_json(system, user)
            cost += result.cost
            cleaned = validate_batch(batch, result.data)
        except (LLMResponseError, CleanValidationError) as exc:
            if attempt == 2:
                raise CleanValidationError(
                    f"batch {batch[0].id}–{batch[-1].id} failed twice: {exc}") from None
            ctx.warn(f"batch {batch[0].id}–{batch[-1].id}: {exc}; retrying once")
            continue
        cache.put_json(key, result.data)
        return cleaned, cost
    raise AssertionError("unreachable")


async def run(ctx: RunContext) -> None:
    cfg = ctx.config
    units = [Unit.from_dict(d) for d in artifacts.read_json(ctx.path("units.json"))]
    edits = _previous_edits(ctx)
    keep = {uid for uid, u in edits.items()
            if any(x.id == uid and x.raw_text == u.raw_text for x in units)}
    # Batch boundaries never depend on edits, so unchanged batches stay cache hits;
    # only batches made entirely of hand-edited units are skipped.
    size = cfg.clean.batch_size
    batches = [b for b in (units[i:i + size] for i in range(0, len(units), size))
               if any(u.id not in keep for u in b)]
    todo = [u for b in batches for u in b if u.id not in keep]
    system = system_prompt()
    glossary = read_glossary(ctx.video, cfg)
    ctx.info(f"cleaning {len(todo)} units in {len(batches)} batch(es) with {cfg.models.llm}"
             + (f"; keeping {len(keep)} hand-edited unit(s)" if keep else "")
             + (f"; glossary of {len(glossary)} terms" if glossary else ""))

    ctx.reporter.set_total(STAGE, len(batches))
    cache = FileCache(ctx.work_dir, "llm")
    cleaned: dict[str, str] = {}
    total_cost = 0.0
    async with DeepInfraClient.from_context(ctx) as client:
        llm = ChatLLM(client, cfg.models.llm, temperature=cfg.clean.temperature)

        async def one(batch: list[Unit]) -> float:
            result, cost = await _clean_batch(ctx, llm, cache, system, glossary, batch)
            cleaned.update(result)
            ctx.reporter.advance(STAGE, 1, f"{batch[-1].id}")
            return cost

        total_cost = sum(await asyncio.gather(*(one(b) for b in batches)))

    for unit in units:
        if unit.id in keep:
            prev = edits[unit.id]
            unit.clean_text, unit.edited = prev.clean_text, True
            unit.llm_text = cleaned.get(unit.id, prev.llm_text)
            unit.flags = flags_for(unit.raw_text, unit.clean_text or "")
            unit.keep_original = prev.keep_original
        else:
            apply_clean(unit, cleaned[unit.id])

    prompt_sha = hashlib.sha256(system.encode()).hexdigest()[:12]
    artifacts.write_json(ctx.path("script.json"), {
        "model": cfg.models.llm, "prompt_sha": prompt_sha,
        "units": [u.to_dict() for u in units],
    })
    artifacts.atomic_write_text(ctx.path("script.md"), script_markdown(units, ctx.video.name))
    flagged = sum(1 for u in units if u.flags)
    unchanged = sum(1 for u in units if u.keep_original)
    ctx.info(f"{len(units)} units cleaned: {flagged} flagged for review, "
             f"{unchanged} already clean; cost ${total_cost:.4f}")
