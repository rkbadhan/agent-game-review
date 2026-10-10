"""The protocol note must open the actual ending message in the trace."""
import json
from pathlib import Path
from urllib.parse import quote

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from agr import demo
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.store import Store
from test_ui import _serving, _launch, _page


@pytest.mark.parametrize("missing_step", [False, True])
def test_user_termination_note_links_to_the_ending_message(tmp_path, missing_step):
    root = Path(demo.find_real_demo_dir())
    run_path = next((root / "runs").glob("*tau3-retail-29*2f510*"))
    review_path = root / "reviews" / run_path.name.replace(".atif.json", ".json")
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    baked = json.loads(review_path.read_text(encoding="utf-8"))
    payload = {"moments": [{**m, "structured_facts": m["validated_facts"]}
                           for m in baked["moments"]]}
    store = Store(str(tmp_path / "store"))
    analysis = analyze(doc, store, reviewer=ScriptedReviewer(payload))
    ending_step = next(s["step_id"] for s in doc["steps"]
                       if s.get("actor") == "user" and "###STOP###" in s.get("content", ""))
    with _serving(store.root) as base, sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?view=review&run=" + quote(analysis.run_source.run_id, safe=""))
        page.get_by_text("Conversation ended by simulated user", exact=True).wait_for()
        if missing_step:
            # Render directly into an isolated host to avoid altering navigation.
            disabled = page.evaluate("""() => {
                state.forensic.steps = [];
                const host = document.createElement('div');
                renderOverviewChapter(host);
                return [...host.querySelectorAll('button')].find(
                    b => b.textContent === 'Inspect ending message').disabled;
            }""")
            assert disabled
        else:
            page.get_by_role("button", name="Inspect ending message", exact=True).click()
            page.wait_for_selector("#trace-drawer.open")
            assert page.locator("#trace-body .step.active").get_attribute("data-step-id") == ending_step
        assert not errors
        browser.close()
