# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The sources Lambda's requirements must not admit a pg8000 and scramp pair that cannot connect.

pg8000 1.31.5 calls ``scramp.make_channel_binding`` on every TLS connection, and scramp
1.5.0 removed that function, so a bundle that resolves this pair fails every PostgreSQL
and Redshift connection with ``AttributeError``. The bundle installs pg8000 from
``requirements.txt`` alone (it is not part of the uv workspace, and the unit tests mock
it), so this check reads the declared ranges. pg8000 1.31.6 uses the new scramp API and
requires scramp 1.5.0: raising pg8000's floor to 1.31.6 is how the scramp pin can go.
"""

from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

pytestmark = pytest.mark.unit

_REQUIREMENTS = Path(__file__).resolve().parents[3] / "requirements.txt"


def _declared() -> dict[str, SpecifierSet]:
    declared: dict[str, SpecifierSet] = {}
    for raw in _REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            requirement = Requirement(line)
            declared[canonicalize_name(requirement.name)] = requirement.specifier
    return declared


def test_pg8000_that_calls_make_channel_binding_never_pairs_with_scramp_1_5():
    declared = _declared()
    assert "pg8000" in declared, f"pg8000 is no longer declared in {_REQUIREMENTS}"
    scramp = declared.get("scramp", SpecifierSet())
    assert not (declared["pg8000"].contains("1.31.5") and scramp.contains("1.5.0")), (
        f"{_REQUIREMENTS} admits pg8000 1.31.5 with scramp 1.5.0 (pg8000 {declared['pg8000']}, "
        f"scramp {scramp or 'any'}): keep scramp below 1.5 while pg8000 1.31.5 is allowed"
    )
