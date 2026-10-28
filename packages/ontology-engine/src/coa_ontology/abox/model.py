# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Vocabulary, deterministic IRIs and record types of the domain ABox (spec §3)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

COA = "urn:coa:vocab#"
DOMAIN_ABOX = f"{COA}DomainAbox"
CONFORMS_TO_VERSION = f"{COA}conformsToVersion"
MAPPING_REVISION = f"{COA}mappingRevision"
BUILD_ID = f"{COA}buildId"
BUILT_AT = f"{COA}builtAt"
LEXICAL_ENTITY_ID = f"{COA}lexicalEntityId"
NORM_KEY = f"{COA}normKey"
FREE_ATTRIBUTE = f"{COA}FreeAttribute"
EVIDENCE = f"{COA}Evidence"
ABOUT = f"{COA}about"
PREDICATE = f"{COA}predicate"
OBJECT = f"{COA}object"
CHUNK_ID = f"{COA}chunkId"
DOC_SOURCE_ID = f"{COA}docSourceId"
SOURCE_KEY = f"{COA}sourceKey"

# graphrag-toolkit stores an SPC complement as an entity of this class
# (graphrag_toolkit/lexical_graph/indexing/constants.py LOCAL_ENTITY_CLASSIFICATION).
LOCAL_ENTITY_CLASS = "__Local_Entity__"
# graphrag-toolkit's TIMESTAMP_UPPER_BOUND: a Source/chunk version still current.
CURRENT_VALID_TO = 10_000_000_000_000

_WS = re.compile(r"\s+")
# Keeps Unicode letters and digits (a Korean class name stays readable in its IRI).
_UNSAFE = re.compile(r"[^\w-]")
_SLUG = re.compile(r"[^a-z0-9]+")


def norm(name: str) -> str:
    """NFKC, casefold, drop format characters, trim and collapse whitespace: the identity of a name.

    Format characters (Unicode category ``Cf``: zero-width space and joiner, soft hyphen, BOM,
    word joiner) are invisible, so two names that differ only by them are the same name.
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    visible = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return _WS.sub(" ", visible).strip()


def _digest(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def norm_key(name: str) -> str:
    """Hex key of ``norm(name)``, safe inside a SPARQL query (serve's keyword guard)."""
    return _digest(norm(name), 16)


def ns_data(namespace: str) -> str:
    """Base IRI of a namespace's data resources."""
    return f"http://coa.amazon.com/namespace/{namespace}/data/"


def iri_tail(iri: str) -> str:
    """Last path or fragment segment of ``iri``, exactly as written (empty when there is none)."""
    return re.split(r"[#/]", iri.rstrip("/#"))[-1]


def local_name(iri: str) -> str:
    """Last path or fragment segment of ``iri``, reduced to IRI-safe characters.

    This is an identifier for building IRIs, not a display label: use ``iri_tail`` for a label.
    """
    return _UNSAFE.sub("_", iri_tail(iri)) or "Thing"


def instance_iri(namespace: str, class_iri: str, name: str) -> str:
    """Deterministic IRI of the instance of ``class_iri`` named ``name``."""
    # The class IRI is hashed in so two classes sharing a local name never collide.
    return f"{ns_data(namespace)}{local_name(class_iri)}/{_digest(class_iri + chr(10) + norm(name), 16)}"


def free_attribute_iri(namespace: str, key: str) -> str:
    """IRI of a free attribute (a key with no datatype property in the TBox)."""
    slug = _SLUG.sub("-", norm(key)).strip("-")[:48] or "attr"
    return f"{ns_data(namespace)}attr/{slug}-{_digest(norm(key), 16)}"


def evidence_iri(namespace: str, s: str, p: str, o: str, chunk_id: str) -> str:
    """IRI of the evidence node for one supporting chunk of the statement (s, p, o)."""
    return f"{ns_data(namespace)}evidence/{_digest(chr(10).join((s, p, o, chunk_id)), 32)}"


def header_iri(namespace: str) -> str:
    """IRI of the ABox header resource of a namespace."""
    return f"{ns_data(namespace)}abox"


@dataclass(frozen=True, order=True)
class Provenance:
    """Where a statement was read: the chunk, its document source and the source key."""

    chunk_id: str
    doc_source_id: str
    source_key: str


@dataclass(frozen=True)
class LexEntity:
    """One entity of the graphrag lexical graph, with its lexical class string."""

    id: str
    value: str
    cls: str


@dataclass(frozen=True)
class LexFact:
    """One graphrag fact. ``object_id`` names an entity, or a complement entity (an attribute)."""

    id: str
    subject_id: str
    relation: str
    object_id: str | None
    prov: Provenance


@dataclass(frozen=True)
class LexicalSnapshot:
    """The entities and facts read from the lexical graph for one build."""

    entities: dict[str, LexEntity]
    facts: tuple[LexFact, ...]


@dataclass(frozen=True)
class XEntity:
    """An instance found by targeted re-extraction."""

    cls: str
    name: str
    attributes: tuple[tuple[str, str], ...]
    prov: Provenance


@dataclass(frozen=True)
class XRelation:
    """A relation found by targeted re-extraction."""

    subject: tuple[str, str]  # (class IRI, name)
    prop: str
    object: tuple[str, str]
    prov: Provenance
