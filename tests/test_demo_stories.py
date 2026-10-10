"""The two-run demo must expose different causes for the same failed verdict."""

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from agr import demo, read
from agr.store import Store

POLYGLOT = "harbor__terminal-bench/polyglot-c-py__6c3b4b8b-991"
RETAIL = "harbor__sierra-research/tau3-bench__tau3-retail-29__2f510d52-4453-4982-95f1-b2f0e3829164"
NGINX = "harbor__terminal-bench/nginx-request-logging__4e941d41-a66"
MIPS = "harbor__terminal-bench/make-mips-interpreter__make-mips-in"


@pytest.fixture(scope="module")
def story_store(tmp_path_factory):
    store = Store(str(tmp_path_factory.mktemp("demo-stories")))
    definition = demo.build_real_demo_store(store, include_comparison=False)
    assert definition["stale_reviews"] == definition["failed_reviews"] == 0
    assert definition["landing_run"] == POLYGLOT
    return store


def test_polyglot_links_the_matching_version_and_the_rewrite(story_store):
    review = read.get_review(story_store, POLYGLOT)
    assert review["outcome"]["status"] == "FAILED"
    broken, working = review["moments"]
    assert broken["anchor_event_ids"] == ["evt_025"]
    assert working["anchor_event_ids"] == ["evt_022"]
    assert broken["taxonomy_verdict"].startswith("Broke it here:")
    assert working["taxonomy_verdict"].startswith("Working here:")
    assert broken["gate_results"]["fact_validation"] == "passed"
    assert review["review_curation"]["date"] == "2026-10-10"
    forensic = read.get_forensic(story_store, POLYGLOT)
    assert "evt_025" in forensic["steps"][24]["event_ids"]
    # The old 2,000-character export cutoff must not masquerade as a broken file.
    source = story_store.read_source(POLYGLOT, review["capture"]["capture_id"])
    assert "b = temporary_pointer;" in source["steps"][24]["content"]
    assert "C=6" in json.dumps(source["verifier"]["log_excerpts"])


@pytest.mark.parametrize("task", [19, 29, 35, 58, 76])
def test_retail_ending_explains_the_loss_without_inventing_agent_blame(story_store, task):
    row = next(r for r in read.list_runs(story_store) if f"tau3-retail-{task}__" in r["run_id"]
               and r["outcome"]["status"] == "FAILED")
    review = read.get_review(story_store, row["run_id"])
    assert review["outcome"]["status"] == "FAILED"
    if task in (35, 76):
        assert review["moments"]  # Earlier strengths survive the ending headline.
    else:
        assert review["moments"] == []
    context = review["outcome_context"]
    assert context["headline"] == "Simulated customer ended the conversation before the agent could act"
    assert context["leads_overview"] is True
    assert context["event_ids"] == list(dict.fromkeys([
        *review["harness_protocol"]["terminal_confirmation_event_ids"],
        *review["harness_protocol"]["user_termination_event_ids"]]))
    assert "not an agent omission" in context["detail"]
    assert row["main_finding"] == context["headline"]
    assert row["main_finding_polarity"] == "neutral"
    assert row["counts"]["concern"] == 0


@pytest.mark.parametrize("status,protocol,expected", [
    ("PASSED", {"user_termination_event_ids": ["e2"]}, None),
    ("FAILED", {}, None),
    ("FAILED", {"user_termination_event_ids": ["e2"]}, "Simulated customer ended the conversation"),
])
def test_ending_context_is_bounded_by_recorded_protocol(status, protocol, expected):
    context = read._outcome_context({"status": status}, protocol)
    assert (context or {}).get("headline") == expected


def test_nginx_counts_eight_tests_separately_from_the_task_reward(story_store):
    review = read.get_review(story_store, NGINX)
    assert review["outcome"]["status"] == "FAILED"
    assert review["outcome"]["test_results"] == {"passed": 7, "total": 8}
    tests = [c for c in review["checks"] if "verifier/ctrf.json" in c["source_pointers"]]
    assert [c["check_id"] for c in tests if c["status"] == "failed"] == ["test_outputs.py::test_log_file_format"]
    assert len(review["checks"]) == 9  # The authoritative reward is retained.
    row = next(r for r in read.list_runs(story_store) if r["run_id"] == NGINX)
    assert row["outcome"]["test_results"] == {"passed": 7, "total": 8}


def test_mips_does_not_present_initialization_as_a_verified_working_state(story_store):
    review = read.get_review(story_store, MIPS)
    finding = next(m for m in review["moments"] if m["polarity"] == "negative")
    assert "before a working VM was verified" in finding["taxonomy_verdict"]
    assert "over-refinement past a working state" not in json.dumps(review)
    assert review["outcome"]["test_results"] == {"passed": 0, "total": 3}


def test_editorial_provenance_survives_bake_and_rebuild(story_store, tmp_path):
    baked = str(tmp_path / "baked")
    demo.bake_reviews(story_store, baked, model="accounts/fireworks/models/kimi-k3")
    rebuilt = Store(str(tmp_path / "rebuilt"))
    demo.build_real_demo_store(rebuilt, real_dir=baked, include_comparison=False)
    for run_id in (POLYGLOT, MIPS):
        original = read.get_review(story_store, run_id)
        replayed = read.get_review(rebuilt, run_id)
        assert replayed["review_curation"] == original["review_curation"]
        assert replayed["reviewed_at"] == original["reviewed_at"]


def test_demo_stories_in_the_browser(story_store, tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from test_ui import _launch, _page, _serving

    with _serving(story_store.root) as base, sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser, viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?run=" + quote(POLYGLOT, safe=""))
        page.locator(".main-finding-title").wait_for()
        assert "Broke it here:" in page.locator(".main-finding-title").inner_text()
        assert "during Execution" not in page.locator(".main-finding-title").inner_text()
        shell = page.locator(".shell-meta")
        assert "Curated review · 2026-10-10" in shell.inner_text()
        assert "AI-enriched" not in shell.inner_text() and "kimi-k3" not in shell.inner_text()
        assert page.locator(".finding-mode").inner_text().startswith("Curated review · 2026-10-10")
        assert "Corrected anchors" in page.locator(".finding-mode").inner_text()
        subtitle = page.locator(".run-subtitle")
        assert subtitle.inner_text().startswith("GLM-5.3 Flash")
        assert "Ox Alpha" not in subtitle.inner_text() and "stealth/ox-alpha" not in subtitle.inner_text()
        assert "stealth/ox-alpha" in subtitle.get_attribute("title")
        page.get_by_role("button", name="View evidence →", exact=True).click()
        assert "trace step 25" in page.locator(".moment-anchor").inner_text()
        assert "Curated review · 2026-10-10" in page.locator(".moment-topline").inner_text()
        assert "b = temporary_pointer;" in page.locator(".evidence-quote").inner_text()
        page.locator(".evidence-anchor-tabs").get_by_role("button", name="Step 22", exact=True).click()
        assert "trace step 22" in page.locator(".evidence-source .evidence-label").first.inner_text()
        page.get_by_role("button", name="Next key moment", exact=True).click()
        assert "Working here:" in page.locator(".moment-heading h2").inner_text()
        assert "trace step 22" in page.locator(".moment-anchor").inner_text()
        page.goto(base + "/?run=" + quote(RETAIL, safe=""))
        page.locator(".main-finding-title").wait_for()
        assert "before the agent could act" in page.locator(".main-finding-title").inner_text()
        assert "All proposals rejected" not in page.locator(".shell-meta").inner_text()
        ending = page.locator(".finding-card .moment-anchors")
        assert "26" in ending.inner_text() and "27" in ending.inner_text()
        for task in (35, 76):
            row = next(r for r in read.list_runs(story_store) if f"tau3-retail-{task}__" in r["run_id"]
                       and r["outcome"]["status"] == "FAILED")
            page.goto(base + "/?run=" + quote(row["run_id"], safe=""))
            page.locator(".main-finding-title").wait_for()
            assert "before the agent could act" in page.locator(".main-finding-title").inner_text()
            assert "Earlier finding:" in page.locator(".finding-earlier").inner_text()
            page.get_by_role("button", name="Inspect earlier finding", exact=True).click()
            assert page.locator(".moment-card").is_visible()
        page.goto(base + "/?run=" + quote(NGINX, safe=""))
        page.locator(".main-finding-title").wait_for()
        assert "7 of 8 tests passed" in page.locator(".run-header").inner_text()
        assert "1 of 8 tests failed" in page.locator(".finding-outcome").inner_text()
        assert "7/8" in page.locator(".run.active").inner_text()
        page.click("#runs-button")
        page.wait_for_selector(".runs-table-card")
        # Locate by data attribute rather than the truncated human-readable title.
        retail = page.locator('.runs-row[data-run-id="' + RETAIL + '"]')
        assert "Simulated customer ended" in retail.inner_text()
        for task in (35, 76):
            run = next(r for r in read.list_runs(story_store) if f"tau3-retail-{task}__" in r["run_id"]
                       and r["outcome"]["status"] == "FAILED")
            row = page.locator('.runs-row[data-run-id="' + run["run_id"] + '"]')
            assert "Simulated customer ended" in row.inner_text()
        nginx = page.locator('.runs-row[data-run-id="' + NGINX + '"]')
        assert "7 of 8 tests passed" in nginx.inner_text()
        model = nginx.locator(".runs-id-model")
        assert model.inner_text() == "GLM-5.3 Flash"
        assert "stealth/ox-alpha" in model.get_attribute("title")
        assert not errors
        browser.close()
