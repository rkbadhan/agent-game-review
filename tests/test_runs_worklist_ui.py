"""Runs input previews and saved views against ingested trace fixtures."""
import copy
import json
from pathlib import Path

import pytest

pytest.importorskip('playwright')
from playwright.sync_api import sync_playwright, expect

from agr import workflow
from agr.ingest_langfuse import convert
from agr.pipeline import analyze
from agr.store import Store
from test_ui import _launch, _serving


@pytest.fixture(scope='module')
def worklist_server(tmp_path_factory):
    root = str(tmp_path_factory.mktemp('worklist') / 'store')
    store = Store(root)
    source = json.loads((Path(__file__).parent / 'fixtures/langfuse/langfuse_toy_run.json').read_text(encoding='utf-8'))
    for i in range(52):
        trace = copy.deepcopy(source)
        trace.update(id=f'case-{i:03d}', name='process_case', scores=[], input=f'Case {i:03d}: {source["input"]}')
        if i == 17:
            trace["input"] = "Investigate case 017 for bob@example.com <img src=x onerror=alert(1)>"
        result = analyze(convert(trace).doc, store)
        if i in (0, 1):
            workflow.set_workflow(store, result.run_source.run_id, actor='Test reviewer', base_version=0,
                                  progress='handled' if i == 0 else 'in_progress',
                                  disposition='no_action' if i == 0 else None)
    for name in ('clean_pass.atif.json', 'chess_best_move.atif.json'):
        analyze(json.loads((Path(__file__).parent.parent / 'archive/synthetic/fixtures' / name).read_text(encoding='utf-8')), store)
    with _serving(root) as base:
        yield base


@pytest.mark.parametrize('width', [1280, 1024, 760, 390])
def test_redacted_input_search_paging_and_return(worklist_server, width):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': width, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(worklist_server + '/?view=runs')
        expect(page.locator('.runs-row')).to_have_count(50)
        expect(page.locator('th.runs-input-heading')).to_have_count(1)
        page.locator('.runs-search').fill('case 017')
        expect(page.locator('.runs-row')).to_have_count(1)
        cell = page.locator('.runs-input')
        if width <= 820:
            assert page.locator('.runs-table-card .table-scroll').evaluate('s => s.scrollWidth <= s.clientWidth + 1')
        cell.scroll_into_view_if_needed()
        expect(cell).to_be_in_viewport()
        expect(cell).to_contain_text('[REDACTED:email]')
        assert 'bob@example.com' not in cell.inner_text()
        assert cell.locator('img').count() == 0
        assert '[REDACTED:email]' in cell.locator('span').get_attribute('title')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.locator('.runs-search').fill('')
        page.get_by_role('button', name='Show more', exact=False).click()
        expect(page.locator('.runs-row')).to_have_count(54)
        last = page.locator('.runs-row').last
        run_id = last.get_attribute('data-run-id')
        last.scroll_into_view_if_needed()
        last.focus()
        expect(last).to_be_in_viewport()
        last.press('Enter')
        page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('id => !state.loading && state.runId === id', arg=run_id)
        page.go_back()
        page.get_by_role('button', name='Close inspector', exact=True).click()
        expect(page.locator('.runs-row')).to_have_count(54)
        expect(page.locator(f'.runs-row[data-run-id="{run_id}"]')).to_be_in_viewport()
        assert not errors
        browser.close()


def test_saved_views_use_human_state_and_preserve_definitions(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.goto(worklist_server + '/?view=runs')
        picker = page.get_by_label('Saved run view', exact=True)
        picker.select_option('awaiting')
        page.wait_for_function("state.queue.runs.length === 53 && state.queue.filters.length === 2")
        assert page.evaluate("state.queue.runs.every(r => ['unreviewed','in_progress'].includes(r.workflow.review_progress))")
        assert page.evaluate("state.queue.runs.some(r => r.workflow.review_progress === 'in_progress')")
        assert page.evaluate("state.queue.runs.some(r => r.outcome.status === 'UNVERIFIED')")
        picker.select_option('failed')
        page.wait_for_function("state.queue.filters.join() === 'failed'")
        assert page.evaluate("state.queue.runs.length > 0 && state.queue.runs.every(r => ['FAILED','ERROR'].includes(r.outcome.status))")
        picker.select_option('awaiting')
        page.wait_for_function('state.filters.has("in_progress")')
        page.locator('.runs-search').fill('case 017')
        page.locator('.runs-toolbar-actions .sort-row select').select_option('duration')
        page.wait_for_function("state.queue.sort === 'duration'")
        page.get_by_label('Filter runs by AI review status').select_option('none')
        page.get_by_text('Save current view', exact=True).click()
        page.get_by_label('View name', exact=True).fill('Config investigation')
        page.get_by_role('button', name='Save view', exact=True).click()
        saved_id = picker.input_value()
        assert saved_id.startswith('saved-')
        picker.select_option('all')
        page.wait_for_function('state.filters.size === 0 && state.queue.runs.length === 54')
        page.reload()
        picker.select_option(saved_id)
        expect(page.locator('.runs-row')).to_have_count(1)
        expect(page.locator('.runs-search')).to_have_value('case 017')
        assert page.evaluate("state.sort === 'duration' && state.filters.has('in_progress') && state.ai.filter === 'none'")
        page.reload()
        expect(picker).to_have_value(saved_id)
        assert page.evaluate("state.ai.filter === 'none'")
        page.evaluate("state.projectId = 'another-project'; refreshRunsViews()")
        assert picker.locator('option', has_text='Config investigation').count() == 0
        page.evaluate("state.projectId = 'existing'; refreshRunsViews()")
        page.get_by_role('button', name='Delete saved view', exact=True).click()
        assert picker.locator('option', has_text='Config investigation').count() == 0
        expect(page.locator('.runs-row')).to_have_count(1)
        browser.close()
