# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import hashlib

import pytest
from coa_ontology.abox import model as m
from coa_ontology.stores.neptune_db_graph import _iri

pytestmark = pytest.mark.unit

NS = "11111111-2222-3333-4444-555555555555"
CUSTOMER = "http://e.com/o#Customer"


def test_norm_folds_case_width_and_whitespace():
    assert m.norm("  ＡＣＭＥ\tCorp ") == "acme corp"
    assert m.norm("ACME   corp") == m.norm("acme corp")


def test_instance_iri_is_deterministic_and_class_scoped():
    a = m.instance_iri(NS, CUSTOMER, "ACME Corp")
    assert a == m.instance_iri(NS, CUSTOMER, " acme   CORP")
    assert a.startswith(f"http://coa.amazon.com/namespace/{NS}/data/Customer/")
    assert len(a.rsplit("/", 1)[1]) == 16
    # Same local name, different class IRI: never the same instance.
    assert a != m.instance_iri(NS, "http://other.com/x#Customer", "ACME Corp")


def test_local_name_is_iri_safe():
    assert m.local_name("http://e.com/o#Order Line") == "Order_Line"
    assert m.local_name("http://e.com/o/") == "o"


def test_free_attribute_iri_has_a_readable_slug_and_a_hash():
    iri = m.free_attribute_iri(NS, "Contract Value (USD)")
    assert iri.startswith(f"http://coa.amazon.com/namespace/{NS}/data/attr/contract-value-usd-")
    assert iri == m.free_attribute_iri(NS, "contract value (usd)")


def test_evidence_iri_differs_per_chunk():
    s, p, o = "urn:s", "urn:p", "urn:o"
    assert m.evidence_iri(NS, s, p, o, "c1") != m.evidence_iri(NS, s, p, o, "c2")
    assert m.evidence_iri(NS, s, p, o, "c1") == m.evidence_iri(NS, s, p, o, "c1")


def test_norm_key_is_hex():
    key = m.norm_key("Add-on Pack")
    assert len(key) == 16 and int(key, 16) >= 0


def test_norm_key_golden_value():
    # Serve re-implements norm_key, so this pins the contract between the two.
    assert m.norm_key("  ACME   Corp ") == hashlib.sha256(b"acme corp").hexdigest()[:16]
    assert m.norm_key("  ACME   Corp ") == "ea6f9c07a2f95c78"


def test_instance_iri_golden_value():
    digest = hashlib.sha256(b"http://e.com/o#Customer\nacme corp").hexdigest()[:16]
    assert digest == "2677107c9b4ba863"
    assert m.instance_iri(NS, CUSTOMER, "ACME Corp") == f"http://coa.amazon.com/namespace/{NS}/data/Customer/{digest}"


def test_norm_strips_format_characters():
    # ZWSP, ZWJ, soft hyphen, BOM and word joiner are invisible: they are not part of a name.
    assert m.norm("AC\u200bME\u200d Co\u00adrp\ufeff\u2060") == "acme corp"
    assert m.norm("\u200b ACME \u200b") == "acme"
    assert m.norm_key("AC\u200bME") == m.norm_key("acme")


def test_korean_class_gets_a_readable_local_name_that_neptune_accepts():
    cls = "http://e.com/o#고객"
    assert m.local_name(cls) == "고객"
    iri = m.instance_iri(NS, cls, "홍길동")
    assert iri.startswith(f"http://coa.amazon.com/namespace/{NS}/data/고객/")
    assert _iri(iri) == f"<{iri}>"
    assert _iri(m.free_attribute_iri(NS, "계약 금액"))


def test_local_name_still_replaces_unsafe_characters():
    assert m.local_name("http://e.com/o#a<b>c%20d") == "a_b_c_20d"
    assert m.local_name("http://e.com/o#") == "o"


def test_iri_tail_is_the_raw_segment():
    assert m.iri_tail("http://e.com/o#Order%20Line") == "Order%20Line"
    assert m.iri_tail("http://e.com/o/") == "o"


def test_free_attribute_hash_is_16_hex():
    iri = m.free_attribute_iri(NS, "Contract Value (USD)")
    assert len(iri.rsplit("-", 1)[1]) == 16
