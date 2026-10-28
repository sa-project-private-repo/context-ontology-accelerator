# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The namespace's IRI space and how the designer's ops mint IRIs from labels.

Minting reuses the inducers' own label functions so a class added in the designer looks like one the
inducer made: ``to_pascal`` / ``to_camel`` from the database inducer, and
``normalize_for_iri_path`` from the document inducer, which keeps Unicode letters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote

from coa_common.constants import GRAPH_BASE_URI

from coa_ontology.abox.model import iri_tail
from coa_ontology.inducer.strategies.base import to_camel, to_pascal
from coa_ontology.inducer.unstructured.services.iri_resolver import normalize_for_iri_path
from coa_ontology.stores.neptune_db_graph import _iri

STRUCTURED = "structured"
DOCUMENT = "document"
XSD = "http://www.w3.org/2001/XMLSchema#"
# The 40 common XSD datatypes, by local name (matched case-sensitively). Left out are the XSD
# built-ins ID, IDREF, IDREFS, ENTITY, ENTITIES, NMTOKENS and NOTATION, and the abstract
# anyType, anySimpleType and anyAtomicType. The three non-XSD literal ranges that follow
# are rdfs:Literal, rdf:langString and rdf:PlainLiteral.
# fmt: off
_XSD_BUILTINS = [
    "string", "boolean", "decimal", "integer", "double", "float",
    "date", "time", "dateTime", "dateTimeStamp",
    "duration", "dayTimeDuration", "yearMonthDuration",
    "gYear", "gYearMonth", "gMonth", "gMonthDay", "gDay",
    "hexBinary", "base64Binary", "anyURI", "QName",
    "normalizedString", "token", "language", "Name", "NCName", "NMTOKEN",
    "long", "int", "short", "byte",
    "nonNegativeInteger", "positiveInteger", "nonPositiveInteger", "negativeInteger",
    "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte",
]
# fmt: on
_EXTRA_DATATYPES = (
    "http://www.w3.org/2000/01/rdf-schema#Literal",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral",
)
_DATATYPES = frozenset([*(f"{XSD}{name}" for name in _XSD_BUILTINS), *_EXTRA_DATATYPES])
MAX_SUFFIX = 1000
# The model's ``VertexIri`` bound on an IRI a request names in its query (``focus``, ``iri``).
MAX_IRI_LENGTH = 2048


class IriError(ValueError):
    """An IRI an op names is malformed, outside the namespace's IRI space, or cannot be minted."""


@dataclass(frozen=True)
class IriScheme:
    """Where a draft's own terms live and which inducer's naming new terms follow."""

    namespace: str
    ontology_prefix: str
    style: str

    @property
    def namespace_space(self) -> str:
        """``{GRAPH_BASE_URI}/namespace/{ns}/ontology/``: every inducer mints under it."""
        return f"{GRAPH_BASE_URI}/namespace/{self.namespace}/ontology/"

    @property
    def document_prefix(self) -> str:
        """The document inducer's prefix (``InMemoryIRIResolver`` with ontology name ``unstructured``)."""
        return f"{self.namespace_space}unstructured/"

    def in_space(self, iri: str) -> bool:
        """Whether ``iri`` is one of this namespace's own terms (an op may change it)."""
        return iri.startswith(self.namespace_space) or iri.startswith(self.ontology_prefix)

    @property
    def match_confidence(self) -> str:
        """The inducers' ``matchConfidence`` annotation property (``{uri_prefix}matchConfidence``)."""
        return f"{self.ontology_prefix}matchConfidence"


def scheme_for(namespace: str, ontology_id: str, source_type: str | None) -> IriScheme:
    """The scheme of a draft whose versions belong to ``ontology_id``."""
    prefix = ontology_id if ontology_id.endswith(("#", "/")) else f"{ontology_id}#"
    style = DOCUMENT if source_type == "UNSTRUCTURED" else STRUCTURED
    return IriScheme(namespace=namespace, ontology_prefix=prefix, style=style)


def check_iri(value: str, what: str) -> str:
    """Return ``value`` if it is an IRI Neptune and SPARQL accept; raise :class:`IriError` otherwise."""
    try:
        _iri(value)
    except ValueError as e:
        raise IriError(f"{what} is not a valid IRI: {value!r}") from e
    return value


def check_in_space(scheme: IriScheme, value: str, what: str) -> str:
    """Return ``value`` if it is a valid IRI inside the namespace's IRI space."""
    check_iri(value, what)
    if not scheme.in_space(value):
        raise IriError(
            f"{what} {value} is outside this namespace's ontology IRI space "
            f"({scheme.namespace_space} or {scheme.ontology_prefix})"
        )
    return value


def expand_xsd(value: str) -> str:
    """``xsd:foo`` as its IRI; any other text as it is. Nothing is checked."""
    return f"{XSD}{value[4:]}" if value.startswith("xsd:") else value


def datatype_iri(value: str) -> str:
    """Expand ``xsd:foo`` and return the IRI if it is a datatype range the designer accepts.

    That is one of the common XSD datatypes listed above (not ID, IDREF, IDREFS, ENTITY,
    ENTITIES, NMTOKENS, NOTATION or the abstract any* types), ``rdfs:Literal``,
    ``rdf:langString`` or ``rdf:PlainLiteral``.
    Matching is case-sensitive: ``xsd:dateTime`` is accepted, ``xsd:datetime`` is not.
    """
    full = expand_xsd(value)
    if full not in _DATATYPES:
        raise IriError(f"{value!r} is not an XSD datatype")
    return full


def has_letter_or_digit(label: str) -> bool:
    """Whether ``label`` can name a term: it holds at least one letter or digit in any script."""
    return any(c.isalnum() for c in label)


def _segment(text: str) -> str:
    """The document inducer's case-preserving path segment (Unicode letters kept, the rest escaped)."""
    return normalize_for_iri_path(text.strip())


def _require_nameable(label: str) -> None:
    """Refuse a label that cannot name a term, rather than minting a placeholder or an empty segment."""
    if not has_letter_or_digit(label):
        raise IriError("A label needs at least one letter or digit")


def _first_free(base: str, taken: Callable[[str], bool]) -> str:
    if not taken(base):
        return base
    for n in range(2, MAX_SUFFIX):
        candidate = f"{base}_{n}"
        if not taken(candidate):
            return candidate
    raise IriError(f"Could not mint a free IRI for {base} after {MAX_SUFFIX} attempts")


def mint_class_iri(scheme: IriScheme, label: str, taken: Callable[[str], bool]) -> str:
    """Mint the IRI the inducer would give a class labelled ``label``, suffixed ``_2``, ``_3``… when taken.

    A label without a letter or digit raises :class:`IriError`; it never mints a placeholder.
    """
    _require_nameable(label)
    if scheme.style == DOCUMENT:
        base = f"{scheme.document_prefix}class/{_segment(label)}"
    elif label.isascii():
        base = f"{scheme.ontology_prefix}{to_pascal(label)}"
    else:
        base = f"{scheme.ontology_prefix}{_segment(label)}"
    return _first_free(check_iri(base, "minted IRI"), taken)


def _domain_part(domain_iri: str, camel: bool) -> str:
    tail = unquote(iri_tail(domain_iri))
    return to_camel(tail) if camel and tail.isascii() else _segment(tail)


def mint_property_iri(scheme: IriScheme, label: str, kind: str, domain_iri: str, taken: Callable[[str], bool]) -> str:
    """Mint a property IRI the inducer's way; ``kind`` is ``ObjectProperty`` or ``DatatypeProperty``.

    Database style: ``{prefix}{camelDomain}_{camelLabel}``. Document style:
    ``{prefix}unstructured/{Domain}/{kind}/{label}``. A label without a letter or digit raises :class:`IriError`.
    """
    _require_nameable(label)
    if scheme.style == DOCUMENT:
        base = f"{scheme.document_prefix}{_domain_part(domain_iri, camel=False)}/{kind}/{_segment(label)}"
    else:
        local = to_camel(label) if label.isascii() else _segment(label)
        base = f"{scheme.ontology_prefix}{_domain_part(domain_iri, camel=True)}_{local}"
    return _first_free(check_iri(base, "minted IRI"), taken)
