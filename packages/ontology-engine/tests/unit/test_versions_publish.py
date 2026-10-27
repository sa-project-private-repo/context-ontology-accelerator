# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for publishing ontology versions (coa_ontology.versions.publish)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology import dynamo_store
from coa_ontology.proposals import SchemaSqlCoverageError, SchemaSqlGenerationError
from coa_ontology.versions import publish, registry, snapshot, storage
from rdflib import URIRef

pytestmark = pytest.mark.unit

NS = "ns-1"
ONTO = "https://example.com/onto"
OTHER_ONTO = "https://other.example/onto"
EX = "https://example.com/onto#"

TTL_A = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology ; rdfs:label "Hospital" .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" .
"""
TTL_B = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology .
<{EX}Doctor> a owl:Class ; rdfs:label "Doctor" .
"""
R2RML_A = f"""@prefix rr: <http://www.w3.org/ns/r2rml#> .
<{EX}PatientMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "patient" ] ;
    rr:subjectMap [ rr:class <{EX}Patient> ; rr:template "p/{{id}}" ] .
"""
TTL_PATIENT_VISIT = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ONTO}> a owl:Ontology .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" .
<{EX}Visit> a owl:Class ; rdfs:label "Visit" .
"""
LIVE = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix wb: <https://ontology-workbench.local/vocab#> .
<{ONTO}> a owl:Ontology ; dct:created "2026-01-01" .
<{EX}Patient> a owl:Class ; rdfs:label "Patient" ; wb:definedBy <{ONTO}> .
"""


def _shape(name: str) -> str:
    return f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}{name}Shape> a sh:NodeShape ; sh:targetClass <{EX}{name}> ;
    sh:property [ sh:path <{EX}name> ; sh:minCount 1 ] ."""


def _req(turtle: str = TTL_A, r2rml: str = R2RML_A, proposal: str = "p-a", **kw) -> publish.PublishRequest:
    return publish.PublishRequest(
        namespace=kw.pop("namespace", NS),
        ontology_id=kw.pop("ontology_id", ONTO),
        ontology_turtle=turtle,
        r2rml_turtle=r2rml,
        source_proposal_id=proposal,
        created_by="alice",
        datasource_ids=kw.pop("datasource_ids", ["ds-1"]),
        **kw,
    )


def _ds_r2rml(*maps: tuple[str, str], joins: dict[str, str] | None = None) -> str:
    """R2RML for ``(class name, datasource id)`` maps on table ``name``; ``joins`` maps a name to its parent map."""
    body = f"@prefix rr: <http://www.w3.org/ns/r2rml#> .\n@prefix coa: <{VOCAB_URI}> .\n"
    for name, datasource in maps:
        join = ""
        if joins and name in joins:
            join = (
                f" ; rr:predicateObjectMap [ rr:predicate <{EX}of{joins[name]}> ;"
                f" rr:objectMap [ rr:parentTriplesMap <{EX}{joins[name]}Map> ] ]"
            )
        body += (
            f'<{EX}{name}Map> a rr:TriplesMap ; coa:datasourceId "{datasource}" ;\n'
            f'    rr:logicalTable [ rr:tableName "{name.lower()}" ] ;\n'
            f'    rr:subjectMap [ rr:class <{EX}{name}> ; rr:template "{name.lower()}/{{id}}" ]{join} .\n'
        )
    return body


def _mapped_tables(version: int) -> set[str]:
    r2rml = snapshot.parse_turtle(storage.require_version_file(NS, version, "mappings.r2rml"))
    return {str(table) for table in r2rml.objects(None, snapshot.RR.tableName)}


def _version_key(version: int) -> dict[str, str]:
    return {"PK": dynamo_store._namespace_pk(NS), "SK": f"VERSION#{version:06d}"}


def _sweep(aws, version: int) -> None:
    """Age a PUBLISHING row past the stale threshold and let a reader sweep it, as a concurrent poll would."""
    aws.table.update_item(
        Key=_version_key(version),
        UpdateExpression="SET updated_at = :old",
        ExpressionAttributeValues={":old": "2000-01-01T00:00:00+00:00"},
    )
    assert registry.get_version(NS, version)["status"] == registry.PUBLISH_FAILED


def _fail_row(aws, version: int) -> None:
    """Move a PUBLISHING row to PUBLISH_FAILED directly, as a sweep that already won would have."""
    aws.table.update_item(
        Key=_version_key(version),
        UpdateExpression="SET #s = :failed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":failed": registry.PUBLISH_FAILED},
    )


def _overwrite(aws, version: int, name: str, body: str) -> None:
    """Replace a snapshot object in place (bypassing the create-only writer) to simulate damage."""
    aws.s3.put_object(Bucket=aws.bucket, Key=storage.version_key(NS, version, name), Body=body.encode())


def _published() -> list[int]:
    return [v["version"] for v in registry.list_versions(NS) if v["status"] == registry.PUBLISHED]


def _seed_legacy(aws, mappings: str = R2RML_A) -> None:
    dynamo_store.put_ontology_registry(
        NS, ONTO, {"uri": ONTO, "ontology_type": "induced", "source_proposals": ["p-old"]}
    )
    aws.s3.put_object(Bucket=aws.bucket, Key=f"ontologies/{NS}/latest/mappings.r2rml", Body=mappings.encode())


@contextmanager
def _shapes(shacl: str) -> Iterator[None]:
    with (
        patch("coa_ontology.validation.shapes.config.ConstraintConfig", side_effect=lambda **kw: kw),
        patch("coa_ontology.validation.shapes.config.compile_to_shacl", return_value=shacl),
    ):
        yield


class _Gate:
    """Holds a background publish inside its first ``write_snapshot`` until a foreground publish waits behind it."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail
        self.errors: list[Exception] = []
        self._held = False
        self._write = storage.write_snapshot

    def write(self, namespace, version, files, manifest_json):
        if not self._held:
            self._held = True
            self.entered.set()
            assert self.release.wait(10)
            if self.fail is not None:
                raise self.fail
        self._write(namespace, version, files, manifest_json)

    def progress(self, stage: str) -> None:
        if stage == "waiting":
            self.release.set()

    @contextmanager
    def holding(self, req: publish.PublishRequest) -> Iterator[None]:
        """Run ``publish_version(req)`` in a thread and yield once it holds its PUBLISHING row."""

        def run() -> None:
            try:
                publish.publish_version(req)
            except Exception as e:
                self.errors.append(e)

        with (
            patch.object(storage, "write_snapshot", side_effect=self.write),
            patch.object(publish, "_RESERVE_POLL_SECONDS", 0.01),
        ):
            thread = threading.Thread(target=run)
            thread.start()
            assert self.entered.wait(10)
            try:
                yield
            finally:
                self.release.set()
                thread.join(10)


@pytest.fixture(autouse=True)
def _schema_sql():
    with patch(
        "coa_ontology.proposals._generate_schema_sql", return_value='CREATE TABLE "patient" ("id" INT);'
    ) as generate:
        yield generate


@pytest.fixture
def _live():
    with patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", return_value=LIVE) as gsp:
        yield gsp


class TestPublish:
    def test_first_publish_writes_complete_snapshot_and_published_row(self, versions_aws):
        assert publish.publish_version(_req()) == 1
        manifest = storage.read_manifest(NS, 1)
        assert set(manifest["files"]) == {
            "ontology.ttl",
            "mappings.r2rml",
            "vkg/ontology.ttl",
            "vkg/schema.sql",
            "diff.json",
        }
        assert manifest["parentVersion"] is None and manifest["sourceProposals"] == ["p-a"]
        assert '"v1"' in storage.read_version_file(NS, 1, "ontology.ttl")
        row = registry.get_version(NS, 1)
        assert row["status"] == registry.PUBLISHED and row["diff_summary"]["classes"]["added"] == 1

    def test_records_the_accepts_title_on_the_version_row(self, versions_aws):
        assert publish.publish_version(_req(title="Plant")) == 1
        assert registry.get_version(NS, 1)["title"] == "Plant"

    def test_a_publish_without_a_title_records_none(self, versions_aws):
        assert publish.publish_version(_req()) == 1
        assert "title" not in registry.get_version(NS, 1)

    def test_second_publish_merges_onto_parent(self, versions_aws):
        publish.publish_version(_req())
        assert publish.publish_version(_req(TTL_B, "", "p-b")) == 2
        onto = snapshot.parse_turtle(storage.read_version_file(NS, 2, "ontology.ttl"))
        classes, _, _ = snapshot.schema_subjects(onto)
        assert {str(c) for c in classes} == {EX + "Patient", EX + "Doctor"}
        manifest = storage.read_manifest(NS, 2)
        assert manifest["parentVersion"] == 1 and manifest["sourceProposals"] == ["p-a", "p-b"]
        assert storage.read_version_file(NS, 2, "mappings.r2rml") == storage.read_version_file(NS, 1, "mappings.r2rml")
        assert storage.read_manifest(NS, 2)["files"]["diff.json"]

    def test_same_inputs_produce_identical_file_checksums(self, versions_aws):
        publish.publish_version(_req(namespace="ns-a"))
        publish.publish_version(_req(namespace="ns-b"))
        a, b = storage.read_manifest("ns-a", 1)["files"], storage.read_manifest("ns-b", 1)["files"]
        for name in ("ontology.ttl", "mappings.r2rml", "vkg/ontology.ttl", "diff.json"):
            assert a[name]["sha256"] == b[name]["sha256"], name

    def test_invalid_turtle_marks_version_failed(self, versions_aws):
        with pytest.raises(snapshot.SnapshotValidationError):
            publish.publish_version(_req("not turtle <<<"))
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED

    def test_mapping_to_unknown_class_is_rejected(self, versions_aws):
        with pytest.raises(snapshot.SnapshotValidationError, match="missing from the ontology"):
            publish.publish_version(_req(TTL_B, R2RML_A))

    def test_different_ontology_id_than_head_is_rejected(self, versions_aws):
        publish.publish_version(_req())
        with pytest.raises(snapshot.SnapshotValidationError, match="belong to ontology"):
            publish.publish_version(_req(ontology_id=OTHER_ONTO))

    def test_constraint_config_compiles_to_shapes(self, versions_aws):
        shacl = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}PatientShape> a sh:NodeShape ; sh:targetClass <{EX}Patient> ."""
        with (
            patch("coa_ontology.validation.shapes.config.ConstraintConfig", side_effect=lambda **kw: kw),
            patch("coa_ontology.validation.shapes.config.compile_to_shacl", return_value=shacl),
        ):
            publish.publish_version(_req(constraint_config={"classes": []}))
        assert "PatientShape" in storage.read_version_file(NS, 1, "shapes.ttl")

    def test_invalid_constraint_config_is_a_validation_error(self, versions_aws):
        with pytest.raises(snapshot.SnapshotValidationError, match="constraint config"):
            publish.publish_version(_req(constraint_config={"classes": "not a list"}))
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED

    def test_waiter_for_another_ontology_queued_behind_the_first_version_is_rejected(self, versions_aws):
        # The head check passes while v1 of ONTO is still PUBLISHING; the parent check catches it.
        gate = _Gate()
        with gate.holding(_req()), pytest.raises(snapshot.SnapshotValidationError, match="belong to ontology"):
            publish.publish_version(_req(ontology_id=OTHER_ONTO), on_progress=gate.progress)
        assert not gate.errors
        assert _published() == [1]
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED


def _datatype_shape(xsd_type: str) -> str:
    return f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}PatientShape> a sh:NodeShape ; sh:targetClass <{EX}Patient> ;
    sh:property [ sh:path <{EX}age> ; sh:datatype <http://www.w3.org/2001/XMLSchema#{xsd_type}> ] ."""


class TestShapes:
    def test_reinduction_changing_a_column_datatype_leaves_one_datatype(self, versions_aws):
        with _shapes(_datatype_shape("string")):
            publish.publish_version(_req(constraint_config={"classes": []}))
        with _shapes(_datatype_shape("integer")):
            publish.publish_version(_req(TTL_A, "", "p-b", constraint_config={"classes": []}))
        shapes = snapshot.parse_turtle(storage.read_version_file(NS, 2, "shapes.ttl"))
        datatypes = list(shapes.objects(None, URIRef("http://www.w3.org/ns/shacl#datatype")))
        assert datatypes == [URIRef("http://www.w3.org/2001/XMLSchema#integer")]

    def test_shapes_accumulate_across_versions_without_duplicates(self, versions_aws):
        with _shapes(_shape("Patient")):
            publish.publish_version(_req(constraint_config={"classes": []}))
        publish.publish_version(_req(TTL_B, "", "p-b"))
        assert storage.read_version_file(NS, 2, "shapes.ttl") == storage.read_version_file(NS, 1, "shapes.ttl")
        with _shapes(_shape("Patient")):
            publish.publish_version(_req(TTL_A, "", "p-c", constraint_config={"classes": []}))
        assert storage.read_version_file(NS, 3, "shapes.ttl") == storage.read_version_file(NS, 1, "shapes.ttl")
        with _shapes(_shape("Doctor")):
            publish.publish_version(_req(TTL_B, "", "p-d", constraint_config={"classes": []}))
        shapes = snapshot.parse_turtle(storage.read_version_file(NS, 4, "shapes.ttl"))
        sh = "http://www.w3.org/ns/shacl#"
        assert {str(s) for s in shapes.subjects(None, None) if str(s).startswith(EX)} == {
            EX + "PatientShape",
            EX + "DoctorShape",
        }
        assert len(list(shapes.objects(None, URIRef(sh + "property")))) == 2


class TestSchemaSql:
    def test_schema_sql_uses_only_the_snapshot_datasources(self, versions_aws, _schema_sql):
        publish.publish_version(_req(TTL_A, _ds_r2rml(("Patient", "DS#ds-3")), datasource_ids=["DS#ds-1"]))
        assert _schema_sql.call_args.kwargs == {"extra_datasource_ids": ["ds-1", "ds-3"], "scan_accepted": False}
        publish.publish_version(_req(TTL_B, _ds_r2rml(("Doctor", "ds-2")), "p-b", datasource_ids=["ds-2"]))
        args, kwargs = _schema_sql.call_args
        assert args == (NS, ONTO, storage.read_version_file(NS, 2, "mappings.r2rml"))
        assert kwargs == {"extra_datasource_ids": ["ds-2", "ds-3"], "scan_accepted": False}

    def test_schema_sql_generation_error_fails_the_publish(self, versions_aws, _schema_sql):
        _schema_sql.side_effect = SchemaSqlGenerationError("catalog unavailable")
        with pytest.raises(SchemaSqlGenerationError, match="catalog unavailable") as exc:
            publish.publish_version(_req())
        assert not isinstance(exc.value, publish.PublishError)  # transient: its own message, accept again
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED

    def test_a_mapped_table_missing_from_the_catalog_fails_the_publish_with_remedies(self, versions_aws, _schema_sql):
        _schema_sql.side_effect = SchemaSqlCoverageError("no matching physical table", missing=['"doctor"'])
        with pytest.raises(publish.PublishError) as exc:
            publish.publish_version(_req())
        assert str(exc.value) == (
            'Mapped table(s) "doctor" are not available in the source catalog (removed, not approved, '
            "or temporarily unreachable). If the source is available, retry the accept; otherwise re-induce "
            "the datasource, or delete the domain ontology to drop its mappings."
        )
        assert isinstance(exc.value.__cause__, SchemaSqlCoverageError)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert storage.read_manifest(NS, 1) is None

    def test_a_long_missing_table_list_is_capped_so_the_remedies_fit(self, versions_aws, _schema_sql):
        missing = [f'"public__2f77729f"."table_{i:02d}"' for i in range(40)]
        _schema_sql.side_effect = SchemaSqlCoverageError("no matching physical table", missing=missing)
        with pytest.raises(publish.PublishError) as exc:
            publish.publish_version(_req())
        message = str(exc.value)
        assert '"table_09"' in message and '"table_10"' not in message
        assert " and 30 more are not available" in message
        assert len(message) < 1000 and message.endswith("to drop its mappings.")


class TestAuthoritativeDatasources:
    def _publish_parent(self) -> None:
        publish.publish_version(_req(TTL_PATIENT_VISIT, _ds_r2rml(("Patient", "ds-1"), ("Visit", "ds-1"))))
        assert _mapped_tables(1) == {"patient", "visit"}

    def test_authoritative_datasource_drops_parent_maps_the_proposal_omits(self, versions_aws):
        self._publish_parent()
        publish.publish_version(_req(TTL_A, _ds_r2rml(("Patient", "ds-1")), "p-b", authoritative_datasources=["ds-1"]))
        assert _mapped_tables(2) == {"patient"}

    def test_without_authoritative_datasources_omitted_parent_maps_are_kept(self, versions_aws):
        self._publish_parent()
        publish.publish_version(_req(TTL_A, _ds_r2rml(("Patient", "ds-1")), "p-b"))
        assert _mapped_tables(2) == {"patient", "visit"}

    def test_prefixed_authoritative_id_matches_bare_map_ids(self, versions_aws):
        self._publish_parent()
        publish.publish_version(
            _req(TTL_A, _ds_r2rml(("Patient", "ds-1")), "p-b", authoritative_datasources=["DS#ds-1"])
        )
        assert _mapped_tables(2) == {"patient"}

    def test_authoritative_datasource_absent_from_the_proposal_keeps_its_parent_maps(self, versions_aws):
        # ds-1 is listed but the proposal carries none of its maps (a failed catalog fetch, say),
        # so its parent maps must survive; ds-2 is mapped and stays authoritative.
        self._publish_parent()
        publish.publish_version(
            _req(TTL_B, _ds_r2rml(("Doctor", "ds-2")), "p-b", authoritative_datasources=["ds-1", "ds-2"])
        )
        assert _mapped_tables(2) == {"patient", "visit", "doctor"}

    def test_dangling_join_names_the_dropped_maps_datasource(self, versions_aws):
        parent_r2rml = _ds_r2rml(("Patient", "ds-1"), ("Visit", "ds-2"), joins={"Visit": "Patient"})
        publish.publish_version(_req(TTL_PATIENT_VISIT, parent_r2rml))
        with pytest.raises(
            snapshot.SnapshotValidationError, match=r"PatientMap \(datasource ds-1\) is no longer mapped"
        ):
            publish.publish_version(
                _req(TTL_B, _ds_r2rml(("Doctor", "ds-1")), "p-b", authoritative_datasources=["ds-1"])
            )
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED


class TestParentSnapshot:
    @pytest.mark.parametrize("name", ["ontology.ttl", "mappings.r2rml", "shapes.ttl"])
    def test_missing_parent_file_is_a_state_error(self, versions_aws, name):
        with _shapes(_shape("Patient")):
            publish.publish_version(_req(constraint_config={"classes": []}))
        versions_aws.s3.delete_object(Bucket=versions_aws.bucket, Key=storage.version_key(NS, 1, name))
        with pytest.raises(publish.PublishStateError) as raised:
            publish.publish_version(_req(TTL_B, "", "p-b"))
        assert isinstance(raised.value.__cause__, storage.SnapshotFileMissingError)
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED

    @pytest.mark.parametrize("damage", ["delete", "malformed"])
    def test_missing_or_malformed_parent_manifest_is_a_state_error(self, versions_aws, damage):
        publish.publish_version(_req())
        if damage == "delete":
            versions_aws.s3.delete_object(Bucket=versions_aws.bucket, Key=storage.version_key(NS, 1, storage.MANIFEST))
        else:
            _overwrite(versions_aws, 1, storage.MANIFEST, '{"files": []}')
        with pytest.raises(publish.PublishStateError) as raised:
            publish.publish_version(_req(TTL_B, "", "p-b"))
        assert isinstance(raised.value.__cause__, storage.SnapshotIncompleteError)
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED

    def test_unparseable_parent_file_is_a_state_error(self, versions_aws):
        publish.publish_version(_req())
        _overwrite(versions_aws, 1, "ontology.ttl", "not turtle <<<")
        with pytest.raises(publish.PublishStateError, match="ontology.ttl"):
            publish.publish_version(_req(TTL_B, "", "p-b"))
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED

    def test_inconsistency_inherited_from_the_parent_is_a_state_error(self, versions_aws):
        publish.publish_version(_req())
        ghost = R2RML_A.replace(f"{EX}Patient>", f"{EX}Ghost>")
        _overwrite(versions_aws, 1, "mappings.r2rml", snapshot.canonical_nt(snapshot.parse_turtle(ghost)))
        with pytest.raises(publish.PublishStateError, match="v1 is itself inconsistent"):
            publish.publish_version(_req(TTL_B, "", "p-b"))
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED

    def test_parent_without_mappings_contributes_none(self, versions_aws):
        publish.publish_version(_req(TTL_A, ""))
        assert "mappings.r2rml" not in storage.read_manifest(NS, 1)["files"]
        publish.publish_version(_req(TTL_A, R2RML_A, "p-b"))
        assert storage.read_version_file(NS, 2, "mappings.r2rml") == snapshot.canonical_nt(
            snapshot.parse_turtle(R2RML_A)
        )

    def test_existing_snapshot_objects_are_busy_and_a_retry_takes_the_next_number(self, versions_aws):
        versions_aws.s3.put_object(Bucket=versions_aws.bucket, Key=storage.version_key(NS, 1, "diff.json"), Body=b"{}")
        with pytest.raises(publish.PublishBusyError) as raised:
            publish.publish_version(_req())
        assert isinstance(raised.value.__cause__, storage.SnapshotExistsError)
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert publish.publish_version(_req()) == 2


class TestRowLifecycle:
    def test_row_swept_before_publish_finishes_raises_busy_error(self, versions_aws):
        write = storage.write_snapshot

        def write_then_get_swept(namespace, version, files, manifest_json):
            write(namespace, version, files, manifest_json)
            _sweep(versions_aws, version)

        with (
            patch.object(storage, "write_snapshot", side_effect=write_then_get_swept),
            patch.object(registry, "mark_publish_failed", wraps=registry.mark_publish_failed) as mark_failed,
            pytest.raises(publish.PublishBusyError, match="v1 was swept as stale"),
        ):
            publish.publish_version(_req())
        mark_failed.assert_not_called()
        row = registry.get_version(NS, 1)
        assert row["status"] == registry.PUBLISH_FAILED and "did not complete" in row["error"]

    def test_failure_after_sweep_raises_the_original_error(self, versions_aws):
        def swept_then_failing_write(namespace, version, files, manifest_json):
            _sweep(versions_aws, version)
            raise RuntimeError("S3 unavailable")

        with (
            patch.object(storage, "write_snapshot", side_effect=swept_then_failing_write),
            pytest.raises(RuntimeError, match="S3 unavailable"),
        ):
            publish.publish_version(_req())
        assert "did not complete" in registry.get_version(NS, 1)["error"]

    def test_lost_mark_published_response_counts_as_published(self, versions_aws):
        mark_published = registry.mark_published

        def landed_but_response_lost(*args, **kwargs):
            mark_published(*args, **kwargs)
            raise registry.VersionStateError("version 1 is no longer PUBLISHING")

        with patch.object(registry, "mark_published", side_effect=landed_but_response_lost):
            assert publish.publish_version(_req()) == 1
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHED

    def test_reserve_gives_up_with_a_busy_error(self, versions_aws):
        registry.reserve_next_version(NS, ontology_id=ONTO, source="PROPOSAL", source_proposal_id="p-x", created_by="x")
        progress: list[str] = []
        with (
            patch.object(publish, "_RESERVE_WAIT_SECONDS", 0.05),
            patch.object(publish, "_RESERVE_POLL_SECONDS", 0.01),
            pytest.raises(publish.PublishBusyError, match="still running"),
        ):
            publish.publish_version(_req(), on_progress=progress.append)
        assert progress and set(progress) == {"waiting"}

    def test_heartbeat_keeps_a_long_publish_from_being_swept(self, versions_aws, _schema_sql):
        def slow_schema_sql(*args, **kwargs):
            time.sleep(1.3)
            # A concurrent poll reads the row; with no heartbeat it would be past the stale threshold.
            assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING
            return 'CREATE TABLE "patient" ("id" INT);'

        _schema_sql.side_effect = slow_schema_sql
        progress: list[str] = []
        with patch.object(registry, "PUBLISH_STALE_SECONDS", 1), patch.object(publish, "_HEARTBEAT_SECONDS", 0.1):
            assert publish.publish_version(_req(), on_progress=progress.append) == 1
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHED
        assert "publishing" in progress

    def test_failing_progress_callback_does_not_stop_the_heartbeat(self, versions_aws, _schema_sql, caplog):
        def slow_schema_sql(*args, **kwargs):
            time.sleep(1.3)
            assert registry.get_version(NS, 1)["status"] == registry.PUBLISHING
            return 'CREATE TABLE "patient" ("id" INT);'

        def broken_progress(stage: str) -> None:
            raise RuntimeError("progress store unavailable")

        _schema_sql.side_effect = slow_schema_sql
        with patch.object(registry, "PUBLISH_STALE_SECONDS", 1), patch.object(publish, "_HEARTBEAT_SECONDS", 0.1):
            assert publish.publish_version(_req(), on_progress=broken_progress) == 1
        assert "progress callback failed" in caplog.text

    def test_heartbeat_that_finds_the_row_swept_stops_before_writing(self, versions_aws, _schema_sql):
        def swept_meanwhile(*args, **kwargs):
            _fail_row(versions_aws, 1)
            time.sleep(0.5)
            return 'CREATE TABLE "patient" ("id" INT);'

        _schema_sql.side_effect = swept_meanwhile
        with (
            patch.object(publish, "_HEARTBEAT_SECONDS", 0.05),
            pytest.raises(publish.PublishBusyError, match="swept"),
        ):
            publish.publish_version(_req())
        assert storage.read_manifest(NS, 1) is None


class TestBootstrap:
    def test_legacy_namespace_is_captured_as_active_v1_before_first_publish(self, versions_aws, _live):
        _seed_legacy(versions_aws)
        assert publish.publish_version(_req(TTL_B, "", "p-b")) == 2
        v1 = storage.read_manifest(NS, 1)
        assert v1["source"] == publish.SOURCE_BOOTSTRAP and v1["sourceProposals"] == ["p-old"]
        assert "ontology-workbench.local/vocab" not in storage.read_version_file(NS, 1, "ontology.ttl")
        assert registry.get_active(NS)["version"] == 1
        assert storage.read_manifest(NS, 2)["parentVersion"] == 1

    def test_new_namespace_has_no_bootstrap(self, versions_aws):
        assert publish.publish_version(_req()) == 1
        assert storage.read_manifest(NS, 1)["source"] == publish.SOURCE_PROPOSAL
        assert registry.get_active(NS) is None

    def test_bootstrap_superseded_by_a_concurrent_publish_burns_no_version(self, versions_aws):
        # Another worker bootstraps v1 and publishes v2 while this one is still reading
        # the live graph. Bootstrapping again would make the stale live graph the parent
        # of this publish and lose v2's proposal, so the late bootstrap gives up.
        _seed_legacy(versions_aws)
        reads: list[str] = []

        def live_graph(graph_uri: str) -> str:
            reads.append(graph_uri)
            if len(reads) == 1:
                publish.publish_version(_req(TTL_B, "", "p-other"))
            return LIVE

        with patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", side_effect=live_graph):
            assert publish.publish_version(_req(TTL_A, "", "p-b")) == 3
        assert _published() == [3, 2, 1]
        assert storage.read_manifest(NS, 3)["parentVersion"] == 2
        assert registry.get_active(NS)["version"] == 1

    @pytest.mark.parametrize(
        ("live", "mappings", "match"),
        [
            (LIVE, R2RML_A.replace(f"{EX}Patient>", f"{EX}Ghost>"), "missing from the ontology"),
            ("not turtle <<<", R2RML_A, "Invalid live ontology"),
            (LIVE, "not turtle <<<", "Invalid latest/ R2RML"),
            (None, R2RML_A, "no graph"),
        ],
    )
    def test_inconsistent_legacy_ontology_fails_without_reserving(self, versions_aws, live, mappings, match):
        _seed_legacy(versions_aws, mappings)
        with (
            patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", return_value=live),
            pytest.raises(publish.BootstrapError, match=match),
        ):
            publish.publish_version(_req(TTL_B, "", "p-b"))
        assert registry.list_versions(NS) == []
        assert issubclass(publish.BootstrapError, publish.PublishStateError)

    def test_active_row_written_meanwhile_abandons_the_bootstrap(self, versions_aws, _live):
        # The ontology was deleted (ACTIVE reset) while v1 was being written.
        _seed_legacy(versions_aws)
        write = storage.write_snapshot

        def write_then_reset(namespace, version, files, manifest_json):
            write(namespace, version, files, manifest_json)
            registry.reset_active(namespace)

        with patch.object(storage, "write_snapshot", side_effect=write_then_reset):
            assert publish.bootstrap_if_needed(NS, ONTO) is None
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert registry.get_active(NS)["version"] is None

    @pytest.mark.parametrize("lost", [registry.VersionStateError, registry.ActiveExistsError])
    def test_lost_bootstrap_commit_response_counts_as_committed(self, versions_aws, _live, lost):
        _seed_legacy(versions_aws)
        commit = registry.commit_bootstrap_version

        def landed_but_response_lost(*args, **kwargs):
            commit(*args, **kwargs)
            raise lost("the retried transaction found its own write")

        with patch.object(registry, "commit_bootstrap_version", side_effect=landed_but_response_lost):
            assert publish.bootstrap_if_needed(NS, ONTO) == 1
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISHED
        assert registry.get_active(NS)["version"] == 1

    def test_publish_behind_a_failed_bootstrap_does_not_publish_parentless(self, versions_aws, _live):
        # Worker A reserved v1 for the bootstrap and then failed; worker B waited behind it.
        _seed_legacy(versions_aws)
        gate = _Gate(fail=RuntimeError("S3 unavailable"))
        with (
            gate.holding(_req(TTL_A, "", "p-a")),
            pytest.raises(publish.BootstrapPendingError, match="not captured as v1 yet"),
        ):
            publish.publish_version(_req(TTL_B, "", "p-b"), on_progress=gate.progress)
        assert issubclass(publish.BootstrapPendingError, publish.PublishBusyError)
        assert [type(e) for e in gate.errors] == [RuntimeError]
        assert _published() == []
        assert registry.get_version(NS, 1)["status"] == registry.PUBLISH_FAILED
        assert registry.get_version(NS, 2)["status"] == registry.PUBLISH_FAILED
        # The next accept captures v1 and publishes on top of it.
        assert publish.publish_version(_req(TTL_B, "", "p-b")) == 4
        assert storage.read_manifest(NS, 4)["parentVersion"] == 3
