"""AI review progress must be visible and trackable outside the one page that started it."""
import json
import re
import time

import pytest
pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, expect

from agr import model_reviewer as models
from agr.store import Store
from agr.userconfig import save_config
from test_ui import _serving, _analyzed, _launch, _page, CHESS


@pytest.fixture
def slow_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("AGR_CONFIG", str(tmp_path / "ai-config.json"))
    for key in ("AGR_REVIEW_PROVIDER", "AGR_REVIEW_MODEL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "AGR_REVIEW_COST_BUDGET_USD", "AGR_REVIEW_TIME_BUDGET_S"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ui-environment-key")
    control = {"calls": [], "delay": 0.0, "fail": set()}

    class Offline(models._LazyModelReviewer):
        provider = "openai"

        def _complete(self, system, user_json):
            packet = json.loads(user_json)
            control["calls"].append(packet)
            time.sleep(control["delay"])
            return {"moments": []}

    monkeypatch.setattr(models, "make_reviewer", lambda provider, model, base_url=None, **kwargs: Offline(model, base_url, **kwargs))
    save_config("openai", "ui-model", "https://offline.invalid/v1", cost_budget_usd=0, time_budget_s=0)
    store = Store(str(tmp_path / "store"))
    for name in ("chess_best_move.atif.json", "contract_mismatch.atif.json", "clean_pass.atif.json", "ignored_failure.atif.json"):
        _analyzed(store, name)
    with _serving(store.root) as base:
        yield base, control


def test_progress_is_visible_everywhere_while_a_review_runs(slow_ai):
    base, control = slow_ai
    control["delay"] = 0.8
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?view=runs")
        page.locator(".runs-row").first.wait_for()
        # Before anything runs: the Runs page says nothing has been reviewed, and the sidebar has no badge.
        expect(page.locator(".ai-strip")).to_contain_text("4 not reviewed")
        expect(page.locator("#ai-review-badge")).to_be_hidden()
        expect(page.locator(".runs-row [data-ai-run]")).to_have_count(4)
        expect(page.locator(".runs-row .ai-chip.ai-none")).to_have_count(4)
        # The Runs button reviews what is on screen, so the selection is prefilled.
        page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
        expect(page.get_by_text("4 selected", exact=True)).to_be_visible()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        # The job page lists every run with its own state, including the ones still waiting.
        expect(page.locator(".ai-run-row")).to_have_count(4)
        expect(page.locator(".ai-run-row .ai-chip.ai-reviewing")).to_have_count(1)
        expect(page.locator(".ai-run-row .ai-chip.ai-queued")).to_have_count(3)
        expect(page.locator(".ai-now")).to_contain_text("Reviewing now:")
        # Leaving the page does not lose it: the sidebar shows progress from anywhere.
        page.get_by_role("button", name="Runs", exact=True).click()
        page.locator(".runs-row").first.wait_for()
        expect(page.locator("#ai-review-badge")).to_be_visible()
        expect(page.locator("#ai-review-badge")).to_have_text(re.compile(r"^\d/4$"))
        expect(page.locator(".ai-strip-active")).to_contain_text("of 4 runs")
        expect(page.locator(".runs-row .ai-chip.ai-reviewing")).to_have_count(1)
        # The Runs table can be narrowed to what is being reviewed.
        page.get_by_label("Filter runs by AI review status").select_option("active")
        rows = page.locator(".runs-row")
        assert 1 <= rows.count() <= 4
        # Only runs being reviewed or waiting stay; none shows another state.
        assert rows.locator(".ai-chip.ai-reviewing, .ai-chip.ai-queued").count() == rows.count()
        assert rows.locator(".ai-chip.ai-reviewed, .ai-chip.ai-none, .ai-chip.ai-failed").count() == 0
        page.get_by_label("Filter runs by AI review status").select_option("")
        # It finishes: chips, summary and badge settle without a reload.
        expect(page.locator(".runs-row .ai-chip.ai-reviewed")).to_have_count(4, timeout=30000)
        expect(page.locator("#ai-review-badge")).to_be_hidden()
        expect(page.locator(".ai-strip")).to_contain_text("4 AI reviewed")
        expect(page.get_by_text(re.compile(r"AI review finished"))).to_be_visible()
        assert len(control["calls"]) == 4 and not errors
        browser.close()


def test_activity_page_lists_jobs_and_survives_a_reload(slow_ai):
    base, control = slow_ai
    control["delay"] = 0.5
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        page.locator(".runs-row").first.wait_for()
        page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.locator(".ai-run-row")).to_have_count(4)
        # A reload finds the running job through the sidebar and the activity list.
        page.reload()
        page.locator(".sidebar-nav-item", has_text="AI review").wait_for()
        page.get_by_role("button", name=re.compile(r"^AI review")).click()
        page.locator("#ai-activity .ai-job-card").first.wait_for()
        expect(page.locator("#ai-activity .ai-job-card").first).to_contain_text("runs")
        expect(page.locator("#ai-activity .ai-job-card progress")).to_have_count(1)
        expect(page.locator("#ai-activity .ai-job-status").first).to_have_text(re.compile(r"running|queued|completed"), timeout=30000)
        expect(page.locator("#ai-activity .ai-job-status").first).to_have_text("completed", timeout=30000)
        page.get_by_role("button", name="View details").click()
        expect(page.locator(".ai-run-row .ai-chip.ai-reviewed")).to_have_count(4)
        page.get_by_role("button", name="All AI reviews", exact=True).click()
        page.locator("#ai-activity .ai-job-card").first.wait_for()
        browser.close()


def test_a_selected_filter_chip_combination_shows_runs_and_stays_readable(slow_ai):
    base, _ = slow_ai
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs&filter=failed&filter=passed")
        page.locator(".runs-row").first.wait_for()
        # Failed and Passed are alternatives: both selected widens the list instead of emptying it.
        assert page.locator(".runs-row").count() >= 2
        assert not page.get_by_text("No runs match the selected filters").count()
        browser.close()


def test_a_review_can_be_stopped_and_resumed_without_repeating_work(slow_ai):
    base, control = slow_ai
    control["delay"] = 1.0
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        page.locator(".runs-row").first.wait_for()
        page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.locator(".ai-run-row .ai-chip.ai-reviewing")).to_have_count(1)
        # Stop: takes effect between runs, so the run in flight finishes and the rest stay unreviewed.
        page.get_by_role("button", name="Cancel remaining reviews", exact=True).click()
        expect(page.locator("#ai-job .engineer-status")).to_contain_text("cancelled", timeout=30000)
        done = page.locator(".ai-run-row .ai-chip.ai-reviewed").count()
        assert 1 <= done < 4
        assert len(control["calls"]) == done
        expect(page.locator("#ai-review-badge")).to_be_hidden()
        # Resume: only the unfinished runs are reviewed.
        page.get_by_role("button", name="Retry unfinished reviews", exact=True).click()
        expect(page.locator("#ai-job .engineer-status")).to_contain_text("completed", timeout=30000)
        expect(page.locator(".ai-run-row .ai-chip.ai-reviewed")).to_have_count(4)
        assert len(control["calls"]) == 4  # nothing was paid for twice
        browser.close()


def test_polling_keeps_keyboard_focus_and_does_not_rebuild_an_unchanged_panel(slow_ai):
    base, control = slow_ai
    control["delay"] = 3.0
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        page.locator(".runs-row").first.wait_for()
        page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.locator(".ai-run-row .ai-chip.ai-reviewing")).to_have_count(1)
        cancel = page.get_by_role("button", name="Cancel remaining reviews", exact=True)
        cancel.focus()
        page.evaluate("window.__status = document.querySelector('#ai-job .engineer-status')")
        # A poll that finds nothing new leaves the panel, and so the status line, exactly as it was.
        page.wait_for_timeout(2200)
        assert page.evaluate("document.body.contains(window.__status)")
        # A poll that brings progress repaints it, and focus is still on the same button afterwards.
        expect(page.locator("#ai-job .engineer-status")).to_contain_text("1/4", timeout=15000)
        assert page.evaluate("document.activeElement && document.activeElement.textContent") == "Cancel remaining reviews"
        # The same on the Runs page: the summary strip's button keeps focus across a repaint.
        page.get_by_role("button", name=re.compile(r"^Runs")).first.click()
        page.locator(".ai-strip-host .ai-strip").wait_for()
        view = page.get_by_role("button", name="View progress", exact=True)
        view.focus()
        expect(page.locator(".ai-strip-active")).to_contain_text("2 of 4 runs", timeout=15000)
        assert page.evaluate("document.activeElement && document.activeElement.textContent") == "View progress"
        browser.close()


def test_a_job_that_settles_between_polls_is_still_announced(slow_ai):
    base, control = slow_ai
    control["delay"] = 0.2
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        page.locator(".runs-row").first.wait_for()
        page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.locator(".ai-run-row")).to_have_count(4)
        # The poller is stopped while the job finishes, then another loader is the first to see it settled.
        page.evaluate("clearTimeout(aiPollTimer); state.ai.pollToken++")
        page.wait_for_timeout(2500)
        assert len(control["calls"]) == 4
        page.evaluate("loadAIJobs()")
        expect(page.get_by_text(re.compile(r"AI review finished"))).to_be_visible(timeout=30000)
        browser.close()


def _start_review(page):
    page.locator(".runs-row").first.wait_for()
    page.get_by_role("button", name=re.compile(r"^Review 4 not yet reviewed with AI")).click()
    page.get_by_role("button", name="Preview AI review", exact=True).click()
    page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
    expect(page.locator(".ai-run-row")).to_have_count(4)


def test_focus_returns_to_the_same_run_row_not_the_first_one(slow_ai):
    base, control = slow_ai
    control["delay"] = 2.0
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        _start_review(page)
        buttons = page.locator(".ai-run-row").get_by_role("button", name="Open run", exact=True)
        buttons.nth(2).focus()
        wanted = page.evaluate("document.activeElement.dataset.aiKey")
        assert wanted and wanted.startswith("run:")
        # Progress repaints the panel; focus is still on the third row's button, not the first.
        expect(page.locator("#ai-job .engineer-status")).to_contain_text("1/4", timeout=15000)
        assert page.evaluate("document.activeElement.dataset.aiKey") == wanted
        browser.close()


def test_focus_survives_a_button_changing_its_label(slow_ai):
    base, control = slow_ai
    control["delay"] = 0.6
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=runs")
        _start_review(page)
        page.get_by_role("button", name="All AI reviews", exact=True).click()
        watch = page.get_by_role("button", name="Watch progress", exact=True)
        watch.focus()
        key = page.evaluate("document.activeElement.dataset.aiKey")
        assert key and key.startswith("activity:")
        # The job finishes and the same button now reads "View details"; it still holds focus.
        expect(page.locator("#ai-activity .ai-job-status").first).to_have_text("completed", timeout=30000)
        assert page.evaluate("document.activeElement.dataset.aiKey") == key
        assert page.evaluate("document.activeElement.textContent") == "View details"
        browser.close()
