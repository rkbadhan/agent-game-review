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


@pytest.fixture
def race_after_read(monkeypatch):
    """Force two threads to interleave right after a read-modify-write's read.

    ``install(module, "read_x")`` wraps ``module.read_x`` so each caller waits at
    a two-party barrier once it has read. Unlocked writers both get past it
    holding the same stale state, which is the lost-update race; a writer that
    holds a lock across its read gets no partner, times out, and carries on. The
    barrier is only a rendezvous point: correct code must not depend on it.
    """
    import threading

    def install(module, name):
        original = getattr(module, name)
        barrier = threading.Barrier(2)

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            try:
                barrier.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            return result

        monkeypatch.setattr(module, name, wrapper)

    return install


@pytest.fixture
def run_concurrently():
    """Run each zero-argument callable in its own thread; return ``[(value, error)]``."""
    import threading

    def run(*calls):
        outcomes = [(None, None)] * len(calls)

        def target(i, call):
            try:
                outcomes[i] = (call(), None)
            except Exception as exc:  # noqa: BLE001 - the test inspects the type
                outcomes[i] = (None, exc)

        threads = [threading.Thread(target=target, args=(i, c)) for i, c in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return outcomes

    return run
