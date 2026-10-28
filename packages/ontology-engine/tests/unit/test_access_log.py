# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""uvicorn's access log lines keep no query string (``coa_ontology.main``)."""

import logging

import coa_ontology.main  # noqa: F401 - installs the access log filter
import pytest
from uvicorn.logging import AccessFormatter

pytestmark = pytest.mark.unit


def _access_line(path: str) -> str:
    """Log a request the way uvicorn does and format it with uvicorn's own access formatter."""
    access = logging.getLogger("uvicorn.access")
    record = access.makeRecord(
        access.name,
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.7:41000", "GET", path, "1.1", 200),
        None,
    )
    assert access.filter(record)
    return AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)


def test_the_access_log_drops_the_query_string():
    """The Domain data search sends the entity name it looks for, a patient's for example, as ``q``."""
    line = _access_line("/abox/instances?q=%EA%B9%80%EB%AF%BC%EC%88%98&limit=50&namespace=ns-1")
    assert line == '10.0.0.7:41000 - "GET /abox/instances HTTP/1.1" 200 OK'


def test_a_path_without_a_query_string_is_logged_as_it_is():
    assert _access_line("/abox/mappings") == '10.0.0.7:41000 - "GET /abox/mappings HTTP/1.1" 200 OK'
