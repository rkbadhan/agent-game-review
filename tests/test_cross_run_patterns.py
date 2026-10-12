"""Cross-run patterns: source provenance, stored diagnostics and honest coverage."""
import copy
import json
from pathlib import Path

import pytest

from agr import fleet, read
from agr.checks import CheckExtractionError, extract_checks
from agr.error_signature import error_signature
from agr.ingest_harbor import convert, _explicit_tool_status, build_test_diagnostic
from agr.pipeline import analyze
from agr.store import Store
from agr.version import ERROR_SIGNATURE_VERSION


def _trial(root, *, extra=None, step_extra=None, agent='claude-code', content='boom', checksum='abc'):
    (root / 'agent').mkdir(parents=True, exist_ok=True)
    trajectory = {'schema_version': 'ATIF-v1.7', 'trajectory_id': 'session',
                  'agent': {'name': agent, 'model_name': 'model-a'}, 'steps': [
        {'source': 'user', 'message': 'Make the program work'},
        {'source': 'agent', 'tool_calls': [{'tool_call_id': 'c1', 'function_name': 'bash', 'arguments': {'command': 'test'}}],
         'extra': step_extra or {}, 'observation': {'results': [
             {'source_call_id': 'c1', 'content': content, 'extra': extra or {}}]}}]}
    (root / 'agent/trajectory.json').write_text(json.dumps(trajectory), encoding='utf-8')
    (root / 'result.json').write_text(json.dumps({'id': 'trial-uuid', 'task_name': 'polyglot',
        'task_checksum': checksum, 'reward': 0}), encoding='utf-8')
    (root / 'config.json').write_text(json.dumps({'task': {'source': 'terminal-bench',
        'git_url': 'https://example.org/tasks.git', 'git_commit_id': 'deadbeef', 'path': 'polyglot'}}), encoding='utf-8')
    (root / 'verifier').mkdir(exist_ok=True)
    (root / 'verifier/ctrf.json').write_text(json.dumps({'results': {'tests': [
        {'name': 'test_outputs.py::test_polyglot', 'status': 'failed',
         'trace': "preamble\nE   AssertionError: f(42): expected 267914296, got C=6\n1 failed"}]}}), encoding='utf-8')
    return root


def _doc(rid, *, model='m1', status='failed', signature='AssertionError: missing output', version='v1', checks=None):
    return {'atif_version': '1.7', 'run': {'logical_run_id': rid, 'task_id': 'task-a',
        'task_version': version, 'verifier_version': version, 'agent': 'agent', 'model': model},
        'task': {'instruction': 'Produce output'}, 'steps': [{'step_id': 's1', 'kind': 'task_received', 'actor': 'harness', 'content': 'Produce output'}],
        'capabilities': {'verifier_results': 'complete'},
        'verifier': {'checks': checks if checks is not None else [{
            'check_id': 'test_outputs.py::test_output', 'name': 'test_outputs.py::test_output',
            'status': status, 'source': 'native_structured', 'timing': 'post_run',
            'diagnostic': build_test_diagnostic(signature, 'verifier/ctrf.json#/results/tests/0')}]}}


@pytest.mark.parametrize('extra,expected', [
    ({'exit_code': 2, 'is_error': False}, {'exit_code': 2, 'status': 'error', 'status_source': 'extra.exit_code'}),
    ({'exit_code': 0}, {'exit_code': 0, 'status': 'ok', 'status_source': 'extra.exit_code'}),
    ({'exit_code': True, 'is_error': False}, {'status': 'ok', 'status_source': 'extra.is_error'}),
    ({'exit_code': '2', 'is_error': True}, {'status': 'error', 'status_source': 'extra.is_error'}),
    ({'is_error': 'false'}, {}),
])
def test_explicit_status_validates_types_and_precedence(extra, expected):
    assert _explicit_tool_status({'extra': extra}, {}, 'claude-code') == expected


@pytest.mark.parametrize('metadata_key', ['tool_result_metadata', 'metadata'])
@pytest.mark.parametrize('flag', [True, False])
def test_metadata_matches_nonempty_result_id(metadata_key, flag):
    step = {'extra': {metadata_key: {'raw_tool_result': {'tool_use_id': 'c1', 'is_error': flag}}}}
    result = _explicit_tool_status({'source_call_id': 'c1'}, step, 'claude-code')
    assert result == {'status': 'error' if flag else 'ok', 'status_source': 'step.' + metadata_key}
    for cid in ('c2', '', None):
        assert _explicit_tool_status({'source_call_id': cid}, step, 'claude-code') == {}


def test_metadata_multiple_results_never_broadcasts_a_flag(tmp_path):
    path = _trial(tmp_path, step_extra={'tool_result_metadata': {'raw_tool_result': {'tool_use_id': 'c1', 'is_error': True}}})
    p = path / 'agent/trajectory.json'; traj = json.loads(p.read_text())
    traj['steps'][1]['observation']['results'].append({'source_call_id': 'c2', 'content': 'other'})
    p.write_text(json.dumps(traj))
    results = [s for s in convert(path).doc['steps'] if s['kind'] == 'tool_result']
    assert results[0]['status'] == 'error'
    assert 'status' not in results[1]


def test_malformed_metadata_falls_through_to_valid_alias():
    step = {'extra': {'tool_result_metadata': {'raw_tool_result': {'tool_use_id': 'c1', 'is_error': 'yes'}},
                      'metadata': {'raw_tool_result': {'tool_use_id': 'c1', 'is_error': False}}}}
    assert _explicit_tool_status({'source_call_id': 'c1'}, step, 'claude-code')['status'] == 'ok'


@pytest.mark.parametrize('content,expected', [
    ('stdout\n<exit_code>2</exit_code>', 2), ('<exit_code>0</exit_code>', 0),
    ('<exit_code>-1</exit_code>', -1), ('<exit_code>true</exit_code>', None),
    ('<exit_code> 2 </exit_code>', None), ('<exit_code>1</exit_code><exit_code>0</exit_code>', None),
    ('<exit_code>' + '9' * 5000 + '</exit_code>', None),
])
def test_judy_exact_tags_are_producer_scoped(content, expected):
    result = _explicit_tool_status({'content': content}, {}, 'judy')
    assert result.get('exit_code') == expected
    assert _explicit_tool_status({'content': content}, {}, 'terminus-2') == {}


def test_harness_wrapper_never_becomes_signature():
    assert error_signature('[error] tool reported failure') == '<empty>'
    assert error_signature('[error] tool reported failure\npython3: command not found') == 'python3: command not found'
    assert error_signature('prefix [error] tool reported failure') != '<empty>'


def test_diagnostic_and_status_survive_conversion_storage_and_api(tmp_path):
    doc = convert(_trial(tmp_path / 'source', extra={'is_error': True})).doc
    store = Store(str(tmp_path / 'store')); analysis = analyze(doc, store)
    review = read.get_review(store, analysis.run_source.run_id)
    diagnostic = review['checks'][0]['diagnostic']
    assert diagnostic['line'] == 'AssertionError: f(42): expected 267914296, got C=6'
    assert diagnostic['basis'] == 'pytest_assertion'
    assert diagnostic['source_pointer'] == 'verifier/ctrf.json#/results/tests/0'
    assert diagnostic['signature_version'] == ERROR_SIGNATURE_VERSION
    events = store.read_derived(analysis.run_source.run_id, analysis.run_source.source_capture_id, 'events.json')
    payload = next(e['payload'] for e in events if e['event_type'] == 'tool_result')
    assert payload['status_source'] == 'extra.is_error' and 'exit_code' not in payload
    pytest.importorskip('fastapi'); pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from agr.api import create_app
    client = TestClient(create_app(str(tmp_path / 'store')))
    assert client.get('/runs/' + analysis.run_source.run_id).json()['checks'][0]['diagnostic'] == diagnostic
    response = client.get('/fleet/test-failures?limit=1&offset=0')
    assert response.status_code == 200 and response.headers['X-Total-Count'] == '1'
    assert response.json()[0]['runs'] == 1
    assert client.get('/fleet/test-failures?group_by=tool').status_code == 400
    assert client.get('/fleet/test-failures?limit=0').status_code == 422
    assert client.get('/fleet/test-failures?offset=1').json() == []


def test_checksum_versions_preserve_git_provenance_and_explicit_task(tmp_path):
    path = _trial(tmp_path)
    result = convert(path); run = result.doc['run']
    assert run['task_id'] == 'terminal-bench/polyglot'
    assert run['task_version'] == run['verifier_version'] == 'checksum:abc'
    assert run['version_provenance']['git_commit_id'] == 'deadbeef'
    assert convert(path, task_id='custom').doc['run']['task_id'] == 'custom'
    assert convert(path, verifier={'checks': []}).doc['run'].get('verifier_version') is None
    _trial(tmp_path, checksum=None)
    assert convert(path).doc['run']['task_version'] == 'git:deadbeef'
    (path / 'config.json').unlink()
    assert 'task_version' not in convert(path).doc['run']


def test_diagnostic_redaction_truncation_and_validation(tmp_path):
    text = 'E   AssertionError: token sk-' + 'a' * 48 + '\n' + 'x' * 3000
    diagnostic = build_test_diagnostic(text, 'verifier/ctrf.json#/results/tests/0')
    assert 'sk-' not in diagnostic['line'] and 'sk-' not in diagnostic['excerpt']
    assert len(diagnostic['excerpt']) == 2000 and diagnostic['truncated']
    doc = _doc('r'); doc['verifier']['checks'][0]['diagnostic'] = {'line': 3}
    with pytest.raises(CheckExtractionError, match='string'):
        analyze(doc, Store(str(tmp_path / 'store')))


def test_unknown_attempts_and_partial_signature_coverage(tmp_path):
    store = Store(str(tmp_path))
    for doc in [_doc('a'), _doc('b', model='m2'), _doc('c', model='m2', signature='ValueError: different'),
                _doc('d', status='unknown')]:
        analyze(doc, store)
    group = fleet.test_failure_groups(store)[0]
    assert group['runs'] == 2 and group['attempts_total'] == 4
    assert group['attempts_with_verdict'] == 3 and group['unknown_attempts'] == 1
    assert group['failing_share'] == .5 and group['consistent_across_configurations']
    assert not group['always_fails_task'] and not group['same_failure_every_attempt']
    assert group['no_observed_pass']
    analyze(_doc('d', status='failed', signature='ValueError: different'), store)
    group = fleet.test_failure_groups(store)[0]
    assert group['always_fails_task'] and not group['same_failure_every_attempt']
    assert group['attempts_total'] == 4  # latest capture, not five captures


def test_task_and_verifier_versions_separate_cohorts(tmp_path):
    store = Store(str(tmp_path))
    analyze(_doc('a'), store); analyze(_doc('b', version='v2'), store)
    c = _doc('c'); c['run']['verifier_version'] = 'other'; analyze(c, store)
    assert len(fleet.test_failure_groups(store)) == 3
    assert all(g['attempts_total'] == 1 for g in fleet.test_failure_groups(store))
    merged = fleet.test_failure_groups(store, ['signature'])[0]
    assert merged['attempts_total'] == 3 and merged['cohort_count'] == 3
    assert not merged['consistent_across_configurations'] and not merged['always_fails_task']


def test_unresolved_versions_suppress_configuration_claim(tmp_path):
    store = Store(str(tmp_path)); analyze(_doc('a', version=None), store)
    group = fleet.test_failure_groups(store)[0]
    assert not group['versions_resolved'] and not group['consistent_across_configurations']


def test_atomic_post_run_checks_only_and_distinct_runs(tmp_path):
    doc = _doc('r'); base = doc['verifier']['checks'][0]
    doc['verifier']['checks'] += [dict(base, check_id='other', name='other'),
        dict(base, check_id='terminal_bench_reward'), dict(base, check_id='during', timing='during_run'),
        dict(base, check_id='interpreted', source='output_interpretation')]
    store = Store(str(tmp_path)); analyze(doc, store)
    assert len(fleet.test_failure_groups(store)) == 2
    task = fleet.test_failure_groups(store, ['task'])[0]
    assert task['runs'] == 1 and task['attempts_total'] == 1


def test_operational_errors_stay_in_coverage_and_examples_are_bounded(tmp_path):
    store = Store(str(tmp_path))
    for i in range(5): analyze(_doc(str(i)), store)
    analyze(_doc('op', status='operational_error'), store)
    group = fleet.test_failure_groups(store)[0]
    assert len(group['example_runs']) == 3 and len(group['run_ids']) == 5
    assert group['attempts_total'] == 6 and group['unknown_attempts'] == 1
    assert not group['always_fails_task']
    analyze(_doc('0', status='passed'), store)
    assert fleet.test_failure_groups(store)[0]['runs'] == 4


def test_demo_retains_the_actual_c_six_diagnostic(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'agr/demo_fixtures/real_demo/runs/harbor__terminal-bench__polyglot-c-py__6c3b4b8b-991.atif.json'
    store = Store(str(tmp_path)); a = analyze(json.loads(path.read_text(encoding='utf-8')), store)
    review = read.get_review(store, a.run_source.run_id)
    assert any('Python=267914296, C=6' in c.get('diagnostic', {}).get('line', '') for c in review['checks'])



def test_reimport_upgrades_capture_without_duplicating_logical_run(tmp_path):
    new = convert(_trial(tmp_path / 'source')).doc
    old = copy.deepcopy(new)
    old['adapter_version'] = 'harbor-adapter-0.13'
    old['run']['task_id'] = 'polyglot'
    for key in ('task_version', 'verifier_version', 'version_provenance'):
        old['run'].pop(key, None)
    old['verifier']['checks'][0].pop('diagnostic')
    store = Store(str(tmp_path / 'store'))
    original = analyze(old, store); refreshed = analyze(new, store)
    assert original.run_source.run_id == refreshed.run_source.run_id
    assert refreshed.run_source.capture_revision == 2
    assert refreshed.run_source.supersedes_source_capture_id == original.run_source.source_capture_id
    analyze(new, store)
    assert len(store.read_index(refreshed.run_source.run_id)) == 2
    assert len(read.list_runs(store)) == 1
    assert store.read_source(original.run_source.run_id, original.run_source.source_capture_id) == old


def test_ctrf_message_fallback_when_trace_is_not_text(tmp_path):
    path = _trial(tmp_path)
    p = path / 'verifier/ctrf.json'; report = json.loads(p.read_text())
    report['results']['tests'][0].update(trace={'invalid': 'shape'}, message='ValueError: from message')
    p.write_text(json.dumps(report))
    diagnostic = convert(path).doc['verifier']['checks'][0]['diagnostic']
    assert diagnostic['line'] == 'ValueError: from message'



def test_comparison_projection_preserves_metrics_without_full_review_reads(tmp_path, monkeypatch):
    from agr import versions
    store = Store(str(tmp_path)); a = analyze(_doc('a'), store)
    expected = read.get_review(store, a.run_source.run_id)
    def forbid_full_review(*args, **kwargs):
        raise AssertionError('Comparison must not load full per-run reviews')
    monkeypatch.setattr(read, 'get_review', forbid_full_review)
    records = versions._runs(store)
    assert len(records) == 1
    assert records[0] == {key: expected[key] for key in records[0]}



def test_slow_fleet_scan_still_populates_cache_and_writes_invalidate_it(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); clock = [100.0]; calls = []
    def slow_scan(*args, **kwargs):
        calls.append(1); clock[0] += 5
        return [{'run_id': 'a'}]
    monkeypatch.setattr(read.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(read, '_list_runs_uncached', slow_scan)
    assert read.list_runs(store) == read.list_runs(store)
    assert len(calls) == 1
    store._write_seq += 1
    read.list_runs(store)
    assert len(calls) == 2
    clock[0] += read._LIST_RUNS_CACHE_TTL_S + 1
    read.list_runs(store)
    assert len(calls) == 3



def test_patterns_use_headers_without_loading_full_reviews(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); analyze(_doc('a'), store)
    expected = read.list_runs(store)[0]
    real_read = store.read_derived
    def small_records_only(rid, cid, name):
        assert name not in {'events.json', 'detector_results.json', 'review_moments.json', 'contract.json'}
        return real_read(rid, cid, name)
    monkeypatch.setattr(store, 'read_derived', small_records_only)
    monkeypatch.setattr(read, 'list_runs', lambda *_: pytest.fail('Patterns must not build the inbox'))
    header = read.list_run_headers(store)[0]
    for key in ('run_id', 'capture_id', 'task_id', 'agent', 'model'):
        assert header[key] == expected[key]
    assert {key: header['outcome'][key] for key in expected['outcome']} == expected['outcome']
    assert fleet.test_failure_groups(store)[0]['runs'] == 1
    assert fleet.fleet_episodes(store) == []
    assert fleet.fleet_execution_quality(store)['by_outcome']['fail']['runs'] == 1


def test_pattern_headers_keep_pinned_capture_and_project_isolation(tmp_path):
    from agr.capture_view import PinnedStore
    from agr.workspace_api import ScopedStore
    store = Store(str(tmp_path / 'one'))
    old = analyze(_doc('a'), store)
    analyze(_doc('a', status='passed', model='m2'), store)
    pinned = PinnedStore(store, 'a', old.run_source.source_capture_id)
    scoped = ScopedStore(store)
    assert read.list_run_headers(scoped)[0]['outcome']['status'] == 'PASSED'
    token = scoped.current.set(pinned)
    try:
        assert read.list_run_headers(scoped)[0]['outcome']['status'] == 'FAILED'
        assert fleet.test_failure_groups(scoped)[0]['runs'] == 1
    finally:
        scoped.current.reset(token)
    assert fleet.test_failure_groups(scoped) == []
    other = Store(str(tmp_path / 'two'))
    analyze(_doc('b'), other)
    token = scoped.current.set(other)
    try:
        assert [r['run_id'] for r in read.list_run_headers(scoped)] == ['b']
    finally:
        scoped.current.reset(token)


def test_test_patterns_share_slow_check_scan_and_invalidate_writes(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); analyze(_doc('a'), store)
    clock = [100.0]; calls = []
    monkeypatch.setattr(fleet.time, 'monotonic', lambda: clock[0])
    real_read = store.read_derived
    def slow_checks(rid, cid, name):
        if name == 'checks.json':
            calls.append(1); clock[0] += 5
        return real_read(rid, cid, name)
    monkeypatch.setattr(store, 'read_derived', slow_checks)
    assert fleet.test_failure_groups(store)[0]['runs'] == 1
    assert fleet.test_failure_groups(store, ['task'])[0]['runs'] == 1
    assert len(calls) == 1
    analyze(_doc('a', status='passed'), store)
    assert fleet.test_failure_groups(store) == []
    assert len(calls) == 2


def test_pattern_headers_and_checks_refresh_after_external_write(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); analyze(_doc('a'), store)
    clock = [100.0]
    monkeypatch.setattr(fleet.time, 'monotonic', lambda: clock[0])
    assert fleet.test_failure_groups(store)[0]['runs'] == 1
    external = Store(str(tmp_path)); analyze(_doc('a', status='passed'), external)
    clock[0] += max(read._RUN_HEADERS_CACHE_TTL_S, fleet._TEST_RECORDS_CACHE_TTL_S) + 1
    assert fleet.test_failure_groups(store) == []


def test_slow_pattern_header_scan_has_a_full_cache_lifetime(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); analyze(_doc('a'), store)
    clock = [100.0]; calls = []
    monkeypatch.setattr(read.time, 'monotonic', lambda: clock[0])
    real_heads = read._capture_heads
    def slow_heads(s):
        calls.append(1); clock[0] += 5
        return real_heads(s)
    monkeypatch.setattr(read, '_capture_heads', slow_heads)
    assert read.list_run_headers(store) == read.list_run_headers(store)
    assert len(calls) == 1
    store._write_seq += 1
    read.list_run_headers(store)
    assert len(calls) == 2
    clock[0] += read._RUN_HEADERS_CACHE_TTL_S + 1
    read.list_run_headers(store)
    assert len(calls) == 3



@pytest.mark.parametrize('agent,content,extra', [
    ('claude-code', 'Exit code 1\npython3: command not found', {'is_error': True}),
    ('judy', '<exit_code>127</exit_code>\n<command_execution_time>0.006502 second</command_execution_time>\n'
     '<stdout>python3: command not found</stdout>\n<stderr></stderr>', None),
])
def test_clean_harbor_tool_signature_persists_without_changing_status(tmp_path, agent, content, extra):
    from agr import argument_shapes, version
    path = _trial(tmp_path / 'source', agent=agent, content=content, extra=extra)
    doc = convert(path).doc
    store = Store(str(tmp_path / 'store')); a = analyze(doc, store)
    assert len(a.recoveries) == 1
    ep = a.recoveries[0]
    assert ep.error_signature == 'python3: command not found'
    assert ep.failure_diagnostic == 'python3: command not found'
    assert ep.raw_failure_text == content
    assert ep.derivation_version == version.RECOVERY_VERSION
    persisted = store.read_derived(a.run_source.run_id, a.run_source.source_capture_id, 'recoveries.json')[0]
    assert persisted['error_signature'] == ep.error_signature
    assert fleet.fleet_episodes(store)[0].key == ('bash', ep.error_signature)
    # Harbor currently retains hoisted command text rather than tool_input.
    assert argument_shapes.argument_shapes(store) == []
    assert next(e for e in a.events if e.event_type == 'tool_result').payload['status'] == 'error'
