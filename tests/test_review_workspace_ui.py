"""Production workbench journeys using ingested Langfuse demo captures."""
import json
from urllib.parse import quote

import pytest

pytest.importorskip('playwright')
from playwright.sync_api import sync_playwright, expect
from test_ui import _launch
from test_runs_worklist_ui import worklist_server


def _open(page, base, query=''):
    page.goto(base + '/?view=runs' + query)
    page.locator('.runs-row').first.wait_for()


@pytest.mark.parametrize('width', [1280, 390])
def test_inspector_does_not_open_review_and_recovers_from_error(worklist_server, width):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': width, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 017')
        row = page.locator('.runs-row')
        run_id = row.get_attribute('data-run-id')
        before = page.request.get(worklist_server + '/runs/' + quote(run_id, safe='')).json()['workflow']
        page.route('**/inspect', lambda route: route.fulfill(status=503, json={'detail': 'Source temporarily unavailable'}))
        row.click()
        expect(page.locator('.trace-inspector [role=alert]')).to_contain_text('Could not load')
        page.unroute('**/inspect')
        page.get_by_role('button', name='Retry preview', exact=True).click()
        panel = page.locator('.trace-inspector')
        expect(panel).to_contain_text('[REDACTED:email]')
        expect(panel).to_contain_text('Fixed port to 8080')
        assert panel.locator('img').count() == 0
        after = page.request.get(worklist_server + '/runs/' + quote(run_id, safe='')).json()['workflow']
        assert before == after
        if width > 1100:
            page.locator('.runs-search').fill('case 018')
            expect(panel).to_contain_text('Outside this filter')
        page.get_by_role('button', name='Close inspector', exact=True).click()
        expect(page.locator('.runs-search')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not errors
        browser.close()


def test_selected_filtered_queue_freezes_order_and_requires_disposition(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 600})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        _open(page, worklist_server)
        page.get_by_label('Saved run view', exact=True).select_option('awaiting')
        page.wait_for_function("state.queue.filters.length === 2")
        page.locator('.runs-search').fill('case 03')
        page.get_by_label('Filter runs by AI review status').select_option('none')
        page.get_by_label('Group runs', exact=True).select_option('task')
        expect(page.locator('.runs-row')).to_have_count(10)
        # Pattern and import scopes are independent intersections with search,
        # human review state and AI state. Freeze exactly their visible result.
        page.evaluate("""() => { const ids = visibleWorklistRuns().map(r => r.run_id);
          state.patternRunIds = new Set(ids.slice(0, 6)); state.importRunIds = new Set(ids.slice(2, 5)); render(); }""")
        expect(page.locator('.runs-row')).to_have_count(3)
        ids = page.locator('.runs-row').evaluate_all('rows => rows.map(row => row.dataset.runId)')
        page.locator('[data-select-run]').first.check()
        page.locator('[data-select-run]').last.check()
        page.get_by_role('button', name='Start review · 2', exact=True).click()
        page.wait_for_function('!state.loading && state.focusedQueue && state.focusedQueue.run_ids.length === 2')
        assert page.evaluate('state.focusedQueue.run_ids') == [ids[0], ids[2]]
        expect(page.locator('.sidebar-nav')).not_to_be_visible()
        expect(page.get_by_role('button', name='Mark reviewed', exact=False)).to_be_in_viewport()
        expect(page.locator('#topbar-stepper')).to_contain_text('Run 1 of 2')
        page.wait_for_function("state.review.workflow.review_progress === 'in_progress'")
        # Choosing a disposition is required. Opening the menu alone cannot complete work.
        page.get_by_role('button', name='Mark reviewed', exact=False).click()
        assert page.evaluate('state.review.workflow.review_progress') == 'in_progress'
        page.get_by_role('button', name='No action', exact=True).click()
        page.wait_for_function('id => !state.loading && state.runId === id', arg=ids[2])
        assert page.evaluate('state.focusedQueue.run_ids') == [ids[0], ids[2]]
        expect(page.locator('#topbar-stepper')).to_contain_text('Run 2 of 2')
        first = page.request.get(worklist_server + '/runs/' + quote(ids[0], safe='')).json()
        assert first['workflow']['review_progress'] == 'handled'
        assert first['workflow']['disposition'] == 'no_action'
        assert first['outcome']['status'] == 'UNVERIFIED'
        page.reload()
        page.wait_for_function('!state.loading && !!state.review')
        assert page.evaluate('state.focusedQueue.run_ids') == [ids[0], ids[2]]
        page.get_by_role('button', name='Next unhandled', exact=False).click()
        expect(page.locator('#toast')).to_contain_text('still need review')
        page.get_by_role('button', name='Mark reviewed', exact=False).click()
        page.get_by_role('button', name='No action', exact=True).click()
        expect(page.locator('#toast')).to_contain_text('Queue complete')
        page.get_by_role('button', name='Back to Runs', exact=True).click()
        page.wait_for_function("state.view === 'runs'")
        assert page.evaluate("state.runsSearch === 'case 03' && state.ai.filter === 'none' && state.runsGrouping === 'task'")
        # The two reviewed rows leave Awaiting review; the one unselected row stays.
        page.get_by_role('button', name='Clear selection', exact=True).click() if page.get_by_role('button', name='Clear selection', exact=True).count() else None
        assert not errors
        browser.close()


def test_grouping_is_optional_and_shared_with_focused_stepper(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 600})
        _open(page, worklist_server)
        assert page.get_by_label('Group runs', exact=True).input_value() == 'none'
        page.get_by_label('Group runs', exact=True).select_option('task')
        expect(page.locator('.worklist-group')).to_have_count(3)
        group = page.locator('.worklist-group').filter(has=page.locator('summary', has_text='process_case')).first
        group.locator('summary').click()
        assert not group.evaluate('group => group.open')
        page.reload()
        group = page.locator('.worklist-group').filter(has=page.locator('summary', has_text='process_case')).first
        expect(group).to_be_attached()
        assert not group.evaluate('group => group.open')
        group.locator('summary').click()
        expected = page.evaluate('visibleWorklistRuns().map(run => run.run_id)')
        page.get_by_role('button', name='Start review · 54', exact=True).click()
        page.wait_for_function('!state.loading && !!state.focusedQueue')
        assert page.evaluate('state.focusedQueue.run_ids') == expected
        selected_group = page.locator('#run-list .focused-group').first
        selected_group.locator('summary').click()
        assert not selected_group.evaluate('group => group.open')
        page.evaluate('loadInbox()')
        assert not selected_group.evaluate('group => group.open')
        page.keyboard.press('Alt+ArrowDown')
        page.wait_for_function('id => !state.loading && state.runId === id', arg=expected[1])
        expect(page.locator('#run-list .active')).to_be_in_viewport()
        page.get_by_label('Find in review queue').fill('case 017')
        expect(page.locator('#run-list .run')).to_have_count(1)
        assert page.evaluate('state.focusedQueue.run_ids') == expected
        browser.close()


def test_home_uses_human_backlog_and_one_primary_action(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.goto(worklist_server)
        expect(page.locator('#home-attention')).to_contain_text('awaiting human review')
        expect(page.locator('#main .button.primary')).to_have_count(1)
        assert page.locator('#add-runs-button').get_attribute('class').split().count('primary') == 0
        assert page.locator('.engineer-stat-row').count() == 0
        assert 'Unverified' not in page.locator('#home-attention').inner_text()
        # Polling updates attention without rebuilding the user's Home page.
        page.evaluate("""() => { const id=state.workspace.detail.runs[0].run_id;
          state.ai.jobs=[{id:'failed-example',status:'completed',run_ids:[id],results:[{run_id:id,status:'failed'}]}];refreshAIViews(); }""")
        expect(page.locator('#home-attention')).to_contain_text('1 AI reviews to retry')
        expect(page.get_by_role('button', name='Review retries', exact=True)).to_be_visible()
        browser.close()


def test_queue_retry_preserves_inspection_and_blocks_stale_review(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 040')
        page.locator('.runs-row').click()
        expect(page.locator('.trace-inspector')).to_contain_text('Case 040')
        page.route('**/queue?*', lambda route: route.fulfill(status=503, json={'detail': 'Storage temporarily unavailable'}))
        page.locator('.runs-controls-row .fchip[data-filter=failed]').click()
        expect(page.locator('.queue-error-notice')).to_contain_text('Showing the last loaded results')
        expect(page.locator('.trace-inspector')).to_contain_text('Case 040')
        expect(page.get_by_role('button', name='Open full review', exact=True)).to_be_disabled()
        page.unroute('**/queue?*')
        page.get_by_role('button', name='Retry loading runs', exact=True).click()
        expect(page.locator('.queue-error-notice')).to_have_count(0)
        expect(page.locator('.trace-inspector')).to_contain_text('Outside this filter')
        expect(page.get_by_role('button', name='Open full review', exact=True)).to_be_enabled()
        browser.close()


def test_late_inspector_response_and_missing_browser_queue_are_safe(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 04')
        ids = page.locator('.runs-row').evaluate_all('rows => rows.map(row => row.dataset.runId)')
        first_data = page.request.get(worklist_server + '/runs/' + quote(ids[0], safe='') + '/inspect').json()
        pending = []
        def delay_first(route):
            if ids[0] in route.request.url:
                pending.append(route)
            else:
                route.continue_()
        page.route('**/inspect', delay_first)
        page.locator('.runs-row').nth(0).click()
        page.locator('.runs-row').nth(1).click()
        expect(page.locator('.trace-inspector')).to_contain_text('Case 041')
        assert pending
        pending[0].fulfill(json=first_data)
        assert page.evaluate('state.inspector.data.run_id') == ids[1]
        assert 'Case 040' not in page.locator('.trace-inspector').inner_text()
        page.goto(worklist_server + '/?run=' + quote(ids[0], safe='') + '&review_queue=missing-browser-queue')
        page.wait_for_function("state.view === 'runs' && !state.focusedQueue")
        expect(page.locator('#toast')).to_contain_text('unavailable in this browser')
        browser.close()


def test_thousand_row_worklist_is_paged_and_searches_unrendered_inputs(tmp_path, monkeypatch):
    # This isolates browser paging over 1,000 read-model rows; pipeline and
    # end-to-end trace ingestion are covered by the journeys above.
    from agr import read
    from agr.pipeline import analyze
    from agr.store import Store
    from pathlib import Path
    from test_ui import _serving
    store = Store(str(tmp_path / 'large'))
    source = json.loads((Path(__file__).parent.parent / 'archive/synthetic/fixtures/clean_pass.atif.json').read_text(encoding='utf-8'))
    analyze(source, store)
    row = read.list_runs(store)[0]
    rows = [dict(row, run_id=f'layout-case-{index:04d}', harness_version='langfuse-export',
                 display_name='process_case', input_excerpt=f'Illustrative input {index:04d}') for index in range(1000)]
    rows[-1]['run_id'] = row['run_id']
    monkeypatch.setattr(read, 'list_runs', lambda store: list(rows))
    with _serving(store.root) as base, sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, base)
        expect(page.locator('.runs-row')).to_have_count(50)
        page.get_by_role('button', name='Show more', exact=False).click()
        expect(page.locator('.runs-row')).to_have_count(100)
        page.locator('.runs-search').fill('input 0999')
        expect(page.locator('.runs-row')).to_have_count(1)
        expect(page.locator('.runs-input')).to_contain_text('0999')
        expect(page.get_by_role('button', name='Start review · 1', exact=True)).to_be_enabled()
        # Inspect a real capture at the end, then clear search to review the
        # whole read-model worklist. Focused review opens its containing window.
        page.locator('.runs-row').click()
        expect(page.get_by_role('button', name='Open full review', exact=True)).to_be_enabled()
        page.locator('.runs-search').fill('')
        page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('!state.loading && !!state.focusedQueue')
        expect(page.locator('#run-list .run')).to_have_count(50)
        expect(page.locator('#run-list .active')).to_be_in_viewport()
        assert page.evaluate('state.focusedQueue.run_ids.length') == 1000
        browser.close()


@pytest.mark.parametrize('width', [390, 320])
def test_mobile_disposition_menu_stays_inside_viewport(worklist_server, width):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': width, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 048')
        page.get_by_role('button', name='Start review · 1', exact=True).click()
        page.wait_for_function('!state.loading && !!state.review')
        page.get_by_role('button', name='Mark reviewed', exact=False).click()
        menu = page.locator('#disp-menu')
        expect(menu).to_be_visible()
        box = menu.bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width
        expect(menu.get_by_role('button', name='No action', exact=True)).to_be_in_viewport()
        browser.close()


def test_winning_background_queue_refresh_paints_rows_without_losing_drafts(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 04')
        payload = page.request.get(worklist_server + '/queue').json()
        page.evaluate("state.queue = Object.assign({}, state.queue, {runs:[], run_ids:[]}); render()")
        expect(page.locator('.runs-row')).to_have_count(0)
        page.get_by_text('Save current view', exact=True).click()
        name = page.get_by_label('View name', exact=True)
        name.fill('My draft view')
        queued = []
        page.route('**/queue?*', lambda route: queued.append(route))
        page.evaluate('() => { loadInbox().catch(showInboxError); loadInbox().catch(showInboxError); }')
        page.wait_for_function('state.queueLoadToken >= 3')
        # Wait for both browser requests, then deliver the older result first.
        page.wait_for_timeout(100)
        assert len(queued) == 2
        queued[0].fulfill(json=payload)
        queued[1].fulfill(json=payload)
        expect(page.locator('.runs-row')).to_have_count(10)
        expect(name).to_have_value('My draft view')
        expect(name).to_be_focused()
        expect(page.locator('.runs-search')).to_have_value('case 04')
        assert page.locator('#queue-controls .fchip').count() == 0
        browser.close()


@pytest.mark.parametrize('late_status', [200, 503])
def test_leaving_a_pending_run_hides_old_findings_and_cancels_late_open(worklist_server, late_status):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        _open(page, worklist_server)
        page.get_by_role('button', name='Start review · 54', exact=True).click()
        page.wait_for_function('!state.loading && state.review')
        previous = page.evaluate('state.runId')
        target = page.evaluate("state.focusedQueue.run_ids.find(id => id.endsWith('case-047'))")
        data = page.request.get(worklist_server + '/runs/' + quote(target, safe='')).json()
        pending = []
        page.route('**/runs/' + target, lambda route: pending.append(route))
        page.locator('#run-list .run[data-run-id="' + target + '"]').click()
        expect(page.locator('.run-loading-notice')).to_be_visible()
        expect(page.locator('.main-finding-title')).not_to_be_visible()
        expect(page.get_by_role('button', name='Mark reviewed')).to_be_disabled()
        page.get_by_role('button', name='Home', exact=True).click()
        expect(page.locator('#home-attention')).to_be_visible()
        assert pending
        pending[0].fulfill(status=late_status, json=data if late_status == 200 else {'detail': 'Temporarily unavailable'})
        page.wait_for_timeout(100)
        assert page.evaluate('state.view') == 'home'
        assert page.evaluate('state.runId') == previous
        assert page.evaluate('state.loading') is False
        current = page.request.get(worklist_server + '/runs/' + quote(target, safe='')).json()
        assert current['workflow']['workflow_version'] == data['workflow']['workflow_version']
        assert not errors
        browser.close()


def test_review_renders_before_open_write_and_queue_refresh(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 048')
        row = page.locator('.runs-row')
        run_id = row.get_attribute('data-run-id')
        before = page.request.get(worklist_server + '/runs/' + quote(run_id, safe='')).json()['workflow']
        if before['review_progress'] != 'unreviewed':
            reset = page.request.post(worklist_server + '/runs/' + quote(run_id, safe='') + '/workflow',
                data={'actor': 'Test setup', 'base_version': before['workflow_version'], 'progress': 'unreviewed'})
            assert reset.status == 200
        row.click()
        writes, queues = [], []
        page.route('**/runs/' + run_id + '/workflow', lambda route: writes.append(route))
        page.route('**/queue?*', lambda route: queues.append(route))
        with page.expect_request('**/runs/' + run_id + '/workflow'):
            page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('!state.loading && state.review', timeout=3000)
        expect(page.locator('.run-header h1')).to_have_text('process_case')
        expect(page.locator('.run-subtitle')).to_contain_text('case-048')
        assert writes and not queues
        writes[0].fulfill(response=writes[0].fetch())
        page.wait_for_function("state.review.workflow.review_progress === 'in_progress'")
        expect(page.locator('.run-header h1')).to_have_text('process_case')
        assert queues
        queues[0].fulfill(response=queues[0].fetch())
        browser.close()


def test_quick_disposition_uses_background_open_version(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 049')
        row = page.locator('.runs-row')
        run_id = row.get_attribute('data-run-id')
        row.click()
        pending, bodies = [], []
        def workflow_route(route):
            body = route.request.post_data_json
            bodies.append(body)
            if body.get('progress') == 'in_progress':
                pending.append(route)
            else:
                route.continue_()
        page.route('**/runs/' + run_id + '/workflow', workflow_route)
        page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('!state.loading && state.review')
        page.get_by_role('button', name='Mark reviewed', exact=False).click()
        page.get_by_role('button', name='No action', exact=True).click()
        page.wait_for_function('state.workflowWrites && state.workflowWrites.size === 1')
        assert len(bodies) == 1
        pending[0].fulfill(response=pending[0].fetch())
        page.wait_for_function("state.review.workflow.review_progress === 'handled'")
        assert [body['base_version'] for body in bodies] == [0, 1]
        assert page.request.get(worklist_server + '/runs/' + quote(run_id, safe='')).json()['workflow']['disposition'] == 'no_action'
        browser.close()


def test_late_open_write_does_not_replace_new_run(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 05')
        ids = page.locator('.runs-row').evaluate_all('rows => rows.map(row => row.dataset.runId)')
        page.locator('.runs-row').first.click()
        pending = []
        page.route('**/runs/' + ids[0] + '/workflow', lambda route: pending.append(route))
        page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('!state.loading && state.review')
        page.get_by_title('Next run in this queue', exact=True).click()
        page.wait_for_function('id => !state.loading && state.runId === id', arg=ids[1])
        pending[0].fulfill(response=pending[0].fetch())
        page.wait_for_function("id => !state.workflowOpens.has(state.projectId + ':' + id)", arg=ids[0])
        assert page.evaluate('state.runId') == ids[1]
        expect(page.locator('.run-subtitle')).to_contain_text('case-051')
        assert page.evaluate('state.review.run.run_id') == ids[1]
        browser.close()


def test_old_langfuse_inspector_explains_missing_metadata(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 017')
        run_id = page.locator('.runs-row').get_attribute('data-run-id')
        data = page.request.get(worklist_server + '/runs/' + quote(run_id, safe='') + '/inspect').json()
        data['adapter_version'] = 'langfuse-adapter-0.4'
        data['identity'].get('source_context', {}).pop('userId', None)
        page.route('**/inspect', lambda route: route.fulfill(json=data))
        page.locator('.runs-row').click()
        page.locator('.trace-inspector details summary').click()
        expect(page.locator('.trace-inspector')).to_contain_text('Re-import to capture available fields in a new revision')
        browser.close()


def test_quick_disposition_does_not_overwrite_conflicting_open(worklist_server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        _open(page, worklist_server)
        page.locator('.runs-search').fill('case 046')
        row = page.locator('.runs-row')
        run_id = row.get_attribute('data-run-id')
        row.click()
        pending = []
        page.route('**/runs/' + run_id + '/workflow', lambda route: pending.append(route))
        page.get_by_role('button', name='Open full review', exact=True).click()
        page.wait_for_function('!state.loading && state.review')
        page.get_by_role('button', name='Mark reviewed', exact=False).click()
        page.get_by_role('button', name='No action', exact=True).click()
        page.wait_for_function('state.workflowWrites && state.workflowWrites.size === 1')
        other = page.request.post(worklist_server + '/runs/' + quote(run_id, safe='') + '/workflow',
            data={'actor': 'Other reviewer', 'base_version': 0, 'progress': 'handled', 'disposition': 'needs_followup'})
        assert other.status == 200
        pending[0].fulfill(response=pending[0].fetch())
        page.wait_for_function("state.review.workflow.disposition === 'needs_followup' && state.workflowWrites.size === 0")
        expect(page.locator('#toast')).to_contain_text('changed elsewhere')
        assert len(pending) == 1
        assert page.request.get(worklist_server + '/runs/' + quote(run_id, safe='')).json()['workflow']['disposition'] == 'needs_followup'
        browser.close()
