# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Decode the caller named by the ``X-Coa-Principal`` header."""

from __future__ import annotations

import unicodedata
from urllib.parse import unquote


def caller(x_coa_principal: str | None) -> str:
    """Return the acting principal, or ``"unknown"`` when none was sent.

    The API proxy percent-encodes the principal so a non-latin-1 character or a
    CR/LF in it cannot break the forwarded request; undo that here. Control
    characters are dropped so the audit fields never hold them.
    """
    decoded = "".join(ch for ch in unquote(x_coa_principal or "") if unicodedata.category(ch) != "Cc")
    return decoded.strip() or "unknown"
