"""The review queue remains usable when every trace has the same name."""
import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, expect

from agr.ingest_langfuse import convert
from agr.pipeline import analyze
from agr.store import Store
from test_ui import _launch, _serving


@pytest.fixture(scope="module")
def trace_server(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("sidebar") / "store")
    store = Store(root)
    source = json.loads((Path(__file__).parent / "fixtures/langfuse/langfuse_toy_run.json").read_text(encoding="utf-8"))
    for i in range(20):
        trace = copy.deepcopy(source)
        trace.update(id=f"{i:08x}-0000-4000-8000-000000000000", name="process_case", scores=[])
        analyze(convert(trace).doc, store)
    with _serving(root) as base:
        yield base


def _active_visible(page):
    return page.evaluate("""() => {
      const list = document.querySelector('#run-list').getBoundingClientRect();
      const active = document.querySelector('#run-list .active').getBoundingClientRect();
      return active.top >= list.top && active.bottom <= list.bottom;
    }""")


@pytest.mark.parametrize("height", [800, 600])
def test_same_name_trace_identity_pinned_filter_and_navigation(trace_server, height):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1280, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(trace_server + "/?view=review")
        expect(page.locator("#run-list .run")).to_have_count(20)
        expect(page.locator("#queue-count")).to_have_text("20 runs")
        assert page.locator("#run-list .run-name").all_text_contents() == ["process_case"] * 20
        identities = page.locator("#run-list .run-identity").all_text_contents()
        assert len(set(identities)) == 20
        assert all(" steps" in identity and "ago" in identity for identity in identities)
        assert page.locator("#run-list .run-checks").all_text_contents() == [""] * 20
        assert "No task evaluation" in page.locator("#run-list .run").first.get_attribute("title")

        for index in range(1, 15):
            page.get_by_title("Next run in this queue", exact=True).click()
            page.wait_for_function("index => !state.loading && queuePosition().index === index", arg=index + 1)
        assert _active_visible(page)
        summary = page.locator(".queue-filter-disclosure > summary")
        before = summary.bounding_box()["y"]
        page.locator("#run-list").evaluate("list => list.scrollTop = list.scrollHeight")
        assert summary.bounding_box()["y"] == pytest.approx(before)
        scroll = page.locator("#run-list").evaluate("list => list.scrollTop")
        page.evaluate("renderRunList()")
        assert page.locator("#run-list").evaluate("list => list.scrollTop") == pytest.approx(scroll)

        summary.click()
        assert page.locator("#run-list").bounding_box()["height"] >= 40
        assert summary.is_visible()
        page.locator('#queue-controls .fchip[data-filter="passed"]').click()
        expect(page.locator("#run-list .run")).to_have_count(0)
        expect(page.locator("#queue-count")).to_have_text("0 runs")
        page.locator('#queue-controls .fchip[data-filter="passed"]').click()
        expect(page.locator("#run-list .run")).to_have_count(20)
        summary.click()
        assert _active_visible(page)
        assert not errors
        browser.close()


def test_returning_to_the_same_run_reveals_it_again(trace_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(trace_server + "/?view=review")
        page.wait_for_selector("#run-list .run")
        run_id = page.locator("#run-list .run").nth(14).get_attribute("data-run-id")
        page.goto(trace_server + "/?run=" + run_id)
        page.wait_for_function("id => !state.loading && state.runId === id", arg=run_id)
        assert _active_visible(page)
        page.click("#runs-button")
        page.wait_for_selector(".runs-row")
        page.go_back()
        page.wait_for_function("id => state.view === 'review' && !state.loading && state.runId === id", arg=run_id)
        assert _active_visible(page)
        browser.close()
