# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Automatic mapping decisions: embedding-ranked candidates, then an LLM choice (spec §4.2).

A row is decided again only when it is new or its candidate set changed (``candidate_hash``),
so the same inputs give the same mapping without calling the model. An item whose reply is
missing or unusable gets no row at all, so the next build decides it again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from coa_common.bedrock import BedrockTruncationError

from .mapping import ATTRIBUTE, CLASS, FORWARD, FREE, IGNORE, INVERSE, MANUAL, RELATION, MappingRow, mapping_id
from .tbox import PropInfo, TBox

logger = logging.getLogger(__name__)

PROMPT_VERSION = "2"
TOP_K = int(os.getenv("ABOX_MAPPING_TOP_K", "5"))
BATCH_SIZE = 20
MAX_DESCRIPTION = 300
MAX_SAMPLES = 3
MAX_SAMPLE_CHARS = 80

Embed = Callable[[list[str]], list[list[float]]]
Judge = Callable[[str, str], dict]
OnBatch = Callable[[list[MappingRow]], None]

SYSTEM = (
    "You map terms extracted from documents onto a domain ontology. For each item choose the "
    "candidate whose meaning matches the item, or -1 when none does. Candidates are numbered from 0 "
    "in the order listed. A class item may carry sample entity values in parentheses. A relation item "
    "reads <subject class> <relation> <object class>; each candidate shows the subject class on the left "
    "and the object class on the right, and its arrow gives the direction of the ontology property. "
    "Return only this JSON object, with no other text before or after it, and keep each rationale to "
    "at most 15 words: "
    '{"decisions": [{"id": <item id>, "choice": <0-based candidate index, or -1>, '
    '"rationale": "<at most 15 words>"}]}'
)


@dataclass(frozen=True)
class Candidate:
    """One ontology term an item may map to, with the text that is shown to the model."""

    iri: str
    direction: str
    text: str


@dataclass(frozen=True)
class Item:
    """One lexical string (or pair) to map, with its ranked candidates.

    ``text`` is stable (it feeds ranking and ``candidate_hash``); ``display`` is what the prompt shows
    when that differs, for example a class with sample entity values.
    """

    kind: str
    key: tuple[str, ...]
    text: str
    candidates: tuple[Candidate, ...]
    display: str = ""


class Embeddings:
    """Embeds each distinct text once per build and keeps its unit-length vector."""

    def __init__(self, embed: Embed) -> None:
        """Wrap ``embed`` with a per-instance cache."""
        self._embed = embed
        self._cache: dict[str, list[float]] = {}

    def vectors(self, texts: list[str]) -> list[list[float]]:
        """The unit-length vector of every text in order, embedding only the texts not seen yet."""
        missing = sorted({t for t in texts if t not in self._cache})
        if missing:
            for text, vector in zip(missing, self._embed(missing), strict=True):
                norm = math.sqrt(sum(x * x for x in vector))
                self._cache[text] = [x / norm for x in vector] if norm else list(vector)
        return [self._cache[t] for t in texts]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _top(emb: Embeddings, text: str, entries: dict[str, str]) -> list[str]:
    """The IRIs of at most ``TOP_K`` entries (IRI to ranking text) closest to ``text``.

    Sorted by IRI when nothing is dropped, best first otherwise; ties break on the IRI.
    """
    iris = sorted(entries)
    if len(iris) <= TOP_K:
        return iris
    query, *vectors = emb.vectors([text, *[entries[i] for i in iris]])
    scored = sorted(zip(iris, vectors, strict=True), key=lambda iv: (-_dot(query, iv[1]), iv[0]))
    return [iri for iri, _ in scored[:TOP_K]]


def _describe(label: str, description: str) -> str:
    description = description.strip()[:MAX_DESCRIPTION].strip()
    return f"{label}: {description}" if description else label


def _prop_text(p: PropInfo) -> str:
    return _describe(p.label, p.description)


def _class_label(tbox: TBox, iri: str) -> str:
    info = tbox.classes.get(iri)
    return info.label if info else iri


def candidate_hash(candidates: tuple[Candidate, ...], text: str = "") -> str:
    """Digest of the candidate set, item text and prompt version; a new digest means the row is decided again."""
    payload = [PROMPT_VERSION, text, [[c.iri, c.direction, c.text] for c in candidates]]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def class_items(
    lexical_classes: Iterable[str] | Mapping[str, Sequence[str]], tbox: TBox, emb: Embeddings
) -> list[Item]:
    """One item per distinct lexical class string, ranked against every TBox class.

    A mapping from class to sample entity values (the first ``MAX_SAMPLES`` non-blank ones, each cut to
    ``MAX_SAMPLE_CHARS``) lets the prompt show them; they never reach the ranking or the hash.
    """
    entries = {c.iri: _describe(c.label, c.description) for c in tbox.classes.values()}
    items = []
    for lc in sorted(set(lexical_classes)):
        candidates = tuple(Candidate(iri, FORWARD, entries[iri]) for iri in _top(emb, lc, entries))
        values = lexical_classes[lc] if isinstance(lexical_classes, Mapping) else ()
        samples = [cut for v in values if (cut := v.strip()[:MAX_SAMPLE_CHARS].strip())][:MAX_SAMPLES]
        display = f"{lc} (e.g. {', '.join(samples)})" if samples else ""
        items.append(Item(CLASS, (lc,), lc, candidates, display))
    return items


def relation_items(keys: Iterable[tuple[str, str, str]], tbox: TBox, emb: Embeddings) -> list[Item]:
    """One item per (relation, subject class IRI, object class IRI).

    Properties are ranked first and each kept property is then expanded to the directions that fit, so
    ``TOP_K`` never cuts one direction of a property it kept.
    """
    items = []
    for rel, s_cls, o_cls in sorted(set(keys)):
        s_label, o_label = _class_label(tbox, s_cls), _class_label(tbox, o_cls)
        fits = {
            p.iri: (tbox.fits(p, s_cls, o_cls), tbox.fits(p, o_cls, s_cls))
            for p in tbox.object_props.values()
            if tbox.fits(p, s_cls, o_cls) or tbox.fits(p, o_cls, s_cls)
        }
        text = f"{s_label} {rel} {o_label}"
        candidates = []
        for iri in _top(emb, text, {i: _prop_text(tbox.object_props[i]) for i in fits}):
            p = tbox.object_props[iri]
            forward, inverse = fits[iri]
            if forward:
                candidates.append(
                    Candidate(iri, FORWARD, _describe(f"{s_label} -{p.label}-> {o_label}", p.description))
                )
            if inverse:
                candidates.append(
                    Candidate(iri, INVERSE, _describe(f"{s_label} <-{p.label}- {o_label}", p.description))
                )
        items.append(Item(RELATION, (rel, s_cls, o_cls), text, tuple(candidates)))
    return items


def attribute_items(keys: Iterable[tuple[str, str]], tbox: TBox, emb: Embeddings) -> list[Item]:
    """One item per (subject class IRI, predicate), ranked against the datatype properties that admit the class."""
    items = []
    for s_cls, predicate in sorted(set(keys)):
        entries = {p.iri: _prop_text(p) for p in tbox.datatype_props.values() if tbox.fits(p, s_cls, None)}
        text = f"{_class_label(tbox, s_cls)} {predicate}"
        candidates = tuple(Candidate(iri, FORWARD, entries[iri]) for iri in _top(emb, text, entries))
        items.append(Item(ATTRIBUTE, (s_cls, predicate), text, candidates))
    return items


def _none(kind: str) -> str:
    return FREE if kind == ATTRIBUTE else IGNORE


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _decisions(result: object) -> dict[int, dict]:
    """The reply's decisions by item id; anything that is not a well-formed decision is dropped."""
    raw = result.get("decisions") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return {}
    return {d["id"]: d for d in raw if isinstance(d, dict) and _is_int(d.get("id"))}


def _row(item: Item, digest: str, decision: dict | None, version: int) -> MappingRow | None:
    """The row a reply decides for ``item``, or None when it holds no valid choice (-1 is a valid choice)."""
    if decision is None:
        return None
    choice = decision.get("choice")
    if not isinstance(choice, int) or isinstance(choice, bool) or not -1 <= choice < len(item.candidates):
        return None
    if choice == -1:
        target, direction = _none(item.kind), FORWARD
    else:
        target, direction = item.candidates[choice].iri, item.candidates[choice].direction
    rationale = str(decision.get("rationale") or "")[:300]
    return MappingRow(
        item.kind,
        item.key,
        target,
        direction=direction,
        rationale=rationale,
        candidate_hash=digest,
        decided_for_version=version,
    )


def decide(
    items: list[Item],
    existing: dict[str, MappingRow],
    *,
    judge: Judge,
    version: int,
    on_batch: OnBatch | None = None,
) -> list[MappingRow]:
    """Rows to save: new keys and keys whose candidates changed. Manual rows are never touched.

    An item gets a row only when the reply holds a valid choice for it; otherwise it is left for the
    next build. ``on_batch`` receives every returned row exactly once, right after it is decided: first
    the rows of items with no candidates, then each judged batch. A batch whose reply is truncated is
    split in half and each half judged on its own, down to single items; a single item whose reply is
    still truncated is logged and skipped. A batch whose judge call raises anything else is logged and
    skipped. When no judge call succeeds the last error is raised, so a judge that cannot run at all
    fails the build instead of producing an empty ABox; a truncation that splitting resolves is no failure.
    """
    out: list[MappingRow] = []
    pending: list[tuple[Item, str]] = []
    for item in items:
        row = existing.get(mapping_id(item.kind, item.key))
        if row is not None and row.origin == MANUAL:
            continue
        digest = candidate_hash(item.candidates, item.text)
        if row is not None and row.candidate_hash == digest:
            continue
        if not item.candidates:
            out.append(
                MappingRow(
                    item.kind,
                    item.key,
                    _none(item.kind),
                    rationale="No compatible ontology term.",
                    candidate_hash=digest,
                    decided_for_version=version,
                )
            )
            continue
        pending.append((item, digest))
    if out and on_batch:
        on_batch(list(out))
    succeeded = False
    last_error: Exception | None = None
    # The batches still to judge, next one last; a truncated batch goes back as its two halves.
    todo = [pending[start : start + BATCH_SIZE] for start in range(0, len(pending), BATCH_SIZE)][::-1]
    while todo:
        batch = todo.pop()
        # Ids are positions in this batch, so a half is numbered from 0 again and maps back the same way.
        user = json.dumps(
            {
                "items": [
                    {
                        "id": i,
                        "kind": it.kind,
                        "text": it.display or it.text,
                        "candidates": [c.text for c in it.candidates],
                    }
                    for i, (it, _) in enumerate(batch)
                ]
            },
            ensure_ascii=False,
        )
        try:
            result = judge(SYSTEM, user)
        except BedrockTruncationError as e:
            if len(batch) > 1:
                half = len(batch) // 2
                logger.info("ABox mapping batch of %d items was truncated; judging its halves", len(batch))
                todo += [batch[half:], batch[:half]]
                continue
            last_error = e
            item = batch[0][0]
            logger.warning("ABox mapping item %s is skipped: %s", mapping_id(item.kind, item.key), e)
            continue
        except Exception as e:
            last_error = e
            logger.warning("ABox mapping batch of %d items failed and is skipped: %s", len(batch), e)
            continue
        succeeded = True
        decisions = _decisions(result)
        rows = [r for i, (it, digest) in enumerate(batch) if (r := _row(it, digest, decisions.get(i), version))]
        if len(rows) < len(batch):
            logger.warning("ABox mapping: %d of %d items got no valid decision", len(batch) - len(rows), len(batch))
        out.extend(rows)
        if rows and on_batch:
            on_batch(list(rows))
    if last_error is not None and not succeeded:
        raise last_error
    return out


def _temperature() -> float | None:
    """``ABOX_LLM_TEMPERATURE`` as a float in [0, 1], or None when unset; ValueError on anything else."""
    raw = os.getenv("ABOX_LLM_TEMPERATURE")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"ABOX_LLM_TEMPERATURE must be a number between 0 and 1, got {raw!r}")
    return value


def _max_tokens() -> int:
    """``ABOX_LLM_MAX_TOKENS`` as a positive int, 8000 when unset; ValueError on anything else."""
    raw = os.getenv("ABOX_LLM_MAX_TOKENS") or "8000"
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"ABOX_LLM_MAX_TOKENS must be a positive integer, got {raw!r}")
    return value


def bedrock_judge(model_id: str | None = None) -> Judge:
    """A judge that sends one batch to Bedrock as JSON and returns the parsed object."""
    from coa_common.bedrock import BedrockClient

    # No temperature by default: newer models (Claude Sonnet 5, Opus 4.7+) reject a non-default value
    # with a ValidationException, as the inducer's llm.py and grounding.py already note. Repeatable
    # mappings come from the candidate-hash cache and the saved rows, not from sampling at zero.
    # ``ABOX_LLM_TEMPERATURE`` opts in for a model that accepts it; it is checked once, here.
    temperature = _temperature()
    # A cap, not a spend: only the tokens a reply uses are billed. 4096 truncated 20-item batches.
    max_tokens = _max_tokens()
    client = BedrockClient(
        model_id=model_id
        or os.getenv("ABOX_MAPPING_MODEL_ID")
        or os.getenv("LLM_MODEL_ID")
        or "us.anthropic.claude-sonnet-5"
    )

    def judge(system: str, user: str) -> dict:
        try:
            result = client.invoke(system, user, max_tokens=max_tokens, temperature=temperature).result
        except BedrockTruncationError:
            raise  # ``decide`` splits a truncated batch; asking again would truncate again
        except ValueError as e:
            # A reply that is not JSON (prose before the object, an empty reply) is the model's slip, not
            # the request's: asking once more usually gets the object. A second slip goes to the caller.
            logger.warning("ABox judge reply was not JSON; asking once more: %s", e)
            result = client.invoke(system, user, max_tokens=max_tokens, temperature=temperature).result
        return result if isinstance(result, dict) else {}

    return judge


def bedrock_embed() -> Embed:
    """The embedding function of the Bedrock text-embeddings client, at one fixed size.

    These vectors only rank TBox terms against lexical strings and never meet an index, so they keep
    the library default size whatever ``BEDROCK_EMBED_DIMENSIONS`` says: the ranking, and so the
    ``candidate_hash`` of the saved rows, does not depend on how a deployment sized its chunk index.
    """
    from coa_common.constants import DEFAULT_EMBED_DIMENSIONS

    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

    return BedrockEmbeddingClient(
        region=os.getenv("AWS_REGION", "us-east-1"), dimensions=DEFAULT_EMBED_DIMENSIONS
    ).embed_texts
