# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The sentences of the designer's check findings, change history and change warnings, as stable codes.

The web app writes a coded sentence in the user's language from its catalogue. The engine keeps writing
the English sentence next to the code (a finding's ``message``, a history entry's ``summary``, a change's
``warnings``) for API clients, and history entries and change sets stored before the codes have only that
text. ``ENGLISH`` holds the English of every code; a parameter is text: a term's label or IRI tail, a
version or revision number, a count, table names.
"""

from __future__ import annotations

from typing import TypedDict

# The model's bounds: ``StudioMessageCode``, ``StudioMessageParams``, ``StudioMessageParamName`` and
# ``StudioMessageList`` (the coded lines of one summary or one list of warnings).
MAX_CODE_LENGTH = 64
MAX_PARAMS = 10
MAX_PARAM_NAME_LENGTH = 32
MAX_MESSAGES = 100

ENGLISH = {
    # The lines of a history entry's summary.
    "OPENED_DRAFT": "Opened the draft on v{version}",
    "OPENED_DRAFT_NO_VERSION": "Opened the draft on no version",
    "UNDID_REVISION": "Undid revision {revision}",
    "REBASED": "Rebased onto v{version}",
    "REBASED_NO_VERSION": "Rebased onto no version",
    "ADD_CLASS": "Added class {label}",
    "SET_LABEL": "Renamed {term} to {label}",
    "SET_COMMENT": "Changed the description of {term}",
    "CHANGE_IRI": "Changed the IRI of {term} to {newTerm}",
    "DELETE_CLASS_DETACH": "Deleted class {term} (detach its properties)",
    "DELETE_CLASS_DELETE": "Deleted class {term} (delete its properties)",
    "MERGE_CLASSES": "Merged {source} into {target}",
    "ADD_PARENT": "Made {term} a subclass of {parent}",
    "REMOVE_PARENT": "Removed {parent} from the parents of {term}",
    "ADD_OBJECT_PROPERTY": "Added relationship {label} from {domain} to {range}",
    "ADD_DATATYPE_PROPERTY": "Added attribute {label} to {domain}",
    # ``fields``: the changed fields, among domain, range (datatype for an attribute; lines stored before say
    # range), label and comment, joined by ", ".
    "UPDATE_PROPERTY": "Changed the {fields} of {term}",
    "DELETE_PROPERTY": "Deleted property {term}",
    "REMOVE_MATCH": "Removed the grounding of {term}",
    "SET_EXACT_MATCH": "Grounded {term} to {target} (exact)",
    "SET_CLOSE_MATCH": "Grounded {term} to {target} (close)",
    "REPLACE_DATATYPE": "Replaced the datatype {old} with {new}",
    "RESTORE_TRIPLES": "Reverted a change",
    # The messages of the instant findings.
    "DANGLING_DOMAIN": "The domain of {property} is {domain}, which is not a class of the draft",
    "DANGLING_RANGE": "The range of {property} is {range}, which is not a class of the draft",
    "UNKNOWN_DATATYPE_RANGE": (
        "The range of the attribute {property} is {range}, which is not a datatype the Ontology designer accepts"
    ),
    "DATATYPE_RANGE_NOT_DATATYPE": "The attribute {property} has the class {range} as its range, not a datatype",
    # ``classes``: their names in the cycle, joined by " -> ".
    "SUBCLASS_CYCLE": "These classes are subclasses of each other: {classes}",
    "MISSING_LABEL": "{term} has no label",
    "DUPLICATE_CLASS_LABEL": "{count} classes share the label {label!r}",
    "DUPLICATE_RELATIONSHIP_LABEL": "{count} relationships share the label {label!r}",
    "DUPLICATE_ATTRIBUTE_LABEL": "{count} attributes share the label {label!r}",
    "DUPLICATE_TERM_LABEL": "{count} terms share the label {label!r}",
    "ISOLATED_CLASS": "{term} has no relationship, parent or attribute",
    # The warnings of a change. ``sourceTables``, ``targetTables`` and ``tables``: table names joined by ", ".
    "MERGE_MAPPED_TABLES": (
        "{source} and {target} are mapped to tables {sourceTables} and {targetTables}; "
        "structured SQL will describe them as one table."
    ),
    "DOMAIN_ON_PARENT_TABLE": (
        "{property} is mapped from table {tables}; with the domain {domain}, "
        "SPARQL through the VKG no longer offers it on {mappedClass}, but NL-to-SQL still does."
    ),
    # ``map``: the TriplesMap, for a mapping that names no table.
    "DOMAIN_ON_PARENT_MAP": (
        "{property} is mapped by {map}; with the domain {domain}, "
        "SPARQL through the VKG no longer offers it on {mappedClass}, but NL-to-SQL still does."
    ),
    "RANGE_ON_PARENT": (
        "{property} joins to {mappedClass} in the mapping; with the range {range}, "
        "SPARQL through the VKG no longer offers it as a join to {mappedClass}, but NL-to-SQL still does."
    ),
    "RANGE_UNCONFIRMED": (
        "{property} is mapped through an IRI template, so the mapping cannot confirm its range {range}"
    ),
    # A mapped property whose domain or range was a deleted parent gets the classes its mapping reads.
    "DOMAIN_GIVEN_BACK_TABLE": (
        "{deleted} was the domain of {property}, which is mapped from table {tables}; its domain is now {classes}."
    ),
    "DOMAIN_GIVEN_BACK_MAP": (
        "{deleted} was the domain of {property}, which is mapped by {map}; its domain is now {classes}."
    ),
    "RANGE_GIVEN_BACK": (
        "{deleted} was the range of {property}, which joins to {classes} in the mapping; its range is now {classes}."
    ),
    "ANOTHER_CLASS_LABELLED": "Another class is also labelled {label!r}: {iri}",
    "ANOTHER_RELATIONSHIP_LABELLED": "Another relationship is also labelled {label!r}: {iri}",
    "ANOTHER_ATTRIBUTE_LABELLED": "Another attribute is also labelled {label!r}: {iri}",
}


class Message(TypedDict):
    """One sentence: its code and its parameters by placeholder name."""

    code: str
    params: dict[str, str]


def message(code: str, **params: object) -> Message:
    """The sentence ``code`` with ``params`` as text."""
    return {"code": code, "params": {name: str(value) for name, value in params.items()}}


def english(m: Message) -> str:
    """The English sentence of ``m``."""
    return ENGLISH[m["code"]].format(**m["params"])


def listed(messages: list[Message] | None) -> list[Message] | None:
    """The first ``MAX_MESSAGES`` of ``messages``, as the API lists them; None (stored before the codes) stays None."""
    return None if messages is None else list(messages[:MAX_MESSAGES])
