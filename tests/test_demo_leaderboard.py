"""The installed offline slice and the four reproducible demo stories."""
import hashlib
import json
import socket
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from agr import demo, demo_leaderboard, fleet, read, versions
from agr.cli import _build_demo
from agr.store import Store


@pytest.fixture(scope="module")
def demo_store(tmp_path_factory):
    store = Store(str(tmp_path_factory.mktemp("offline-demo") / "store"))
    def no_network(*args, **kwargs):
        raise AssertionError("The demo must build without network access")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", no_network)
        patch.setattr(socket, "create_connection", no_network)
        definition = _build_demo(store, SimpleNamespace(store=store.root))
    assert definition["leaderboard_runs"] == 350
    assert definition["real_runs"] >= 31
    assert definition["model_reviews"] >= 23
    return store


def test_packaged_slice_has_exact_coverage_and_source_checksums():
    directory = demo_leaderboard.dataset_dir()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["repository"] == "harborframework/terminal-bench-2-leaderboard"
    assert manifest["revision"] == "572b2614be2c0cb2527e14f5b1e4026f1072e6c1"
    assert manifest["license"] == "Apache-2.0"
    assert len(manifest["submissions"]) == 7
    assert len(manifest["tasks"]) == 10
    assert len(manifest["runs"]) == manifest["run_count"] == 350
    assert len({row["run_id"] for row in manifest["runs"]}) == 350
    assert set(Counter((r["submission"], r["task"]) for r in manifest["runs"]).values()) == {5}
    assert sum(row["bytes"] for row in manifest["runs"]) == manifest["bytes"] < 30_000_000
    for row in manifest["runs"]:
        payload = (directory / row["file"]).read_bytes()
        assert len(payload) == row["bytes"]
        assert hashlib.sha256(payload).hexdigest() == row["sha256"]
        assert {p["path"].split("/", 4)[-1].split("/")[-1] for p in row["source_files"]} >= {"trajectory.json", "result.json", "config.json"}
        assert all(len(p["sha256"]) == 64 for p in row["source_files"])
    for name in ("SOURCE.md", "START_HERE.md", "LICENSE"):
        assert (directory / name).is_file()


def test_corrupt_payload_is_rejected_before_any_run_is_written(tmp_path):
    directory = demo_leaderboard.dataset_dir()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    first = bad / manifest["runs"][0]["file"]
    first.parent.mkdir()
    first.write_text("{}", encoding="utf-8")
    store = Store(str(tmp_path / "store"))
    with pytest.raises(ValueError, match="checksum"):
        demo_leaderboard.build(store, bad)
    assert not read.list_runs(store)


def test_restart_reuses_unchanged_analysis(demo_store, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unchanged bundled runs should not be reanalyzed")
    monkeypatch.setattr(demo, "_ingest_with_confirmed_contract", unexpected)
    assert demo_leaderboard.build(demo_store)["run_count"] == 350


def test_filter_story_covers_all_seven_configurations_and_35_attempts(demo_store):
    groups = fleet.test_failure_groups(demo_store)
    group = next(g for g in groups if g["key"][:2] == [
        "terminal-bench/filter-js-from-html", "test_outputs.py::test_clean_html_unchanged"])
    assert group["runs"] == group["attempts_on_task"] == 35
    assert len(group["configurations"]) == len(group["configurations_on_task"]) == 7
    assert group["same_failure_every_attempt"] is True
    assert group["passed_attempts"] == group["unknown_attempts"] == 0


def test_upgrade_story_uses_real_matching_tasks_and_sample_outcomes(demo_store):
    story = demo_leaderboard.start_here()["stories"][3]
    query = parse_qs(urlparse(story["href"]).query)
    configurations = {c["label"]: c for c in versions.list_configurations(demo_store)}
    result = versions.compare_versions(demo_store,
        configurations[query["baseline"][0]]["selector"],
        configurations[query["candidate"][0]]["selector"], query["axis"][0])
    assert result["match_status"] == "valid"
    assert result["report"]["matched_run_pairs"] == 50
    assert result["report"]["exact_matched_tasks"] == 10
    assert result["pass_rate"]["baseline"] == {"numerator": 19, "denominator": 50}
    assert result["pass_rate"]["candidate"] == {"numerator": 28, "denominator": 50}


def test_curated_story_links_have_the_claimed_evidence(demo_store):
    polyglot, retail = demo_leaderboard.start_here()["stories"][:2]
    review = read.get_review(demo_store, polyglot["run_id"])
    assert review["review_mode"] == "model_enriched"
    assert any("C=6" in str(check.get("diagnostic", {})) for check in review["checks"])
    assert any("evt_025" in m["anchor_event_ids"] for m in review["moments"])
    events = demo_store.read_derived(polyglot["run_id"], review["capture"]["capture_id"], "events.json")
    assert any(e["event_id"] == "evt_022" for e in events)
    review = read.get_review(demo_store, retail["run_id"])
    events = demo_store.read_derived(retail["run_id"], review["capture"]["capture_id"], "events.json")
    assert any("Yes, please proceed with both exchanges" in str(e) and "###STOP###" in str(e) for e in events)
    assert review["harness_protocol"]["user_termination_event_ids"] == ["evt_027"]


def test_guide_api_only_appears_in_demo_stores(demo_store, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from agr.api import create_app
    client = TestClient(create_app(str(demo_store.root)))
    guide = client.get("/demo/start-here").json()
    assert len(guide["stories"]) == 4
    assert guide["own_runs_command"] == "agr ingest-harbor <your-job-folder>"
    for story in guide["stories"][:2]:
        assert client.get("/runs/" + story["run_id"]).status_code == 200
    other = TestClient(create_app(str(tmp_path / "empty")))
    assert other.get("/demo/start-here").json() is None


def test_four_story_links_in_browser_and_mobile_layout(demo_store):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright, expect
    from test_ui import _serving, _launch
    with _serving(str(demo_store.root)) as base, sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?view=home")
        cards = page.locator(".demo-story")
        expect(cards).to_have_count(4, timeout=30000)
        links = cards.locator("a").evaluate_all("links => links.map(a => a.getAttribute('href'))")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(base + links[0])
        expect(page.locator("body")).to_contain_text("Broke it here", timeout=30000)
        page.goto(base + links[1])
        expect(page.locator("body")).to_contain_text("retail-29", timeout=30000)
        page.goto(base + links[2])
        expect(page.locator(".test-pattern-table")).to_contain_text("filter-js-from-html", timeout=30000)
        page.goto(base + links[3])
        expect(page.locator("body")).to_contain_text("38%", timeout=30000)
        expect(page.locator("body")).to_contain_text("56%")
        assert not errors
        browser.close()
