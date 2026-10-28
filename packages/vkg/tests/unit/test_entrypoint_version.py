# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ontology version extraction in entrypoint.sh.

Published snapshots are canonical N-Triples (full IRIs:
``<http://www.w3.org/2002/07/owl#versionInfo> "v3"``) while hand-written Turtle
uses the ``owl:versionInfo "v2"`` prefix form. entrypoint.sh must read both with
a portable ``sed -E`` (the old GNU-only ``grep -oP`` returned nothing for the
IRI form). These tests execute the REAL lines pulled from the script, under the
same ``set -euo pipefail`` the entrypoint uses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"

_EXTRACT_PREFIX = "ONTOLOGY_VERSION=$(sed "
_FALLBACK_PREFIX = "ONTOLOGY_VERSION=${ONTOLOGY_VERSION:-"

_NTRIPLES = '<https://e/o> <http://www.w3.org/2002/07/owl#versionInfo> "v3" .\n'
_TURTLE = '@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<https://e/o> owl:versionInfo "v2" .\n'


def _line(prefix: str) -> str:
    """Extract the exact line starting with `prefix` from entrypoint.sh."""
    for line in _ENTRYPOINT.read_text().splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting with {prefix!r} found in entrypoint.sh")


def _run(ontology: Path, version: str = "latest") -> str:
    """Run the real extraction + fallback lines against `ontology`."""
    script = "\n".join(
        [
            "set -euo pipefail",
            _line(_EXTRACT_PREFIX),
            _line(_FALLBACK_PREFIX),
            'printf "%s" "${ONTOLOGY_VERSION}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", "ONTOLOGY_FILE": str(ontology), "VERSION": version},
        capture_output=True,
        text=True,
        check=True,  # a non-zero exit (set -e abort) fails the test
    )
    return result.stdout


def _resolve(ontology_text: str, tmp_path: Path, version: str = "latest") -> str:
    ontology = tmp_path / "ontology.ttl"
    ontology.write_text(ontology_text)
    return _run(ontology, version)


class TestOntologyVersionExtraction:
    def test_reads_full_iri_form_from_ntriples(self, tmp_path):
        assert _resolve(_NTRIPLES, tmp_path) == "v3"

    def test_reads_prefixed_form_from_turtle(self, tmp_path):
        assert _resolve(_TURTLE, tmp_path) == "v2"

    def test_stops_at_semicolon_in_turtle_predicate_lists(self, tmp_path):
        text = '<https://e/o> owl:versionInfo "v7" ;\n    rdfs:label "x" .\n'
        assert _resolve(text, tmp_path) == "v7"

    def test_first_match_wins(self, tmp_path):
        assert _resolve(_NTRIPLES + _TURTLE, tmp_path) == "v3"

    def test_falls_back_to_version_env_when_no_version_info(self, tmp_path):
        text = '<https://e/o> <http://www.w3.org/2000/01/rdf-schema#label> "x" .\n'
        assert _resolve(text, tmp_path, version="latest") == "latest"

    def test_many_matches_do_not_abort_under_pipefail(self, tmp_path):
        # head -n1 closes the pipe early; with enough matching lines sed dies on
        # SIGPIPE (exit 141). Under set -euo pipefail that must not abort the
        # entrypoint, and the first value must still be returned.
        line = '<https://e/c{i}> <http://www.w3.org/2002/07/owl#versionInfo> "v{i}" .\n'
        text = "".join(line.format(i=i) for i in range(200_000))
        assert _resolve(text, tmp_path) == "v0"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
    def test_unreadable_file_falls_back_to_version_env(self, tmp_path):
        ontology = tmp_path / "ontology.ttl"
        ontology.write_text(_NTRIPLES)
        ontology.chmod(0o000)
        try:
            assert _run(ontology, version="latest") == "latest"
        finally:
            ontology.chmod(0o600)

    def test_does_not_use_gnu_only_grep_p(self):
        # grep -P is unavailable on BSD/macOS and returned nothing for N-Triples.
        assert "grep -oP" not in _ENTRYPOINT.read_text()
