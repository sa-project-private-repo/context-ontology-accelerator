# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit cover for how the integ suite reads the tables of a published mapping.

``latest/mappings.r2rml`` is a copy of the deployed version's snapshot, which the
engine writes as canonical N-Triples (``snapshot.canonical_nt``). The integ checks
once matched the Turtle text ``rr:tableName "x"`` and so found no table in it; this
runs their reader on that serialization without a deployed environment. Loaded by
path because the integ directory is not an importable package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from coa_ontology.versions import snapshot

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[1] / "integ" / "test_accept_ontology_components.py"

if not _MODULE_PATH.exists():
    pytest.skip("tests/integ/ is absent or incomplete in this checkout (public mirror)", allow_module_level=True)

_MAPPING = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <http://example.org/shop#> .
ex:TriplesMap_Claim a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"pc_insurance\\".\\"claim\\"" ] ;
    rr:subjectMap [ rr:template "http://example.org/shop/claim/{claim_id}" ; rr:class ex:Claim ] .
ex:TriplesMap_Policy a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "policy" ] ;
    rr:subjectMap [ rr:template "http://example.org/shop/policy/{policy_id}" ; rr:class ex:Policy ] .
"""


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("_accept_components_integ", _MODULE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_tables_of_a_published_mapping_are_read_from_its_n_triples(mod):
    published = snapshot.canonical_nt(snapshot.parse_turtle(_MAPPING, what="R2RML"))
    assert "rr:tableName" not in published
    assert mod._r2rml_table_names(published) == {'"pc_insurance"."claim"', "policy"}


def test_the_tables_of_a_turtle_mapping_are_read_the_same_way(mod):
    assert mod._r2rml_table_names(_MAPPING) == {'"pc_insurance"."claim"', "policy"}
