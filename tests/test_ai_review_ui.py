"""Browser journeys use an offline provider; never send traces to a real API."""
import json
import re

import pytest
pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, expect

from agr import model_reviewer as models
from agr.store import Store
from agr.userconfig import save_config
from test_ui import _serving, _analyzed, _launch, _page, CHESS


@pytest.fixture
def ai_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGR_CONFIG", str(tmp_path / "ai-config.json"))
    for key in ("AGR_REVIEW_PROVIDER", "AGR_REVIEW_MODEL", "OPENAI_BASE_URL",
                "ANTHROPIC_BASE_URL", "AGR_REVIEW_COST_BUDGET_USD", "AGR_REVIEW_TIME_BUDGET_S"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ui-environment-key")
    control = {"calls": [], "response": {"moments": []}}
    class Offline(models._LazyModelReviewer):
        provider = "openai"
        def _complete(self, system, user_json):
            control["calls"].append(json.loads(user_json))
            return control["response"]
    monkeypatch.setattr(models, "make_reviewer", lambda provider, model, base_url=None, **kwargs: Offline(model, base_url, **kwargs))
    save_config("openai", "ui-model", "https://offline.invalid/v1", cost_budget_usd=0, time_budget_s=0)
    store = Store(str(tmp_path / "store"))
    for name in ("chess_best_move.atif.json", "contract_mismatch.atif.json"):
        _analyzed(store, name)
    with _serving(store.root) as base:
        yield base, control


def test_ai_settings_batch_history_and_refresh(ai_server):
    base, control = ai_server
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?view=settings")
        page.get_by_role("heading", name="AI review", exact=True).wait_for()
        page.get_by_label("Reviewer model ID", exact=True).fill("ui-model")
        page.get_by_label("API key (optional; server session only)", exact=True).fill("ui-session-key")
        page.get_by_role("button", name="Save AI settings", exact=True).click()
        expect(page.get_by_text(re.compile(r"Credentials: session\."))).to_be_visible()
        assert not control["calls"]
        page.get_by_role("button", name="Test connection · uses tokens", exact=True).click()
        expect(page.get_by_text(re.compile(r"Connection: verified"))).to_be_visible()
        assert control["calls"] == [{"self_test": True, "expected": {"moments": []}}]
        page.get_by_role("button", name="Review project runs", exact=True).click()
        page.get_by_role("heading", name="AI review", exact=True).wait_for()
        page.get_by_role("button", name="Select all project runs", exact=True).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("heading", name="Ready to review", exact=True).wait_for()
        expect(page.get_by_text(re.compile(r"Cost target: disabled; elapsed target: disabled"))).to_be_visible()
        assert len(control["calls"]) == 1
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="completed · 2/2")).to_be_visible(timeout=15000)
        assert len(control["calls"]) == 3
        assert "review_job=" in page.url
        page.reload()
        expect(page.get_by_role("status").filter(has_text="completed · 2/2")).to_be_visible()
        page.get_by_role("button", name="Choose other runs", exact=True).click()
        page.get_by_role("button", name="Select all project runs", exact=True).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        expect(page.get_by_text("2 selected · 2 already reviewed with this configuration", exact=True)).to_be_visible()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="completed · 2/2")).to_be_visible()
        assert len(control["calls"]) == 3
        assert not errors
        browser.close()


def test_single_run_selection_and_failed_connection(ai_server):
    base, control = ai_server
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=review&run=" + CHESS)
        page.get_by_role("button", name="Run AI review", exact=True).click()
        expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="completed · 1/1")).to_be_visible(timeout=15000)
        page.get_by_role("button", name="AI settings", exact=True).click()
        control["response"] = {"unexpected": True}
        page.get_by_role("button", name="Test connection · uses tokens", exact=True).click()
        expect(page.get_by_text(re.compile(r"Connection: failed"))).to_be_visible()
        page.get_by_label("API provider", exact=True).select_option("anthropic")
        expect(page.get_by_label("Reviewer model ID", exact=True)).to_have_value("")
        expect(page.get_by_label("Endpoint URL (change for a custom or local server)", exact=True)).to_have_value("https://api.anthropic.com")
        browser.close()



def test_bulk_selection_invalidates_preview(ai_server):
    base, control = ai_server
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=review&run=" + CHESS)
        page.get_by_role("button", name="Run AI review", exact=True).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        expect(page.get_by_role("heading", name="Ready to review", exact=True)).to_be_visible()
        page.get_by_role("button", name="Select all project runs", exact=True).click()
        expect(page.get_by_role("heading", name="Ready to review", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Start AI review · uses tokens", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        expect(page.get_by_text("2 selected · 0 already reviewed with this configuration", exact=True)).to_be_visible()
        page.get_by_role("button", name="Clear selection", exact=True).click()
        expect(page.get_by_role("heading", name="Ready to review", exact=True)).to_have_count(0)
        expect(page.get_by_text("0 selected", exact=True)).to_be_visible()
        assert not control["calls"]
        browser.close()


def test_model_picker_lists_endpoint_models(ai_server, monkeypatch):
    base, _ = ai_server
    monkeypatch.setattr(models, "list_models", lambda provider, base_url=None, **kwargs: ["listed-a", "listed-b"])
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=settings")
        page.get_by_role("heading", name="AI review", exact=True).wait_for()
        page.get_by_role("button", name="Load models", exact=True).click()
        page.get_by_label("Available models", exact=True).select_option("listed-b")
        expect(page.get_by_label("Reviewer model ID", exact=True)).to_have_value("listed-b")
        page.get_by_role("button", name="Save AI settings", exact=True).click()
        expect(page.get_by_text("Effective: openai · listed-b · https://offline.invalid/v1", exact=True)).to_be_visible()
        browser.close()


def test_sample_project_can_configure_and_start_ai_review(ai_server):
    base, control = ai_server
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=settings")
        page.get_by_role("heading", name="AI review", exact=True).wait_for()
        assert page.evaluate("fetch('/projects/sample', {method: 'POST'}).then(r => r.ok)")
        page.goto(base + "/?view=settings")  # creating the sample made it the active project
        page.get_by_role("heading", name="AI review", exact=True).wait_for()
        expect(page.get_by_label("Switch project", exact=True).locator("option:checked")).to_have_text("Sample data · sample")
        expect(page.get_by_label("Reviewer model ID", exact=True)).to_have_value("ui-model")
        expect(page.get_by_text(re.compile("unavailable in sample"))).to_have_count(0)
        page.get_by_role("button", name="Review project runs", exact=True).click()
        page.get_by_role("button", name="Select all project runs", exact=True).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("heading", name="Ready to review", exact=True).wait_for()
        browser.close()
