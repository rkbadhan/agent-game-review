"""WCAG 2.1 A/AA checks of the Runs workbench using locally installed axe-core.

Install development assets with `npm ci`; the served app never loads axe.
"""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright
from test_ui import _launch
from test_runs_worklist_ui import worklist_server


@pytest.fixture(scope="module")
def axe_source():
    source = Path(__file__).resolve().parents[1] / "node_modules/axe-core/axe.min.js"
    if not source.is_file():
        message = "axe-core is missing; run npm ci before the accessibility suite"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return source.read_text(encoding="utf-8")


def _assert_accessible(page, source, surface):
    # A navigation replaces the JS context; injecting per scan also covers reloads.
    page.evaluate("document.fonts.ready")
    page.add_script_tag(content=source)
    result = page.evaluate("""async () => {
        const result = await axe.run(document, {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] }
        });
        return result.violations.map(v => ({
            id: v.id, impact: v.impact, help: v.help, url: v.helpUrl,
            nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary }))
        }));
    }""")
    assert not result, f"{surface}: accessibility violations\n" + json.dumps(result, indent=2)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_runs_workbench_accessibility(worklist_server, axe_source, width, theme):
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=theme)
            page.goto(worklist_server + "/?view=runs")
            expect(page.locator(".runs-row")).to_have_count(50)
            _assert_accessible(page, axe_source, f"Runs / {width} / {theme}")

            page.locator(".runs-search").fill("case 017")
            expect(page.locator(".runs-row")).to_have_count(1)
            page.locator(".runs-row").click()
            expect(page.locator(".trace-inspector")).to_contain_text("Fixed port to 8080")
            _assert_accessible(page, axe_source, f"Inspector / {width} / {theme}")

            page.get_by_role("button", name="Open full review", exact=True).click()
            page.wait_for_function("!state.loading && state.review && state.view === 'review'")
            _assert_accessible(page, axe_source, f"Review / {width} / {theme}")
        finally:
            browser.close()
