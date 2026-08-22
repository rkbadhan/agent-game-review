"""The published ATIF schema doc must stay in lockstep with the code.

docs/atif-schema.json is what external adapter authors target. If its
controlled vocabularies drift from agr's actual constants, the doc lies. These
tests make drift a failing build rather than a silent trap.
"""

import json
import os

from agr.events import KIND_TO_EVENT
from agr.schema import (
    CAPABILITY_LEVELS,
    CAPTURE_COMPLETENESS,
    CHECK_SOURCES,
    CHECK_STATUSES,
)
from agr.adapter import DEFAULT_CAPABILITIES

SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "docs", "atif-schema.json"
)


def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def test_step_kind_enum_matches_code():
    schema = load_schema()
    enum = schema["properties"]["steps"]["items"]["properties"]["kind"]["enum"]
    assert set(enum) == set(KIND_TO_EVENT), (
        "docs/atif-schema.json step kinds drifted from agr/events.py:KIND_TO_EVENT"
    )


def test_capability_levels_match_code():
    schema = load_schema()
    enum = schema["properties"]["capabilities"]["additionalProperties"]["enum"]
    assert enum == list(CAPABILITY_LEVELS)


def test_capability_names_match_code():
    schema = load_schema()
    names = schema["properties"]["capabilities"]["propertyNames"]["enum"]
    assert set(names) == set(DEFAULT_CAPABILITIES)


def test_capture_completeness_matches_code():
    schema = load_schema()
    enum = schema["properties"]["capture_completeness"]["enum"]
    assert set(enum) == set(CAPTURE_COMPLETENESS)


def test_verifier_check_vocab_matches_code():
    schema = load_schema()
    check = schema["properties"]["verifier"]["properties"]["checks"]["items"]["properties"]
    assert set(check["status"]["enum"]) == set(CHECK_STATUSES)
    assert set(check["source"]["enum"]) == set(CHECK_SOURCES)
