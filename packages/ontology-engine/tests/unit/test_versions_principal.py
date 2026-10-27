# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The caller principal travels as a percent-encoded ``X-Coa-Principal`` header."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from coa_ontology.versions import principal

pytestmark = pytest.mark.unit

KOREAN = "홍길동@example.com"


class TestCaller:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, "unknown"),
            ("", "unknown"),
            ("   ", "unknown"),
            ("%20%0D%0A", "unknown"),
            ("alice@example.com", "alice@example.com"),
            (quote(KOREAN, safe="@._+-"), KOREAN),
            ("arn%3Aaws%3Aiam%3A%3A1%3Auser%2Fbob", "arn:aws:iam::1:user/bob"),
            ("%20alice%20", "alice"),
            ("a%0D%0Ab", "ab"),
            ("%00", "unknown"),
            ("%09%0D%0A", "unknown"),
            ("a%00b%09c", "abc"),
        ],
    )
    def test_decodes_the_header(self, header, expected):
        assert principal.caller(header) == expected
