import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixtures_dir():
    return FIXTURES


@pytest.fixture
def load_fixture():
    return load


@pytest.fixture(autouse=True)
def _fresh_langfuse_version_cache():
    """A host's detected Langfuse version is cached in-process; tests must not inherit one another's."""
    from agr import langfuse_source
    langfuse_source.clear_cache()
    yield
    langfuse_source.clear_cache()
