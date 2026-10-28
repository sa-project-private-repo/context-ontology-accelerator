# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Ontology designer's coded sentences: the engine's English and the web app's table must agree.

The engine sends a check finding, a data or structured check error, a history line and a change
warning as a stable code next to its English text. The web app writes the code's catalogue entry
instead, from its own table of codes and English sentences (``studio-messages.ts``), so a code the
engine adds without the table, or a sentence it rewords, would leave the web app silently writing
English for that code, or an older sentence. Both sides are read as source text, so neither
package needs importing here.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).parent.parent.parent
STUDIO = REPO_ROOT / "packages" / "ontology-engine" / "src" / "coa_ontology" / "studio"
SENTENCES_TS = REPO_ROOT / "packages" / "web-app" / "src" / "pages" / "ontology" / "studio" / "studio-messages.ts"


def _module_assignments(path: Path) -> dict[str, ast.expr]:
    """The module-level ``NAME = value`` assignments of a Python file, by name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _engine_sentences() -> dict[str, str]:
    """Every code the engine sends, with its English: ``messages.ENGLISH`` and ``checks.ERROR_CODES``."""
    english = ast.literal_eval(_module_assignments(STUDIO / "messages.py")["ENGLISH"])
    checks = _module_assignments(STUDIO / "checks.py")
    error_codes = checks["ERROR_CODES"]
    assert isinstance(error_codes, ast.Dict)
    for key, value in zip(error_codes.keys, error_codes.values, strict=True):
        assert isinstance(key, ast.Name) and isinstance(value, ast.Constant), ast.dump(key)
        english[value.value] = ast.literal_eval(checks[key.id])
    return english


def _web_sentences() -> dict[str, str]:
    """The ``SENTENCES`` table of ``studio-messages.ts``: code to English sentence."""
    text = SENTENCES_TS.read_text(encoding="utf-8")
    table = re.search(r"const SENTENCES\b.*?\(\[(.*?)\n\]\);", text, re.S)
    assert table, "SENTENCES not found in studio-messages.ts"
    entries = re.findall(r'\[\s*"([A-Z][A-Z0-9_]*)",\s*"((?:[^"\\]|\\.)*)",?\s*\]', table.group(1))
    return {code: json.loads(f'"{sentence}"') for code, sentence in entries}


def _as_the_web_writes_it(english: str) -> str:
    """``{label!r}`` quotes the label as Python's repr does, which the web app writes as ``'{label}'``."""
    return re.sub(r"\{(\w+)!r\}", r"'{\1}'", english)


def test_the_web_app_knows_every_code_the_engine_sends_and_no_other():
    engine, web = _engine_sentences(), _web_sentences()
    assert len(engine) > 40, "the engine's sentences were not read"
    assert sorted(set(engine) - set(web)) == [], "codes the web app writes in English only"
    assert sorted(set(web) - set(engine)) == [], "codes the engine does not send"


def test_the_web_app_writes_each_code_s_sentence_as_the_engine_does_in_english():
    engine, web = _engine_sentences(), _web_sentences()
    differ = {code: (_as_the_web_writes_it(text), web[code]) for code, text in engine.items() if code in web}
    assert {code: pair for code, pair in differ.items() if pair[0] != pair[1]} == {}
