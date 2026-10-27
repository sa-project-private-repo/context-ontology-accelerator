# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What ingest reads from an R2RML mapping: mapped properties, physical names, and the NL-to-SQL documents.

The documents are the AOSS class docs serve's NL-to-SQL path retrieves. Its parser
(``coa_serve.tier2.nl_to_sql.sql_generator._table_name_from_class_text``) reads the
table name from the first ``" | "`` segment, ``Table: <name>``, and the SQL firewall
checks the tables the generated SQL names; everything after the table segment is
prompt text for the SQL writer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.catalog import ingest
from coa_ontology.studio import checks, ops
from coa_ontology.versions import snapshot

from . import test_r2rml_spec_compliance as spec_compliance

pytestmark = pytest.mark.unit

EX = "http://example.org/o#"
CUSTOMER, ORDER, REGION, PARTY = f"{EX}Customer", f"{EX}Order", f"{EX}Region", f"{EX}Party"
CUSTOMER_ID, EMAIL, TIER, PREFERS = (f"{EX}{n}" for n in ("customer_id", "customer_email", "customer_tier", "prefers"))
PLACED_BY, IN_REGION, REGION_NAME = f"{EX}order_placedBy", f"{EX}order_region", f"{EX}region_name"

# Customer and Order are mapped. Customer's label and its email attribute's label were edited, and two terms
# were added to it with no mapping: the attribute "loyalty tier" and the relationship "preferred order".
TBOX = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{CUSTOMER}> a owl:Class ; rdfs:label "Client" ; skos:altLabel "buyer" ; owl:hasKey ( <{CUSTOMER_ID}> ) .
<{ORDER}> a owl:Class ; rdfs:label "Purchase" .
<{REGION}> a owl:Class ; rdfs:label "Region" .
<{CUSTOMER_ID}> a owl:DatatypeProperty ; rdfs:label "id" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:integer .
<{EMAIL}> a owl:DatatypeProperty ; rdfs:label "E-mail address" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:string .
<{TIER}> a owl:DatatypeProperty ; rdfs:label "loyalty tier" ; rdfs:domain <{CUSTOMER}> ; rdfs:range xsd:string .
<{PREFERS}> a owl:ObjectProperty ; rdfs:label "preferred order" ; rdfs:domain <{CUSTOMER}> ; rdfs:range <{ORDER}> .
<{PLACED_BY}> a owl:ObjectProperty ; rdfs:label "customer_id" ; rdfs:domain <{ORDER}> ; rdfs:range <{CUSTOMER}> .
<{IN_REGION}> a owl:ObjectProperty ; rdfs:label "region" ; rdfs:domain <{ORDER}> ; rdfs:range <{REGION}> .
<{REGION_NAME}> a owl:DatatypeProperty ; rdfs:label "name" ; rdfs:domain <{REGION}> .
"""
# The inducers' SQL-delimited literals: a bare table, a schema-qualified one, quoted columns.
R2RML = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix coa: <{VOCAB_URI}> .
<{EX}TriplesMap_Customer> a rr:TriplesMap ; coa:datasourceId "ds-1" ;
    rr:logicalTable [ rr:tableName "\\"customers\\"" ] ;
    rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "c/{{\\"id\\"}}" ] ;
    rr:predicateObjectMap [ rr:predicate <{CUSTOMER_ID}> ; rr:objectMap [ rr:column "\\"id\\"" ] ] ,
        [ rr:predicateMap [ rr:constant <{EMAIL}> ] ; rr:objectMap [ rr:column "\\"email\\"" ] ] .
<{EX}TriplesMap_Order> a rr:TriplesMap ; coa:datasourceId "ds-1" ;
    rr:logicalTable [ rr:tableName "\\"sales\\".\\"orders\\"" ] ;
    rr:subjectMap [ rr:class <{ORDER}> ; rr:template "o/{{\\"id\\"}}" ] ;
    rr:predicateObjectMap [ rr:predicate <{PLACED_BY}> ; rr:objectMap [ rr:parentTriplesMap <{EX}TriplesMap_Customer> ;
            rr:joinCondition [ rr:child "\\"customer_id\\"" ; rr:parent "\\"id\\"" ] ] ] ,
        [ rr:predicate <{IN_REGION}> ; rr:objectMap [ rr:template "r/{{\\"region\\"}}" ; rr:termType rr:IRI ] ] .
"""

# A parent of Customer read from a table of its own, which has no email column.
PARTY_MAP = f"""<{EX}TriplesMap_Party> a rr:TriplesMap ; coa:datasourceId "ds-1" ;
    rr:logicalTable [ rr:tableName "\\"parties\\"" ] ;
    rr:subjectMap [ rr:class <{PARTY}> ; rr:template "p/{{\\"id\\"}}" ] .
"""


def _moved_up(*, email: bool = False, placed_by: bool = False, party_label: str = "Party") -> str:
    """TBOX with Party as a parent of Customer, and email's domain or placed by's range moved up to it.

    The designer allows both, with a warning; the mapping still reads email from customers and joins orders
    to customers.
    """
    tbox = TBOX + f'<{PARTY}> a owl:Class ; rdfs:label "{party_label}" .\n<{CUSTOMER}> rdfs:subClassOf <{PARTY}> .\n'
    if email:
        tbox = tbox.replace(f'"E-mail address" ; rdfs:domain <{CUSTOMER}>', f'"E-mail address" ; rdfs:domain <{PARTY}>')
    if placed_by:
        tbox = tbox.replace(f"<{ORDER}> ; rdfs:range <{CUSTOMER}> .", f"<{ORDER}> ; rdfs:range <{PARTY}> .")
    return tbox


def _documents(tbox: str = TBOX, r2rml: str = R2RML, *, physical: bool = True) -> dict[str, dict]:
    """The class documents ``_accumulate_embeddings`` writes, by IRI; ``physical`` passes the R2RML's names."""
    g = snapshot.parse_turtle(tbox)
    classes, obj_props, dt_props = ingest.schema_subjects(g)
    vector_store = MagicMock()
    vector_store.store_embeddings_batch.side_effect = lambda items: list(items)
    bedrock = MagicMock(model_id="m")
    bedrock.embed_texts.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
    with patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", return_value=bedrock):
        result = ingest._accumulate_embeddings(
            vector_store=vector_store,
            graph=g,
            ontology_id=EX,
            classes=classes,
            obj_props=obj_props,
            dt_props=dt_props,
            namespace="ns",
            class_to_datasource=ingest._class_datasource_map_from_r2rml(r2rml),
            physical_names=ingest._physical_names_from_r2rml(r2rml) if physical else None,
        )
    assert result["status"] == "ok"
    items = vector_store.store_embeddings_batch.call_args.args[0]
    return {i["entity_uri"]: i for i in items if i["entity_type"] == "class"}


def _context(doc: dict) -> str:
    return doc.get("context_text", doc["text"])


class TestMappedPredicates:
    def test_a_property_named_by_rr_predicate_or_a_constant_predicate_map_is_mapped(self):
        assert ingest._mapped_predicates_from_r2rml(R2RML) == {CUSTOMER_ID, EMAIL, PLACED_BY, IN_REGION}

    def test_a_predicate_map_without_a_constant_maps_nothing(self):
        r2rml = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}M> a rr:TriplesMap ;
    rr:predicateObjectMap [ rr:predicateMap [ rr:column "p" ] ; rr:objectMap [ rr:column "v" ] ] ."""
        assert ingest._mapped_predicates_from_r2rml(r2rml) == set()

    @pytest.mark.parametrize("r2rml", [None, "", "   ", "this is not turtle {{{"])
    def test_no_mapping_maps_nothing(self, r2rml):
        assert ingest._mapped_predicates_from_r2rml(r2rml) == set()


class TestPhysicalNames:
    def test_tables_and_columns_are_unquoted_and_joins_and_templates_read_no_column(self):
        names = ingest._physical_names_from_r2rml(R2RML)
        assert names.tables == {CUSTOMER: {"customers"}, ORDER: {"orders"}}
        assert names.columns == {
            CUSTOMER: {CUSTOMER_ID: "id", EMAIL: "email"},
            ORDER: {PLACED_BY: "", IN_REGION: ""},
        }

    def test_a_class_mapped_from_two_tables_has_both_and_a_map_without_a_table_name_has_none(self):
        r2rml = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}A> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "client" ] ; rr:subjectMap [ rr:class <{CUSTOMER}> ] .
<{EX}B> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "customer" ] ; rr:subjectMap [ rr:class <{CUSTOMER}> ] .
<{EX}C> a rr:TriplesMap ; rr:logicalTable [ rr:sqlQuery "SELECT 1" ] ; rr:subjectMap [ rr:class <{ORDER}> ] ."""
        names = ingest._physical_names_from_r2rml(r2rml)
        assert names.tables == {CUSTOMER: {"client", "customer"}, ORDER: {""}}

    def test_no_mapping_names_nothing(self):
        assert ingest._physical_names_from_r2rml(None) == ingest.PhysicalNames()

    def test_a_property_is_named_by_its_column_when_every_map_of_it_reads_that_column(self):
        names = ingest._physical_names_from_r2rml(R2RML)
        assert names.property_columns == {CUSTOMER_ID: {"id"}, EMAIL: {"email"}, PLACED_BY: {""}, IN_REGION: {""}}
        assert (names.table_of(CUSTOMER), names.column_of(CUSTOMER_ID), names.column_of(EMAIL)) == (
            "customers",
            "id",
            "email",
        )
        # A join, an IRI template and a property no map reads have no column.
        assert names.column_of(PLACED_BY) == names.column_of(IN_REGION) == names.column_of(TIER) == ""

    def test_a_property_read_from_two_columns_or_from_no_single_column_has_no_name(self):
        r2rml = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}A> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "client" ] ; rr:subjectMap [ rr:class <{CUSTOMER}> ] ;
    rr:predicateObjectMap [ rr:predicate <{CUSTOMER_ID}> ; rr:objectMap [ rr:column "id" ] ] ,
        [ rr:predicate <{EMAIL}> ; rr:objectMap [ rr:column "email" ] ] ,
        [ rr:predicate <{TIER}> ; rr:objectMap [ rr:column "tier" ] ; rr:object "gold" ] .
<{EX}B> a rr:TriplesMap ; rr:logicalTable [ rr:tableName "customer" ] ; rr:subjectMap [ rr:class <{CUSTOMER}> ] ;
    rr:predicateObjectMap [ rr:predicate <{CUSTOMER_ID}> ; rr:objectMap [ rr:column "\\"id\\"" ] ] ,
        [ rr:predicate <{EMAIL}> ; rr:objectMap [ rr:column "e_mail" ] ] ,
        [ rr:predicate <{REGION_NAME}> ; rr:objectMap [ rr:template "r/{{name}}" ] ] ."""
        names = ingest._physical_names_from_r2rml(r2rml)
        assert names.column_of(CUSTOMER_ID) == "id"  # both maps read column id, however it is quoted
        assert names.column_of(EMAIL) == ""  # two columns
        assert names.column_of(TIER) == ""  # a column and a constant
        assert names.column_of(REGION_NAME) == ""  # an IRI template
        assert names.table_of(CUSTOMER) == ""  # two tables


class TestProjectionSpecs:
    def test_only_properties_of_a_mapped_class_without_a_mapping_are_unmapped(self):
        g = snapshot.parse_turtle(TBOX)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        _, props = ingest.projection_specs(
            g,
            classes,
            obj_props,
            dt_props,
            ingest._class_datasource_map_from_r2rml(R2RML),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(R2RML),
        )
        assert {p["prop_uri"] for p in props if p["unmapped"]} == {TIER, PREFERS}  # REGION_NAME: Region is unmapped

    def test_a_relationship_into_a_mapped_class_without_a_mapping_is_unmapped(self):
        # The foreign-key walk follows a relationship from both ends, so one that only reaches a mapped class
        # (from the unmapped Region) is marked too; one between two unmapped classes is not.
        member, neighbour = f"{EX}region_member", f"{EX}region_neighbour"
        tbox = TBOX + (
            f"<{member}> a owl:ObjectProperty ; rdfs:domain <{REGION}> ; rdfs:range <{CUSTOMER}> .\n"
            f"<{neighbour}> a owl:ObjectProperty ; rdfs:domain <{REGION}> ; rdfs:range <{REGION}> .\n"
        )
        g = snapshot.parse_turtle(tbox)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        _, props = ingest.projection_specs(
            g,
            classes,
            obj_props,
            dt_props,
            ingest._class_datasource_map_from_r2rml(R2RML),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(R2RML),
        )
        assert {p["prop_uri"] for p in props if p["unmapped"]} == {TIER, PREFERS, member}

    def test_only_attributes_and_relationships_get_the_marker_as_the_designer_check_lists_them(self):
        # Serve offers only owl:DatatypeProperty and owl:ObjectProperty terms, and the designer's check lists
        # only those. A term typed rdf:Property, owl:AnnotationProperty or owl:FunctionalProperty alone is
        # neither, so it gets no marker; a relationship that is also functional is still a relationship.
        note, rates, ref, best = f"{EX}customer_note", f"{EX}rates", f"{EX}uniqueRef", f"{EX}customer_best"
        tbox = TBOX + (
            f"<{note}> a <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> ; rdfs:domain <{CUSTOMER}> .\n"
            f"<{rates}> a owl:AnnotationProperty ; rdfs:range <{ORDER}> .\n"
            f"<{ref}> a owl:FunctionalProperty ; rdfs:domain <{ORDER}> .\n"
            f"<{best}> a owl:ObjectProperty, owl:FunctionalProperty ; rdfs:domain <{CUSTOMER}> ; "
            f"rdfs:range <{ORDER}> .\n"
        )
        g = snapshot.parse_turtle(tbox)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        _, props = ingest.projection_specs(
            g,
            classes,
            obj_props,
            dt_props,
            ingest._class_datasource_map_from_r2rml(R2RML),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(R2RML),
        )
        marked = {p["prop_uri"] for p in props if p["unmapped"]}
        assert marked == {TIER, PREFERS, best}
        listed = checks.structured_check(ops.Draft(g, snapshot.parse_turtle(R2RML)), base_maps_tables=True)
        assert {t["iri"] for t in listed["unmappedProperties"]["terms"]} == marked

    def test_mapped_classes_and_attributes_carry_their_table_and_column_names(self):
        g = snapshot.parse_turtle(TBOX)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        class_specs, props = ingest.projection_specs(
            g,
            classes,
            obj_props,
            dt_props,
            ingest._class_datasource_map_from_r2rml(R2RML),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(R2RML),
            physical_names=ingest._physical_names_from_r2rml(R2RML),
        )
        # Customer and its email attribute keep their table and column although they are labelled otherwise.
        assert {c["class_uri"]: c["physical_name"] for c in class_specs} == {
            CUSTOMER: "customers",
            ORDER: "orders",
            REGION: "",
        }
        # Relationships get no name, nor do attributes no column maps.
        assert {p["prop_uri"]: p["physical_name"] for p in props} == {
            CUSTOMER_ID: "id",
            EMAIL: "email",
            TIER: "",
            PREFERS: "",
            PLACED_BY: "",
            IN_REGION: "",
            REGION_NAME: "",
        }

    def test_a_class_read_from_two_tables_gets_no_name_and_neither_does_one_without_the_names(self):
        second = f"""<{EX}TriplesMap_Client> a rr:TriplesMap ; coa:datasourceId "ds-2" ;
    rr:logicalTable [ rr:tableName "\\"client\\"" ] ;
    rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "k/{{\\"id\\"}}" ] .
"""
        g = snapshot.parse_turtle(TBOX)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        for r2rml, names in ((R2RML + second, ingest._physical_names_from_r2rml(R2RML + second)), (R2RML, None)):
            class_specs, _ = ingest.projection_specs(
                g,
                classes,
                obj_props,
                dt_props,
                ingest._class_datasource_map_from_r2rml(r2rml),
                mapped_predicates=ingest._mapped_predicates_from_r2rml(r2rml),
                physical_names=names,
            )
            customer = next(c for c in class_specs if c["class_uri"] == CUSTOMER)
            assert customer["is_mapped"] is True and customer["physical_name"] == ""

    def test_a_property_with_one_mapped_domain_among_several_is_unmapped_without_a_mapping(self):
        tbox = TBOX + f"<{REGION_NAME}> rdfs:domain <{CUSTOMER}> .\n"
        g = snapshot.parse_turtle(tbox)
        classes, obj_props, dt_props = ingest.schema_subjects(g)
        _, props = ingest.projection_specs(
            g, classes, obj_props, dt_props, ingest._class_datasource_map_from_r2rml(R2RML), mapped_predicates=set()
        )
        assert next(p for p in props if p["prop_uri"] == REGION_NAME)["unmapped"] is True

    @staticmethod
    def _mapped_ends(tbox: str, r2rml: str) -> dict[str, tuple[list[str], list[str]]]:
        g = snapshot.parse_turtle(tbox)
        _, props = ingest.projection_specs(
            g,
            *ingest.schema_subjects(g),
            ingest._class_datasource_map_from_r2rml(r2rml),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(r2rml),
            physical_names=ingest._physical_names_from_r2rml(r2rml),
        )
        return {p["prop_uri"]: (p["mapped_domains"], p["mapped_ranges"]) for p in props}

    def test_a_property_moved_up_to_a_parent_records_where_its_mapping_puts_it(self):
        # The designer allows a domain or range moved up to a parent; the mapping still reads email from
        # customers and joins orders to customers, so deploy records those classes for the NL-to-SQL walk.
        ends = self._mapped_ends(_moved_up(email=True, placed_by=True), R2RML + PARTY_MAP)
        assert ends[EMAIL] == ([CUSTOMER], [])
        assert ends[PLACED_BY] == ([], [CUSTOMER])
        assert {p for p, (domains, ranges) in ends.items() if domains or ranges} == {EMAIL, PLACED_BY}

    def test_a_property_where_its_mapping_puts_it_records_nothing(self):
        # Every mapped term of an unedited induction is where its domain and range say; an unmapped one and
        # an IRI-template relationship (no join to tell its target) record nothing either.
        assert all(ends == ([], []) for ends in self._mapped_ends(TBOX, R2RML).values())


class TestClassDocuments:
    def test_a_relabelled_class_keeps_its_table_and_lists_the_label_as_a_synonym(self):
        doc = _documents()[CUSTOMER]
        assert doc["text"] == "Table: customers | Synonyms: Client, buyer | Columns: id, email"
        assert _context(doc) == (
            "Table: customers | Synonyms: Client, buyer | Primary key: id | "
            "Columns: id:integer, email:string [label: E-mail address]"
        )
        assert doc["data_source_id"] == "ds-1"

    def test_a_relabelled_class_whose_document_lists_nothing_keeps_its_table(self):
        # Every attribute of Customer lost its mapping (deleted in the draft, say): the document still names
        # the table, which the NL-to-SQL parser reads from its first segment, and keeps the label as a synonym.
        r2rml = R2RML.replace(
            f""" ;
    rr:predicateObjectMap [ rr:predicate <{CUSTOMER_ID}> ; rr:objectMap [ rr:column "\\"id\\"" ] ] ,
        [ rr:predicateMap [ rr:constant <{EMAIL}> ] ; rr:objectMap [ rr:column "\\"email\\"" ] ] .""",
            " .",
        )
        assert EMAIL not in r2rml
        doc = _documents(r2rml=r2rml)[CUSTOMER]
        assert doc["text"] == "Table: customers | Synonyms: Client, buyer"
        assert _context(doc) == "Table: customers | Synonyms: Client, buyer | Primary key: id"
        assert doc["data_source_id"] == "ds-1"

    def test_attributes_and_relationships_without_a_mapping_are_left_out(self):
        doc = _documents()[CUSTOMER]
        for text in (doc["text"], _context(doc)):
            assert "loyalty tier" not in text and "preferred order" not in text and "Relationships" not in text

    def test_a_relationship_names_its_range_by_table_and_only_when_the_range_is_mapped(self):
        doc = _documents()[ORDER]
        assert (
            doc["text"]
            == _context(doc)
            == "Table: orders | Synonyms: Purchase | Relationships: customer_id->customers, region->"
        )

    def test_a_relationship_moved_up_to_a_parent_names_the_table_its_join_reads(self):
        # The join still reads customers, whether the parent has no table or a table of its own.
        for r2rml in (R2RML, R2RML + PARTY_MAP):
            doc = _documents(_moved_up(placed_by=True), r2rml)[ORDER]
            assert _context(doc) == (
                "Table: orders | Synonyms: Purchase | Relationships: customer_id->customers, region->"
            )

    def test_an_attribute_moved_up_to_a_parent_stays_on_the_table_its_column_is_in(self):
        # Party is mapped from a table of its own that has no email column: email stays a column of customers.
        documents = _documents(_moved_up(email=True, party_label="parties"), R2RML + PARTY_MAP)
        assert _context(documents[CUSTOMER]) == (
            "Table: customers | Synonyms: Client, buyer | Kind of: parties | Primary key: id | "
            "Columns: id:integer, email:string [label: E-mail address]"
        )
        assert documents[PARTY]["text"] == _context(documents[PARTY]) == "Table: parties"

    def test_the_primary_key_and_a_mapped_parent_are_named_by_their_column_and_table(self):
        # The key attribute is labelled apart from its column, and a mapped subclass of the relabelled
        # Customer is read from its own table.
        vip, level = f"{EX}VipCustomer", f"{EX}vip_level"
        tbox = TBOX.replace('rdfs:label "id"', 'rdfs:label "Customer number"') + (
            f'<{vip}> a owl:Class ; rdfs:label "VIP customer" ; rdfs:subClassOf <{CUSTOMER}> .\n'
            f'<{level}> a owl:DatatypeProperty ; rdfs:label "level" ; rdfs:domain <{vip}> ; rdfs:range xsd:string .\n'
        )
        vip_map = f"""<{EX}TriplesMap_Vip> a rr:TriplesMap ; coa:datasourceId "ds-1" ;
    rr:logicalTable [ rr:tableName "\\"vip_customers\\"" ] ;
    rr:subjectMap [ rr:class <{vip}> ; rr:template "v/{{\\"id\\"}}" ] ;
    rr:predicateObjectMap [ rr:predicate <{level}> ; rr:objectMap [ rr:column "\\"level\\"" ] ] .
"""
        documents = _documents(tbox, R2RML + vip_map)
        assert _context(documents[CUSTOMER]) == (
            "Table: customers | Synonyms: Client, buyer | Primary key: id | "
            "Columns: id:integer [label: Customer number], email:string [label: E-mail address]"
        )
        assert _context(documents[vip]) == (
            "Table: vip_customers | Synonyms: VIP customer | Kind of: customers | Columns: level:string"
        )

    def test_a_column_and_a_join_the_inducer_could_not_map_are_left_out_too(self):
        # R2RML cannot read a column whose name holds a double quote: the inducer declares parents.has"quote and
        # the foreign key from children.parent_id to it, but maps neither. The mapping is the only record of
        # what a table holds, so the documents leave both out, as for a designer's terms, and both get the marker.
        cases = spec_compliance.TestUnsupportedColumnsKeepMappingAndOntologyInStep
        onto, mapping = cases._artifacts(cases.CASES["single-fk-quoted-parent-column"][0])
        tbox, r2rml = onto.serialize(format="turtle"), mapping.serialize(format="turtle")
        documents = _documents(tbox, r2rml)
        prefix = spec_compliance.PREFIX
        assert _context(documents[f"{prefix}Children"]) == "Table: children | Primary key: id | Columns: id:integer"
        assert _context(documents[f"{prefix}Parents"]) == (
            "Table: parents | Primary key: safe_id | Columns: safe_id:integer"
        )
        g = snapshot.parse_turtle(tbox)
        _, props = ingest.projection_specs(
            g,
            *ingest.schema_subjects(g),
            ingest._class_datasource_map_from_r2rml(r2rml),
            mapped_predicates=ingest._mapped_predicates_from_r2rml(r2rml),
        )
        assert {p["prop_uri"] for p in props if p["unmapped"]} == {
            f"{prefix}children_parentId",
            f"{prefix}parents_hasquote",
        }

    def test_the_sql_writer_reads_the_physical_table_name_from_both_texts(self):
        for doc in _documents().values():
            if "data_source_id" in doc:
                table = {CUSTOMER: "customers", ORDER: "orders"}[doc["entity_uri"]]
                assert doc["text"].split(" | ")[0] == _context(doc).split(" | ")[0] == f"Table: {table}"

    def test_an_unmapped_class_document_is_built_from_the_tbox(self):
        doc = _documents()[REGION]
        assert (doc["text"], _context(doc)) == ("Table: Region | Columns: name", "Table: Region | Columns: name:string")
        assert "data_source_id" not in doc

    def test_a_class_mapped_from_two_tables_keeps_its_labels_but_lists_only_mapped_properties(self):
        second = f"""<{EX}TriplesMap_Client> a rr:TriplesMap ; coa:datasourceId "ds-2" ;
    rr:logicalTable [ rr:tableName "\\"client\\"" ] ;
    rr:subjectMap [ rr:class <{CUSTOMER}> ; rr:template "k/{{\\"id\\"}}" ] .
"""
        doc = _documents(r2rml=R2RML + second)[CUSTOMER]
        assert _context(doc) == (
            "Table: Client | Synonyms: buyer | Primary key: id | Columns: id:integer, E-mail address:string"
        )

    def test_without_a_mapping_the_documents_are_built_from_the_tbox_alone(self):
        doc = _documents(physical=False)[CUSTOMER]
        assert _context(doc) == (
            "Table: Client | Synonyms: buyer | Primary key: id | Relationships: preferred order->Purchase | "
            "Columns: id:integer, E-mail address:string, loyalty tier:string"
        )
