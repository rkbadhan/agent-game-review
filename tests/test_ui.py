"""End-to-end browser test of the forensic evidence browser.

Drives the served SPA with a real Chromium via Playwright, asserting the
synchronized forensic view actually works: selecting a source step lights up
the panel its evidence belongs to, the guided review chapters (§4.4) walk from
the outline rail, and the source view reports a verified hash.

Playwright and a usable browser are optional, so this skips cleanly when either
is absent (e.g. in the default CI matrix, which installs neither). The read
model and HTTP transport are covered dependency-free in ``test_read.py`` /
``test_api.py``; this adds the interaction layer where a browser is available.
"""

import contextlib
import glob
import json
import os
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

from agr import read  # noqa: E402
from agr.api import create_app  # noqa: E402
from agr.model_reviewer import ScriptedReviewer  # noqa: E402
from agr.pipeline import analyze  # noqa: E402
from agr.store import Store  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
CHESS = "chess_best_move__seed42"


def _find_chromium():
    """Locate a pre-installed Chromium if the pip build number mismatches.

    Playwright's bundled download may not match the browser staged in the
    environment; fall back to any chrome under PLAYWRIGHT_BROWSERS_PATH.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        return None
    for pat in ("chromium-*/chrome-linux/chrome",
                "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                "chromium-*/chrome-win/chrome.exe"):
        hits = sorted(glob.glob(os.path.join(root, pat)))
        if hits:
            return hits[-1]
    return None


def _launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        exe = _find_chromium()
        if not exe:
            pytest.skip("no usable Chromium for Playwright")
        return pw.chromium.launch(executable_path=exe)


def _page(browser, **kwargs):
    """A fresh browser page.

    T1: the §4.19 orientation overlay no longer opens automatically on first
    use, so every page already starts straight in the app — nothing to
    pre-dismiss.
    """
    return browser.new_page(**kwargs)


@contextlib.contextmanager
def _serving(store_root):
    """Serve one store on a free port for the duration of a test."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(create_app(store_root), host="127.0.0.1", port=port,
                            log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    else:
        pytest.skip("uvicorn did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


def _analyzed(store, name, **kwargs):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return analyze(json.load(fh), store, **kwargs)


@pytest.fixture
def server(tmp_path):
    root = str(tmp_path / "store")
    store = Store(root)
    for name in ("chess_best_move.atif.json", "contract_mismatch.atif.json"):
        _analyzed(store, name)
    with _serving(root) as base:
        yield base


def _grounded_facts(envelope_moment):
    """The judged candidate's facts in model-payload form.

    Since the Terminal-Bench hardening, Stage G drops a model moment with no
    recomputable-passing fact (an ungrounded claim is unselected, §8.8). A
    scripted moment that mirrors a deterministic candidate must therefore carry
    that candidate's structured facts; ``validated_facts`` on the envelope is
    the original fact plus ``validation``/``recomputed`` annotations, so strip
    the annotations and the facts recompute identically.
    """
    return [{k: v for k, v in f.items() if k not in ("validation", "recomputed")}
            for f in envelope_moment.get("validated_facts", [])]


@pytest.fixture
def compare_server(tmp_path):
    """A store whose chess run carries two reviews, so §4.16 compare is live.

    The second reviewer is scripted (no model call): it mirrors the deterministic
    anchors and adds enrichment, which is the matched-with-diffs case the compare
    surface is built for.
    """
    root = str(tmp_path / "store")
    store = Store(root)
    _analyzed(store, "chess_best_move.atif.json")
    payload = {"moments": [{
        "candidate_id": m["candidate_id"],
        "anchor_event_ids": m["anchor_event_ids"],
        "kind": m["kind"],
        "polarity": m["polarity"],
        "affected_checks": m["affected_checks"],
        "structured_facts": _grounded_facts(m),
        "behaviour_tags": ["failed_to_replan"],
        "better_action": "Write every winning move the engine reported.",
        "root_cause_candidates": [{"locus": "agent_decision", "rationale": "stopped at the first move"}],
    } for m in read.get_review(store, CHESS)["review_moments"] if m.get("selected")]}
    _analyzed(store, "chess_best_move.atif.json",
              reviewer=ScriptedReviewer(payload, source="model:test"))
    with _serving(root) as base:
        yield base


@pytest.fixture
def versions_server(tmp_path):
    """Exactly the store ``agr demo-store`` builds, so the browser test drives what
    a reviewer following the README actually sees."""
    from agr import demo  # noqa: PLC0415

    demo.build_demo_store(Store(str(tmp_path / "store")), FIXTURES)
    with _serving(str(tmp_path / "store")) as base:
        yield base


def _write_claude_session(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(line) for line in lines))


def _claude_edit_failure_session(path, session_id, file_path, extra_input=None):
    """A Claude Code session with one unrecovered, retained-argument Edit
    failure — the same shape test_argument_shapes.py uses. Ingesting two of
    these under a distinct ``file_path`` (and one with an extra key) gives the
    Patterns view a real recovery episode group AND a matching item-31
    argument-shape distribution (two distinct shapes) to drill into."""
    tool_input = {"file_path": file_path, "old_string": "foo", "new_string": "bar"}
    if extra_input:
        tool_input.update(extra_input)
    lines = [
        {"type": "user", "sessionId": session_id,
         "message": {"role": "user", "content": "Fix the bug."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "m",
         "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": tool_input}]}},
        {"type": "user", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "String to replace not found in file.", "is_error": True}]}},
    ]
    _write_claude_session(path, lines)


@pytest.fixture
def patterns_server(tmp_path):
    """A store with a repeated (Edit, "String...not found...") failure across
    two runs — one recovery-episode group (item 30) with two distinct
    retained-argument shapes (item 31) behind it, so the Patterns view has a
    real group to drill into on both axes."""
    from agr.ingest_claude import convert  # noqa: PLC0415

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    a = session_dir / "a.jsonl"
    b = session_dir / "b.jsonl"
    _claude_edit_failure_session(a, "sess-a", "/app/calc.py")
    _claude_edit_failure_session(b, "sess-b", "/app/other.py", extra_input={"replace_all": True})

    store = Store(str(tmp_path / "store"))
    for i, path in enumerate([a, b]):
        analyze(convert(str(path), task_id=f"edit-fail-{i}").doc, store)
    with _serving(str(tmp_path / "store")) as base:
        yield base


def test_version_comparison_surface(versions_server):
    """§4.16: construct a matched slice, read the result, share it, and trace a row
    back to the runs behind it."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(versions_server)
        page.wait_for_selector(".run-header")

        # The comparison is a surface of its own, reachable from the app bar.
        page.click("#versions-button")
        page.wait_for_selector(".vs-construct")
        assert _query(page.url)["view"] == ["versions"]

        # The match report comes before any number (§4.16.1).
        page.click('button:has-text("Preview match")')
        page.wait_for_selector(".vs-report")
        assert page.query_selector(".vs-label").inner_text().lower() == "matched"
        report = page.query_selector(".vs-report").inner_text()
        assert "exact matched tasks" in report and "repeated-run strata" in report

        # Outcome row carries numerator/denominator, a direction, and its interval.
        outcome = page.query_selector(".vs-table").inner_text()
        assert "2/5" in outcome and "5/5" in outcome
        assert "Improved" in outcome
        assert "95% CI" in page.query_selector(".vs-table").inner_text()

        # Opportunity-normalized rows are gated abilities, not verifier checks.
        cards = [c.inner_text() for c in page.query_selector_all(".card")]
        behaviour = next(c for c in cards if "OPPORTUNITY-NORMALIZED" in c.upper())
        assert "Verify before submission" in behaviour
        assert "Artifact exists" not in behaviour

        # The synthesis is labelled interpretation and stays correlational.
        interp = page.query_selector(".vs-interp").inner_text()
        assert "INTERPRETATION" in interp.upper()
        assert "does not isolate causality" in interp

        # Declaring an axis that did not move downgrades the claim (§4.16.1).
        page.select_option('.vs-field:has-text("Declared change axis") select', "model")
        page.click('button:has-text("Preview match")')
        page.wait_for_selector(".vs-label.configuration_comparison")
        assert "configuration comparison" in page.query_selector(".vs-label").inner_text().lower()

        # Save the matched definition and reopen it from its link alone.
        page.select_option('.vs-field:has-text("Declared change axis") select', "evaluation_harness")
        page.click('button:has-text("Preview match")')
        page.wait_for_selector(".vs-label.matched")
        page.click('button:has-text("Save comparison")')
        page.wait_for_function("() => location.search.includes('comparison=')")
        shared = page.url

        other = _page(browser)
        other.goto(shared)
        other.wait_for_selector(".vs-report")
        assert other.query_selector(".vs-label").inner_text().lower() == "matched"

        # Every aggregate opens onto the run pairs behind it, and a pair opens the run.
        drill = other.query_selector_all("details.vs-drill")[-1]
        drill.query_selector("summary").click()
        assert "matched run pair" in drill.query_selector("summary").inner_text()
        drill.query_selector_all(".vs-pair button")[0].click()
        other.wait_for_selector(".run-header")
        assert _query(other.url)["run"]

        browser.close()


def test_runs_workspace_is_full_width_with_search_filter_sort_and_scroll(server):
    """U1/U2: the Runs workspace is a destination of its own — full width, no
    persistent evidence panel — with a table carrying the required columns,
    working search/filter/sort, and a round trip through a run that preserves
    all three plus scroll position."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run-header")  # boot still auto-opens the first run

        page.click("#runs-button")
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url)["view"] == ["runs"]
        # U1: no persistent run-evidence panel or queue sidebar on this surface.
        assert not page.is_visible(".queue")
        assert not page.is_visible(".evidence-panel")
        assert page.evaluate("() => document.body.classList.contains('workspace-mode')")

        # U2 columns.
        headers = [h.inner_text().upper() for h in page.query_selector_all(".runs-table th")]
        assert headers == ["RUN", "OUTCOME", "MAIN FINDING", "REVIEW STATUS", "DURATION", "COST"]
        rows = page.query_selector_all(".runs-row")
        assert len(rows) == 2

        # Search matches task name and run id.
        page.fill(".runs-search", "chess")
        page.wait_for_function("() => document.querySelectorAll('.runs-row').length === 1")
        assert "chess" in page.query_selector(".runs-row").inner_text().lower()
        assert _query(page.url).get("q") == ["chess"]

        # Filters are explicit for outcome (failed/passed/undetermined) and
        # review status (unreviewed/in_progress/handled), not just a sort order.
        chip_labels = {c.inner_text() for c in page.query_selector_all(".runs-controls-row .fchip")}
        assert {"Failed", "Passed", "Undetermined", "Unreviewed", "In progress", "Handled"} <= chip_labels
        page.fill(".runs-search", "")
        page.click('.runs-controls-row .fchip:has-text("Failed")')
        page.wait_for_function("() => location.search.includes('filter=failed')")
        failed_rows = page.query_selector_all(".runs-row")
        assert len(failed_rows) >= 1
        for r in failed_rows:
            assert "FAILED" in r.inner_text().upper() or "ERROR" in r.inner_text().upper()

        # Sorting is reachable from this surface too (shared with the sidebar).
        page.select_option('.runs-controls-row .sort-row select', "cost")
        page.wait_for_function("() => location.search.includes('sort=cost')")

        # Scroll the table, open a run, and return: search box and sort persist
        # in the URL and the reopened workspace's controls.
        run_id = page.query_selector(".runs-row").get_attribute("data-run-id")
        page.evaluate("() => document.querySelector('#main').scrollTo(0, 40)")
        page.click(f'.runs-row[data-run-id="{run_id}"]')
        page.wait_for_selector(".run-header")
        assert _query(page.url)["run"] == [run_id]

        page.click("#runs-button")
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url)["filter"] == ["failed"]
        assert _query(page.url)["sort"] == ["cost"]
        assert page.query_selector('.runs-controls-row .fchip:has-text("Failed")').get_attribute("aria-pressed") == "true"
        assert page.eval_on_selector('.runs-controls-row .sort-row select', "el => el.value") == "cost"

        browser.close()


def test_boot_landing_on_a_run_does_not_push_a_spurious_history_entry(server):
    """U1 regression: `loadInbox()` syncs the URL on its own before boot has
    resolved a destination, which used to seed the history-push baseline from
    the wrong (transient, run-less) state — so auto-opening the first run at
    boot pushed an extra entry, and the first Back press landed back on the
    Runs workspace instead of actually leaving the app. `history.length`
    itself isn't a reliable signal here (a fresh page's `about:blank` counts
    toward it too, independent of the app), so this instruments
    `history.pushState` directly: boot must never call it, only
    `replaceState`."""
    def count_history_calls(page):
        page.add_init_script("""
          window.__pushCalls = 0;
          const origPush = history.pushState.bind(history);
          history.pushState = function(...args) { window.__pushCalls++; return origPush(...args); };
        """)

    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        count_history_calls(page)
        page.goto(server)
        page.wait_for_selector(".run-header")
        assert page.evaluate("() => window.__pushCalls") == 0

        # Same for a shared link straight to a run — dispatchLocation's other
        # boot-time path.
        run_id = _query(page.url)["run"][0]
        direct = _page(browser)
        count_history_calls(direct)
        direct.goto(server + f"/?run={run_id}")
        direct.wait_for_selector(".run-header")
        assert direct.evaluate("() => window.__pushCalls") == 0

        browser.close()


def test_browser_back_restores_workspace_and_run_destinations(server):
    """U1: Browser Back/Forward moves between the app's actual destinations —
    a run's investigation shell, the Runs workspace, and Patterns — not just
    within one of them."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run-header")
        first_run = _query(page.url)["run"][0]

        page.click("#runs-button")
        page.wait_for_selector(".runs-table-card")
        page.click("#fleet-button")
        page.wait_for_selector(".review-nav")
        assert _query(page.url)["view"] == ["fleet"]

        page.go_back()
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url)["view"] == ["runs"]

        page.go_back()
        page.wait_for_selector(".run-header")
        assert _query(page.url)["run"] == [first_run]

        page.go_forward()
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url)["view"] == ["runs"]

        browser.close()


def test_browser_back_to_runs_refreshes_the_table_under_restored_filters(server):
    """U1/U2 regression: readUrl() restores state.filters/state.sort from the
    URL a Back press lands on, but the Runs table itself was still painted
    from whatever state.queue held from the last loadInbox() call — so a
    filter changed from the investigation sidebar (while a run was open,
    which only replaceState's that run's OWN history entry) used to keep
    showing that newer, filtered queue underneath the older, just-restored
    'no filter' chip state once Back returned to an earlier unfiltered Runs
    page."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run-header")

        # An unfiltered Runs page goes on the history stack first.
        page.click("#runs-button")
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url).get("filter") is None
        unfiltered_count = len(page.query_selector_all(".runs-row"))
        assert unfiltered_count == 2

        # Open a run (a new history entry), then change a filter from the
        # investigation sidebar — this updates state.queue and replaceState's
        # the RUN's own URL; the earlier Runs entry above keeps its old,
        # filter-less query string.
        run_id = page.query_selector(".runs-row").get_attribute("data-run-id")
        page.click(f'.runs-row[data-run-id="{run_id}"]')
        page.wait_for_selector(".run-header")
        # Both fixture runs have a failing check (overall outcome FAILED), so
        # "Passed" is guaranteed to actually shrink the queue (to 0) rather
        # than coincidentally leaving it at 2 either way.
        page.click('#queue-controls .fchip:has-text("Passed")')
        page.wait_for_function("() => location.search.includes('filter=passed')")

        # Back returns to the unfiltered Runs entry: the URL and the filter
        # chip must agree with the table actually shown.
        page.go_back()
        page.wait_for_selector(".runs-table-card")
        assert _query(page.url).get("filter") is None
        assert page.query_selector('.runs-controls-row .fchip:has-text("Passed")') \
            .get_attribute("aria-pressed") == "false"
        page.wait_for_function("() => document.querySelectorAll('.runs-row').length === 2")

        browser.close()


def test_patterns_surface_representative_episodes_and_argument_shapes(patterns_server):
    """U3: a Patterns group's evidence links open the exact run and event, and
    a reviewer can inspect representative episodes and argument shapes inline
    without leaving the surface."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(patterns_server)
        page.wait_for_selector(".run-header")

        page.click("#fleet-button")
        page.wait_for_selector(".vs-table")
        assert not page.is_visible(".queue")
        assert not page.is_visible(".evidence-panel")

        headers = [h.inner_text().upper() for h in page.query_selector_all(".vs-table th")]
        assert "EPISODES" in headers and "ARGUMENT SHAPES" in headers

        row = page.query_selector(".vs-table tbody tr") or page.query_selector_all(".vs-table tr")[1]
        drills = row.query_selector_all("details.vs-drill")
        episodes_drill, shapes_drill = drills[0], drills[1]

        episodes_drill.query_selector("summary").click()
        assert "representative episode" in episodes_drill.inner_text().lower()

        shapes_drill.query_selector("summary").click()
        shapes_text = shapes_drill.inner_text()
        assert "file_path:str" in shapes_text
        assert "shape(s) among 2 failing call(s)" in shapes_text

        # Evidence link opens the exact run and event.
        episodes_drill.query_selector(".vs-pair button").click()
        page.wait_for_selector("#trace-drawer.open")
        assert _query(page.url)["run"]

        browser.close()


def test_patterns_regrouping_mid_load_lands_on_the_latest_choice(patterns_server):
    """U3 regression: switching the Patterns group-by while the PREVIOUS
    grouping's fetch is still in flight used to leave the screen stuck on
    "Loading fleet episodes…" if that in-flight fetch's response landed after
    the switch (nothing re-rendered the now-current, unrelated in-flight
    request's completion into the visible DOM) — or, worse, paint the OLD
    grouping's data under the NEW grouping's selected chip. Delaying the
    first request and letting the second resolve immediately forces exactly
    that out-of-order landing; the surface must still end up showing the
    LAST grouping clicked, matching data and chip together.

    The delay is injected by wrapping window.fetch in the page itself (a
    setTimeout-based, non-blocking delay) rather than via Playwright's
    Python-side route interception: a blocking time.sleep() in a sync route
    handler runs on Playwright's own driver thread and serializes the two
    Python-side page.click() calls instead of letting them race, which
    defeats the point of the test.
    """
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.add_init_script("""
          window.__fleetDelay = false;
          const origFetch = window.fetch.bind(window);
          window.fetch = (url, opts) => {
            if (window.__fleetDelay && typeof url === "string" && /group_by=tool(?!%2C|,)/.test(url)) {
              return new Promise(resolve => setTimeout(() => resolve(origFetch(url, opts)), 600));
            }
            return origFetch(url, opts);
          };
        """)
        page.goto(patterns_server)
        page.wait_for_selector(".run-header")

        page.click("#fleet-button")
        page.wait_for_selector(".vs-table")  # initial "Tool + error" load settles

        # "Tool only" is requested first but answered last (artificially
        # delayed); "Error only" is requested second but answered first —
        # the exact out-of-order landing the fix must handle.
        page.evaluate("() => { window.__fleetDelay = true; }")
        page.click('.vs-keys .fchip:has-text("Tool only")')
        page.click('.vs-keys .fchip:has-text("Error only")')

        # A stuck screen (the bug) never re-renders the table at all, so this
        # times out rather than passing vacuously.
        page.wait_for_selector("#main .vs-table", timeout=5000)
        assert page.query_selector('.vs-keys .fchip:has-text("Error only")') \
            .get_attribute("aria-pressed") == "true"
        assert page.query_selector('.vs-keys .fchip:has-text("Tool only")') \
            .get_attribute("aria-pressed") == "false"
        group_cell = page.query_selector_all(".vs-table tr")[1].query_selector("td")
        assert "Edit" not in group_cell.inner_text()

        # The delayed "Tool only" response must not land afterward and flip
        # any of this back — give it time to arrive, then re-check.
        page.wait_for_timeout(800)
        assert page.query_selector('.vs-keys .fchip:has-text("Error only")') \
            .get_attribute("aria-pressed") == "true"
        assert page.query_selector("#main .vs-table")
        group_cell = page.query_selector_all(".vs-table tr")[1].query_selector("td")
        assert "Edit" not in group_cell.inner_text()

        browser.close()


def test_overview_main_finding_is_the_most_prominent_element_and_opens_evidence(server):
    """U4: the main finding reads as the dominant element on the page — bigger
    than the run identifier in the header above it — and "View evidence" is a
    one-click path from the initial view into its supporting evidence, not a
    second manual step after "Open in Key moments"."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".outcome")  # boot lands on Overview by default

        title = page.query_selector(".main-finding-title")
        assert title is not None
        finding_size = page.evaluate("el => parseFloat(getComputedStyle(el).fontSize)", title)
        header_size = page.eval_on_selector(".run-header h1", "el => parseFloat(getComputedStyle(el).fontSize)")
        assert finding_size > header_size

        page.click('button:has-text("View evidence")')
        page.wait_for_selector(".moment-card")
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()
        assert page.query_selector("#evidence-panel .trust-strip") is not None

        browser.close()


def test_workspace_and_full_trace(server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)

        page.wait_for_selector(".run")
        assert len(page.query_selector_all(".run")) == 2

        page.click('.run[data-run-id="chess_best_move__seed42"]')

        # A run now opens on Overview (the synthesis) so it reads in one pass;
        # step into Key moments to exercise the guided moment view below.
        _wait_chip(page, "Overview")
        page.click('.outline > .ochip:has-text("Key moments")')

        # Guided workspace: unified timeline (§4.8) with 3 lanes + moment markers,
        # and the five-block moment card (§4.7.1).
        page.wait_for_selector(".timeline-wrap")
        assert len(page.query_selector_all(".lane-row")) == 3  # phase / progress / moments
        assert page.query_selector(".moment-mark") is not None
        page.wait_for_selector(".moment-card")
        # T2: the collapsed card is concise; the five-part breakdown lives behind
        # the "Full breakdown" expand toggle.
        page.click(".moment-detail summary")
        card = page.query_selector(".moment-card").inner_text().lower()
        # T2: a deterministic moment collapses "Likely impact" and "Better
        # action" into one "AI interpretation" absent block instead of two.
        for block in ("situation", "observed consequence", "ai interpretation"):
            assert block in card

        # §4.2 shell: the header states the review mode and the harness version,
        # and the eyebrow carries the run's position in the active queue.
        assert "Deterministic review" in page.query_selector(".shell-meta").inner_text()
        assert "harbor-0.9" in page.query_selector(".run-subtitle").inner_text()
        assert "OF 2" in page.query_selector(".run-header .eyebrow").inner_text().upper()

        # Evidence panel (§4.9): trust cards carry evidence grade + attribution in
        # plain language, and evidence is grouped by claim.
        panel = page.query_selector("#evidence-panel").inner_text()
        assert "Strong" in panel and "Linked to outcome" in panel
        assert "what the agent did" in panel.lower()

        # The review tabs (T1/§4.2/§4.4) are exactly the three primary chapters;
        # Trace, Source, More analysis and Compare live in the tools cluster on
        # the right. Key moments is the one selected above and is marked current.
        assert [c.inner_text().strip() for c in page.query_selector_all(".outline > .ochip .ochip-label")] == \
            ["Overview", "Key moments", "Checks"]
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()
        assert page.query_selector('.review-util .trace-chip') is not None

        # Open the full-trace slide-over (§4.12): synchronized forensic panels.
        page.click(".trace-chip")
        page.wait_for_selector("#trace-drawer.open")
        assert len(page.query_selector_all("#trace-body .caps .cap")) == 10
        assert len(page.query_selector_all("#trace-body .fsteps .step")) == 9

        # Selecting the artifact step lights the artifact panel (synchronization).
        page.click('#trace-body .step[data-step-id="s7"]')
        lit = page.query_selector("#trace-body .pane.lit")
        assert lit.get_attribute("data-panel") == "artifact"
        assert "/solution.txt" in page.query_selector("#trace-body .pane.lit .pb").inner_text()
        # A tool_call step re-syncs onto the tool-I/O panel.
        page.click('#trace-body .step[data-step-id="s3"]')
        assert page.query_selector("#trace-body .pane.lit").get_attribute("data-panel") == "tool_io"
        page.keyboard.press("Escape")  # close the drawer

        # Overview chapter (T1/§4.5): outcome, main finding, and — further down —
        # a disabled detector listed under review limits.
        page.click('.outline > .ochip:has-text("Overview")')
        page.wait_for_selector(".outcome")
        # AGR-05: the compaction detector is a registered placeholder — it shows
        # the honest "not implemented" chip, not a capability-gated skip.
        assert any("not implemented" in c.inner_text()
                   for c in page.query_selector_all(".limit-item .chip"))

        # ...and the final environment/artifact summary (§4.5) states the closing
        # observed state plus the capability levels that bound it.
        final = page.query_selector(".final-table").inner_text()
        assert "/solution.txt" in final and "Observed" in final and "g2e4" in final
        block = page.query_selector(".final-block").inner_text()
        assert "shell exited 0" in block                       # last process exit
        assert "Artifact capture: checkpoint_only" in block    # what bounds it

        # Checks chapter (T1): the atomic-check table moved here from Overview.
        page.click('.outline > .ochip:has-text("Checks")')
        page.wait_for_selector(".check-list")

        # Task Opportunities (§4.6) and Ability Signature (§4.10) now live under
        # "More analysis" instead of occupying a primary tab.
        page.click(".more-analysis summary")
        page.click('.more-analysis-list .ochip:has-text("Opportunities")')
        page.wait_for_selector(".opp-table")
        assert page.query_selector(".opp-table .stat-badge.measured") is not None

        # A no-opportunity row stays distinct from the measured rows. Selecting
        # Opportunities left the disclosure open on the next render (it
        # reflects the current chapter), so it need not be reopened by hand.
        if not page.is_visible(".more-analysis-list"):
            page.click(".more-analysis summary")
        page.click('.more-analysis-list .ochip:has-text("Ability signature")')
        page.wait_for_selector(".sig-table")
        assert "No opportunity occurred" in page.query_selector(".sig-table").inner_text()

        # Eval Lesson (T1/§4.11) is attached to its recommending moment instead of
        # a chapter of its own; a deterministic review recommends none, so no
        # moment card shows the inline "Eval lesson available" disclosure.
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")
        assert page.query_selector(".moment-lesson") is None

        # Source view (utility, not a chapter) reports the verified hash.
        page.click('.review-util button:has-text("Source")')
        page.wait_for_selector(".verify-ok")

        # A provisional run shows the watermark banner.
        page.click('.run[data-run-id="greeting_report__seed7"]')
        page.wait_for_selector(".watermark")
        assert "PROVISIONAL" in page.query_selector(".watermark").inner_text()

        browser.close()


def test_triage_inbox_disposition_and_feedback(server):
    """The runs inbox (§4.3): sweep summary, filter chips, disposition menu, and
    Tier-1 moment feedback all round-trip through the write API and update in place."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)

        # Sweep summary + full queue (no filter) lists both fixtures.
        page.wait_for_selector("#sweep-summary .sweep-stats")
        assert len(page.query_selector_all(".run")) == 2
        # Header meta shows the harness (implicit sweep, honest — no fake sweep id).
        assert "harbor-0.9" in page.query_selector("#sweep-summary .sweep-name").inner_text()
        # Run cards carry the real step count (§4.3.3).
        card = page.query_selector('.run[data-run-id="chess_best_move__seed42"]').inner_text()
        assert "9 steps" in card

        # The "Failed" chip narrows the queue to failed runs only.
        page.click('.fchip:has-text("Failed")')
        page.wait_for_selector(".run")
        assert page.query_selector('.run[data-run-id="chess_best_move__seed42"]') is not None

        # Open a run and step into Key moments; the five-block card and its
        # actions render (a run now opens on Overview by default).
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.wait_for_selector(".trace-chip")
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card .moment-actions")

        # Tier-1 quick feedback records "Agreed".
        page.click('.moment-actions button:has-text("Agree")')
        page.wait_for_selector(".mfb .tag")
        assert "agreed" in page.query_selector(".mfb").inner_text().lower()

        # Disposition via the bottom-bar menu; the inbox row reflects handled state.
        page.click('.bottom-bar button:has-text("Set disposition")')
        page.wait_for_selector("#disp-menu.open")
        page.click('#disp-menu button:has-text("Corrected")')
        page.wait_for_selector('.run[data-run-id="chess_best_move__seed42"] .review-state.handled')
        assert "Corrected" in page.query_selector(
            '.run[data-run-id="chess_best_move__seed42"] .review-state').inner_text()

        browser.close()


def test_rapid_run_switch_never_lets_a_stale_response_win(server):
    """F4: selecting chess then, before its response lands, greeting must
    always end up showing greeting's identity/review — never a mix where a
    stale (later-arriving) chess response overwrites the already-newer
    greeting selection just because it happened to resolve last."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(server)
        page.wait_for_selector(".run")
        # Delay chess's own API responses so they resolve well AFTER
        # greeting's — the out-of-order race this bug is about. Done entirely
        # in-page (wrapping window.api), not via Playwright route
        # interception: a blocking delay in a Python route handler blocks the
        # sync API's own dispatcher thread too (it shares one driver
        # connection with page.click() etc.), so it cannot produce a genuine
        # race between two rapid clicks — this can.
        page.evaluate("""() => {
          const real = window.api;
          window.api = function(path) {
            if (path.includes('chess_best_move')) {
              return new Promise((resolve, reject) => {
                setTimeout(() => { real(path).then(resolve, reject); }, 800);
              });
            }
            return real(path);
          };
        }""")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.click('.run[data-run-id="greeting_report__seed7"]')

        # Greeting has no delay, so it lands first and settles the view.
        page.wait_for_function(
            "() => document.querySelector('#crumb-task')?.textContent === 'greeting-report'",
            timeout=5000)

        # Give the deliberately-delayed, now-stale chess response time to
        # arrive; if the staleness guard did not work it would clobber the view.
        time.sleep(1.2)

        assert page.query_selector("#crumb-task").inner_text() == "greeting-report"
        assert page.query_selector('.run[data-run-id="greeting_report__seed7"].active') is not None
        mono = page.query_selector(".run-subtitle .mono")
        assert mono.inner_text() == "greeting_report__seed7"

        assert errors == []
        browser.close()


def test_annotation_actions_disabled_while_a_newer_selection_is_loading(server):
    """F4: once chess is fully loaded and its moment actions are live,
    starting a (delayed) switch to greeting must disable those still-visible
    chess actions immediately — a click during the pending window would
    otherwise write a chess moment_id against whatever run/reviewer lands."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(server)
        page.wait_for_selector(".run")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card .moment-actions")
        agree = page.query_selector('.moment-actions button:has-text("Agree")')
        assert not agree.is_disabled()

        page.evaluate("""() => {
          const real = window.api;
          window.api = function(path) {
            if (path.includes('greeting_report')) {
              return new Promise((resolve, reject) => {
                setTimeout(() => { real(path).then(resolve, reject); }, 800);
              });
            }
            return real(path);
          };
        }""")
        page.click('.run[data-run-id="greeting_report__seed7"]')
        # While greeting's (delayed) response is in flight, the actions still
        # on screen (chess's, since the view has not been replaced yet) must
        # be disabled — not just visually stale but non-interactive.
        page.wait_for_function(
            "() => { const b = [...document.querySelectorAll('.moment-actions button')]"
            ".find(x => x.textContent.includes('Agree')); return !!b && b.disabled; }",
            timeout=2000)

        # The switch still completes correctly once greeting's response lands.
        page.wait_for_function(
            "() => document.querySelector('#crumb-task')?.textContent === 'greeting-report'",
            timeout=5000)

        assert errors == []
        browser.close()


def test_disposition_shortcut_honors_the_same_loading_guard_as_its_button(server):
    """Review of PR #64: the 'd' keyboard shortcut calls toggleDisposition()
    directly, bypassing the disabled state the bottom-bar button itself gets
    while a newer run/reviewer selection is loading — pressing d during that
    window must not open the menu either."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(server)
        page.wait_for_selector(".run")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.wait_for_selector(".bottom-bar button:has-text('Set disposition')")

        page.evaluate("""() => {
          const real = window.api;
          window.api = function(path) {
            if (path.includes('greeting_report')) {
              return new Promise((resolve, reject) => {
                setTimeout(() => { real(path).then(resolve, reject); }, 800);
              });
            }
            return real(path);
          };
        }""")
        page.click('.run[data-run-id="greeting_report__seed7"]')
        # While greeting's response is still in flight, the button is
        # disabled — confirm the guard is actually armed for this window...
        page.wait_for_function(
            "() => { const b = [...document.querySelectorAll('.bottom-bar button')]"
            ".find(x => x.textContent.includes('Set disposition')); return !!b && b.disabled; }",
            timeout=2000)
        # ...then the shortcut, not the button, must respect it too.
        page.keyboard.press("d")
        page.wait_for_timeout(150)
        assert page.query_selector("#disp-menu.open") is None

        page.wait_for_function(
            "() => document.querySelector('#crumb-task')?.textContent === 'greeting-report'",
            timeout=5000)
        assert errors == []
        browser.close()


def _wait_chip(page, text):
    """Wait until the current chapter chip renders ``text``.

    ``.ochip.current`` already exists when a run is opened from inside another
    run's view, and ``selectRun`` re-renders asynchronously after two fetches,
    so a plain ``wait_for_selector`` can return the previous run's chip. Poll
    for the expected label instead.
    """
    page.wait_for_function(
        "text => (document.querySelector('.ochip.current')?.innerText ?? '').includes(text)",
        arg=text)


def test_entry_preference_chooses_where_a_run_opens(server):
    """§4.3.5 fast/deep entry: by default a run opens on the Overview summary so it
    reads in one pass; switching the workspace preference to "First key moment"
    opens the guided moment view instead, and the choice is remembered per
    browser."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run")

        # Default: opening a run lands on Overview.
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        _wait_chip(page, "Overview")
        assert "Overview" in page.query_selector(".ochip.current").inner_text()

        # Switch the preference to the fast path; the next run opens on Key moments.
        # The stored value is "first_moment" for that choice.
        page.select_option('.sort-row:has-text("Open at") select', "first_moment")
        page.click('.run[data-run-id="greeting_report__seed7"]')
        _wait_chip(page, "Key moments")
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()

        # The preference is persisted, so a fresh page in the same browser keeps it.
        assert page.evaluate("() => localStorage.getItem('agr-entry-pref')") == "first_moment"
        page.reload()
        page.wait_for_selector(".run")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        _wait_chip(page, "Key moments")
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()

        browser.close()


def test_header_run_stepper_walks_the_queue(server):
    """The header queue position is a stepper: it walks the sweep run-by-run
    without returning to the queue list. The store holds two runs, so from the
    first the previous arrow is disabled and the next arrow advances."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run-header")

        assert "of 2" in page.query_selector(".run-stepper .step-label").inner_text().lower()
        first = page.query_selector(".run-header h1").inner_text()
        arrows = page.query_selector_all(".run-stepper .step-arrow")
        assert arrows[0].is_disabled()          # previous, at the first run
        assert not arrows[1].is_disabled()      # next
        arrows[1].click()
        page.wait_for_function(
            "t => (document.querySelector('.run-header h1')?.innerText ?? '') !== t", arg=first)
        assert page.query_selector(".run-header h1").inner_text() != first

        browser.close()


def test_first_session_guidance_and_glossary(server):
    """T1/§4.19: first use opens straight into the review, with no blocking
    terminology modal; the same orientation overlay stays reachable on demand
    from Help, and a compact glossary is reachable from the app bar on every
    surface, built from the controlled vocabulary."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        # A raw page (empty storage) — the overlay must still not appear.
        page = browser.new_page()
        page.goto(server)
        page.wait_for_selector(".run")
        assert page.query_selector("#intro-modal.open") is None

        # The overlay is reachable on demand from Help, and explains review
        # mode, evidence grade, attribution, and the fact/interpretation split.
        page.click("#help-button")
        page.wait_for_selector("#help-modal.open")
        page.click("#open-intro")
        page.wait_for_selector("#intro-modal.open")
        intro = page.query_selector("#intro-modal").inner_text().lower()
        for idea in ("review mode", "evidence grade", "attribution", "interpretation"):
            assert idea in intro
        page.click("#intro-dismiss")
        page.wait_for_selector("#intro-modal.open", state="hidden")

        # The glossary is reachable from the app bar and defines terms in plain
        # language, grouped by kind — the same labels the badges carry.
        page.click("#glossary-button")
        page.wait_for_selector("#glossary-modal.open")
        gloss = page.query_selector("#glossary-body").inner_text()
        assert "REVIEW MODE" in gloss.upper() and "ATTRIBUTION" in gloss.upper()  # group headings
        assert "Deterministic review" in gloss and "Linked to outcome" in gloss  # defined terms

        # It is reachable while the Full Trace surface is open, too (§4.19).
        page.keyboard.press("Escape")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        # Trace is reachable from any chapter; a run now opens on Overview.
        page.wait_for_selector(".trace-chip")
        page.click(".trace-chip")
        page.wait_for_selector("#trace-drawer.open")
        page.keyboard.press("g")
        page.wait_for_selector("#glossary-modal.open")

        browser.close()


def _query(url):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query)


def test_tier3_correction_and_instrumentation(server):
    """§4.13: Correct → Add detail → the expert editor, validated server-side, with
    the accepted view shown and the generated one still inspectable. §4.21: the
    reviewer's path through that flow lands in the analytics log."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".trace-chip")
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")

        # Tier 2 stays one click; the confirmation offers the escalation.
        page.click('.moment-actions button:has-text("Correct label")')
        page.wait_for_selector("#correct-modal.open")
        page.click("#add-detail")
        page.wait_for_selector("#correction-modal.open")

        # The editor shows the generated value beside every field it can change.
        editor = page.query_selector("#correction-fields").inner_text()
        assert "generated:" in editor
        # the chip is uppercased by CSS, which inner_text renders
        assert "FACTUAL · NEEDS EVIDENCE" in editor.upper()

        # Correcting a factual claim with no evidence is refused by the write path.
        page.select_option('.corr-row:has-text("Consequence") select', "incorrect_state")
        page.fill("#corr-evidence", "")
        page.click("#correction-save")
        page.wait_for_selector("#correction-error:not(:empty)")
        assert "evidence_event_ids" in page.query_selector("#correction-error").inner_text()

        # With evidence cited it saves as a new annotation revision.
        page.fill("#corr-evidence", "evt_008")
        page.click("#correction-save")
        page.wait_for_selector(".corrected-strip")
        strip = page.query_selector(".corrected-strip").inner_text()
        assert "Human corrected" in strip and "consequence" in strip
        assert "Applied to this review" in strip
        assert "not yet eligible for the reviewer dataset" in strip

        # The generated version stays inspectable behind the toggle.
        page.click('.corrected-strip button:has-text("Show generated version")')
        assert page.query_selector(".generated-peek").is_visible()

        # §4.21: the path through this flow is in the analytics log, as identifiers.
        page.wait_for_timeout(1600)  # the client batches on an idle tick
        metrics = page.evaluate("() => fetch('/metrics').then(r => r.json())")
        assert metrics["counts"]["run_opened"] >= 1
        assert metrics["counts"]["correction_saved"] == 1
        assert metrics["counts"]["moment_viewed"] >= 0
        assert metrics["sessions"] == 1
        assert metrics["full_correction_rate"] is not None

        browser.close()


def test_review_position_is_shareable_in_the_url(server):
    """§4.1: run, chapter, moment, queue context, and the selected trace step are
    encoded in the URL, and a shared link reopens the review at that exact spot."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run-header")

        # The default landing (Overview) is addressable; stepping into Key
        # moments adds the selected-moment position to the URL.
        assert _query(page.url)["chapter"] == ["overview"]
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")
        q = _query(page.url)
        assert q["run"] == [CHESS] and q["chapter"] == ["moments"] and q["moment"]
        moment_id = q["moment"][0]

        # Chapter navigation and queue filters both move into the URL. Opportunities
        # now lives under "More analysis" (T1) but keeps its own chapter id.
        page.click(".more-analysis summary")
        page.click('.more-analysis-list .ochip:has-text("Opportunities")')
        page.wait_for_selector(".opp-table")
        assert _query(page.url)["chapter"] == ["opportunities"]
        assert "moment" not in _query(page.url)  # not a moment chapter
        page.click('.fchip:has-text("Failed")')
        page.wait_for_function("() => location.search.includes('filter=failed')")
        assert _query(page.url)["filter"] == ["failed"]

        # A selected trace step is the shareable evidence position.
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")
        page.click(".trace-chip")
        page.wait_for_selector("#trace-drawer.open")
        page.click('#trace-body .step[data-step-id="s7"]')
        shared = page.url
        assert _query(shared)["evidence"] == ["s7"] and _query(shared)["trace"] == ["1"]
        # Closing the drawer drops it again — the URL tracks the live position.
        page.keyboard.press("Escape")
        assert "evidence" not in _query(page.url)
        # ...and so does moving to another run — reachable from the keyboard with
        # the drawer still open, which must not leave a stale step in the URL.
        page.click('.fchip:has-text("Failed")')  # clear the filter
        page.wait_for_function("() => !location.search.includes('filter=')")
        page.click(".trace-chip")
        page.wait_for_selector("#trace-drawer.open")
        page.keyboard.press("n")  # next unhandled run
        page.wait_for_selector("#trace-drawer:not(.open)", state="attached")
        assert "trace" not in _query(page.url) and "evidence" not in _query(page.url)
        assert _query(page.url)["run"] != [CHESS]

        # The shared link restores run, chapter, moment, filter, and trace step.
        other = _page(browser)
        other.goto(shared)
        other.wait_for_selector("#trace-drawer.open")
        assert other.query_selector("#trace-body .step.active").get_attribute("data-step-id") == "s7"
        assert other.query_selector("#trace-body .pane.lit").get_attribute("data-panel") == "artifact"
        assert _query(other.url)["moment"] == [moment_id]
        assert other.query_selector(".fchip.on").inner_text() == "Failed"

        # A link to a run this store does not hold falls back to the queue rather
        # than stranding the reviewer on an empty workspace.
        stray = _page(browser)
        stray.goto(server + "/?run=not_a_run__seed1&chapter=outcome")
        stray.wait_for_selector(".run-header")
        assert _query(stray.url)["run"] == [CHESS]

        browser.close()


def test_compare_surface_names_the_sides_it_is_comparing(compare_server):
    """§4.16: the compare pair is shareable, and the gutter names the reviewer that
    surfaced a moment from the pair actually loaded — not an assumed orientation."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(compare_server)
        page.wait_for_selector(".run-header")

        # Two reviews exist, so comparing them is reachable through the unified
        # Compare control (T1/§4.16) and defaults to baseline → model.
        page.click('.compare-menu summary')
        page.click('.compare-item:has-text("Between reviewers")')
        page.wait_for_selector(".compare-list")
        q = _query(page.url)
        assert q["view"] == ["compare"] and q["left"] == ["deterministic"] and q["right"] == ["model:test"]
        assert page.query_selector(".cmp-badge.matched") is not None
        # The scripted pass adds enrichment the baseline does not have.
        assert "+ better action" in page.query_selector(".cmp-gutter").inner_text()

        # An added/removed gutter label is derived from the loaded pair's keys.
        for status, expected in (("added", "test only"), ("removed", "Deterministic baseline only")):
            side = "right" if status == "added" else "left"
            label = page.evaluate(
                "([status, side]) => renderComparePair("
                "  Object.assign({status}, {[side]: {kind: 'omission', summary: 'x'}}),"
                "  'deterministic', 'model:test').querySelector('.cmp-gutter').innerText",
                [status, side])
            assert label == expected
        # The side that did not surface a moment reads as an empty card, not "[object …]".
        empty = page.evaluate(
            "renderComparePair({status: 'added', right: {kind: 'omission', summary: 'x'}},"
            " 'deterministic', 'model:test').querySelector('.cmp-card.empty').innerText")
        assert "not surfaced" in empty

        # A shared compare link reopens the same pair, in the same orientation.
        flipped = _page(browser)
        flipped.goto(compare_server + f"/?run={CHESS}&view=compare&left=model:test&right=deterministic")
        flipped.wait_for_selector(".compare-list")
        selects = flipped.query_selector_all(".compare-select")
        assert [s.input_value() for s in selects] == ["model:test", "deterministic"]

        # A stale link naming a review this capture does not have falls back to the
        # default pair instead of asking the API for a review that is not there.
        stale = _page(browser)
        stale.goto(compare_server + f"/?run={CHESS}&view=compare&left=deterministic&right=model:gone")
        stale.wait_for_selector(".compare-list")
        assert _query(stale.url)["right"] == ["model:test"]

        browser.close()


def test_side_panels_resize_and_remember(server):
    """Both reference columns are draggable, clamped, and keyboard-reachable, and
    the widths survive a reload — a reviewer's layout is their own."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser, viewport={"width": 1600, "height": 900})
        page.goto(server)
        page.wait_for_selector(".run")

        def width(name):
            return int(page.evaluate(
                f"() => parseInt(getComputedStyle(document.documentElement)"
                f".getPropertyValue('--{name}-w'))"))

        def drag_to(x):
            """Grab the handle where it is now — it moves with every drag."""
            box = page.query_selector("#queue-resizer").bounding_box()
            page.mouse.move(box["x"] + 2, 400)
            page.mouse.down()
            page.mouse.move(x, 400, steps=8)
            page.mouse.up()

        before = width("queue")
        drag_to(before + 120)
        assert width("queue") > before

        # Clamped: a drag past the limit stops at it rather than eating the stage.
        drag_to(1500)
        assert width("queue") == 520

        # The handle is a real control: arrow keys nudge it.
        page.query_selector("#queue-resizer").press("ArrowLeft")
        assert width("queue") == 504

        # And the width is remembered across a reload.
        page.reload()
        page.wait_for_selector(".run")
        assert width("queue") == 504

        # Double-click restores the default.
        page.query_selector("#queue-resizer").dblclick()
        assert width("queue") == 296

        browser.close()


@pytest.fixture
def lesson_server(tmp_path):
    """A store whose only run carries a model-enriched, lesson-recommending moment,
    so the §4.11 Eval Lesson chapter offers the full create→approve→experiment flow."""
    root = str(tmp_path / "store")
    store = Store(root)
    _analyzed(store, "chess_best_move.atif.json")
    payload = {"moments": [{
        "candidate_id": m["candidate_id"], "anchor_event_ids": m["anchor_event_ids"],
        "kind": m["kind"], "polarity": m["polarity"], "affected_checks": m["affected_checks"],
        "structured_facts": _grounded_facts(m),
        "behaviour_tags": ["stopped_enumeration"],
        "better_action": "Check the complete candidate set before submission",
        "root_cause_candidates": [{"locus": "evaluation_harness",
                                   "rationale": "Add an unresolved-requirement submission gate"}],
        "eval_lesson_recommended": True,
    } for m in read.get_review(store, CHESS)["review_moments"] if m.get("selected")]}
    _analyzed(store, "chess_best_move.atif.json",
              reviewer=ScriptedReviewer(payload, source="model:test"))
    with _serving(root) as base:
        yield base


def test_eval_lesson_lifecycle_and_experiment(lesson_server):
    """§4.11 / §13.2 (T1: attached to its moment, not a chapter of its own): an
    accepted moment becomes an approved lesson and then a human-approved
    improvement experiment, all through the moment card's inline section."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(lesson_server)
        page.wait_for_selector(".trace-chip")
        page.click('.outline > .ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")

        # The recommending moment shows the inline "Eval lesson available"
        # disclosure; expanding it reveals the lesson body, projected from that
        # moment, status proposed.
        page.click(".moment-lesson summary")
        page.wait_for_selector(".moment-lesson-body .lesson-actions")
        assert "proposed" in page.query_selector(".lesson-status").inner_text()
        assert "submission gate" in page.query_selector(".moment-lesson-body").inner_text()

        # Approve for test → status advances and persists.
        page.click('.lesson-actions button:has-text("Approve for test")')
        page.wait_for_selector(".lesson-status:has-text('approved_for_test')")

        # Now the experiment proposal can be generated (§13.2)…
        page.click('button:has-text("Create regression-eval proposal")')
        page.wait_for_selector(".lesson-experiment")
        proposal = page.query_selector(".lesson-experiment").inner_text().lower()
        assert "proposed" in proposal
        assert "owner" in proposal  # the "thresholds set by the owner" note

        # …and a human approves it — the Milestone 5 accept criterion.
        page.click('.lesson-experiment button:has-text("Approve proposal")')
        page.wait_for_selector(".lesson-experiment:has-text('Approved by')")
        assert "approved" in page.query_selector(".lesson-experiment").inner_text().lower()

        browser.close()


@pytest.fixture
def not_reviewable_server(tmp_path):
    """A store whose only run declares an incomplete capture, so the §4.15
    not_reviewable state is what the Outcome chapter must show."""
    root = str(tmp_path / "store")
    store = Store(root)
    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["capture_completeness"] = "incomplete"
    doc["capabilities"]["tool_calls"] = "unavailable"
    doc["capabilities"]["verifier_code"] = "unavailable"
    analyze(doc, store)
    with _serving(root) as base:
        yield base


def test_not_reviewable_capture_says_why(not_reviewable_server):
    """§4.15: an incomplete capture is badged Not reviewable and names the missing
    capabilities rather than presenting a review it cannot support."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(not_reviewable_server)
        page.wait_for_selector(".run")
        page.click(".run")
        page.click('.outline > .ochip:has-text("Overview")')
        page.wait_for_selector(".review-mode-note.not-reviewable")
        note = page.query_selector(".review-mode-note.not-reviewable").inner_text()
        assert "Not reviewable" in note
        assert "tool_calls" in note and "verifier_code" in note
        browser.close()


def test_fresh_unconfirmed_import_opens_every_chapter(server):
    """A freshly imported run is watermarked until its contract is
    human-confirmed, and that is the normal entry path for a real import — so
    every review chapter must open on it without a page error.

    Regression gate: the Overview chapter once referenced an undeclared variable
    while rendering the provisional watermark, throwing ``ReferenceError`` on
    exactly this path. The synthetic demo auto-confirms its contracts, which is
    why the demo smoke check never saw it. The gate fails on any collected
    ``pageerror``, not just the chapter known to have crashed.
    """
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(server)
        page.wait_for_selector(".run")
        page.click('.run[data-run-id="greeting_report__seed7"]')

        # No confirmation was recorded for this fixture, so the shell must say
        # the review is provisional — and render at all.
        page.wait_for_selector(".watermark")
        assert "not human-confirmed" in page.query_selector(".watermark").inner_text()

        # Walk every available chapter; the Overview chapter carries the
        # provisional row in "What limits the review". Primary chapters (T1)
        # first, then the two under "More analysis".
        page.click('.outline > .ochip:has-text("Overview")')
        page.wait_for_selector('.limit-item .chip:has-text("provisional")')
        primary_labels = [c.inner_text() for c in
                           page.query_selector_all(".outline > .ochip:not(.trace-chip) .ochip-label")]
        for label in primary_labels:
            page.click(f'.outline > .ochip:has-text("{label}")')
            page.wait_for_function(
                "() => { const c = document.querySelector('.ochip.current .ochip-label');"
                " return c && c.textContent === %r; }" % label)

        page.click(".more-analysis summary")
        more_labels = [c.inner_text() for c in
                        page.query_selector_all(".more-analysis-list .ochip:not(.unavailable) .ochip-label")]
        for label in more_labels:
            # Selecting one of these chapters leaves the disclosure open on the
            # next render (it reflects the current chapter); only open it by
            # hand when it is not already showing its list.
            if not page.is_visible(".more-analysis-list"):
                page.click(".more-analysis summary")
            page.click(f'.more-analysis-list .ochip:has-text("{label}")')
            page.wait_for_function(
                "() => { const c = document.querySelector('.ochip.current .ochip-label');"
                " return c && c.textContent === %r; }" % label)

        assert errors == [], f"page errors on a fresh unconfirmed import: {errors}"
        browser.close()


def test_outcome_headlines_are_honest_for_undetermined_and_unverified(server, tmp_path):
    """F1 follow-up: a run whose checks recorded no verdict is headed
    UNDETERMINED, and a run with no checks is headed as never verified —
    neither can be headed 'All checks passed.'"""
    import copy

    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        base = json.load(fh)

    undet = copy.deepcopy(base)
    undet["run"]["logical_run_id"] = "undetermined__seed9"
    for c in undet["verifier"]["checks"]:
        c["status"] = "error"

    unverified = copy.deepcopy(base)
    unverified["run"]["logical_run_id"] = "unverified__seed10"
    unverified["verifier"]["checks"] = []

    root = str(tmp_path / "extra")
    store = Store(root)
    analyze(undet, store)
    analyze(unverified, store)
    with _serving(root) as extra:
        with sync_playwright() as pw:
            browser = _launch(pw)
            page = _page(browser)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(extra)
            page.wait_for_selector(".run")
            page.click('.run[data-run-id="undetermined__seed9"]')
            page.click('.outline > .ochip:has-text("Overview")')
            page.wait_for_selector(".outcome h2")
            headline = page.query_selector(".outcome h2").inner_text()
            assert "undetermined" in headline.lower()
            assert "passed" not in headline.lower()

            page.click('.run[data-run-id="unverified__seed10"]')
            page.wait_for_function("() => { const h = document.querySelector('.outcome h2');"
                                   " return h && /no verifier checks/i.test(h.textContent); }")
            headline = page.query_selector(".outcome h2").inner_text()
            assert "no verifier checks" in headline.lower()
            assert "passed" not in headline.lower()

            assert errors == []
            browser.close()


def test_missing_outcome_never_defaults_to_passed(tmp_path):
    """Review of PR #64: outcomeNarrative (ui-utils.js) must not fall through
    to the PASSED narrative when rv.outcome is missing or carries a status it
    does not recognize — that would silently re-introduce "reads as pass when
    not proven" for exactly the class of bug F3 exists to prevent. Simulates
    a corrupted/pre-migration outcome.json directly (real checks still exist,
    so this is not the already-covered UNVERIFIED/no-checks case)."""
    root = str(tmp_path / "store")
    store = Store(root)
    _analyzed(store, "chess_best_move.atif.json")
    capture_id = store.latest_capture_id(CHESS)
    store.write_derived(CHESS, capture_id, "outcome.json", {})  # no "status" key at all

    with _serving(root) as extra:
        with sync_playwright() as pw:
            browser = _launch(pw)
            page = _page(browser)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(extra)
            page.wait_for_selector(".run")
            page.click(f'.run[data-run-id="{CHESS}"]')
            page.wait_for_selector(".run-verdict")

            verdict = page.query_selector(".run-verdict").inner_text()
            assert "passed" not in verdict.lower()

            page.click('.outline > .ochip:has-text("Overview")')
            page.wait_for_selector(".outcome h2")
            headline = page.query_selector(".outcome h2").inner_text()
            assert "passed" not in headline.lower()

            assert errors == []
            browser.close()


def test_superseded_check_never_makes_a_reconciled_pass_read_as_failed(tmp_path):
    """F3: header, Overview, run list, and Checks must all read the backend's
    reconciled outcome, not re-derive their own from raw check.status. C3 here
    originally failed, then a later same-scope check (C3b) passed — reconcile_
    checks() marks C3 superseded_by C3b, so it is excluded from rv.outcome
    (which is PASSED). Before the fix, the header verdict and Overview
    headline independently filtered on raw c.status === "failed" and so
    counted the superseded C3 anyway, showing "Failed —" while the header
    pill (which already read rv.outcome.status) said PASSED on the same
    screen — a direct contradiction this test pins shut."""
    import copy

    with open(os.path.join(FIXTURES, "chess_best_move.atif.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["run"]["logical_run_id"] = "superseded_pass__seed11"
    checks = doc["verifier"]["checks"]
    c3 = next(c for c in checks if c["check_id"] == "C3")
    c3["scope"] = "winning_moves"
    c3["sequence"] = 1
    c3b = copy.deepcopy(c3)
    c3b["check_id"] = "C3b"
    c3b["status"] = "passed"
    c3b["expected"] = ["g2e4", "e2e4"]
    c3b["observed"] = ["g2e4", "e2e4"]
    c3b["sequence"] = 2
    checks.append(c3b)

    root = str(tmp_path / "store")
    store = Store(root)
    analyze(doc, store)
    rv = read.get_review(store, "superseded_pass__seed11")
    assert rv["outcome"]["status"] == "PASSED"  # backend already reconciles correctly
    by_id = {c["check_id"]: c for c in rv["checks"]}
    assert by_id["C3"]["superseded_by"] == "C3b"
    assert by_id["C3"]["status"] == "failed"          # historical status preserved
    assert by_id["C3"]["effective_status"] is None    # excluded from the rollup

    with _serving(root) as extra:
        with sync_playwright() as pw:
            browser = _launch(pw)
            page = _page(browser)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(extra)
            page.wait_for_selector(".run")

            # Run list: already reads rv.outcome.status server-side.
            row = page.query_selector('.run[data-run-id="superseded_pass__seed11"] .run-status')
            assert "PASSED" in row.inner_text()

            page.click('.run[data-run-id="superseded_pass__seed11"]')
            page.wait_for_selector(".run-verdict")

            # Header pill and header verdict line must agree.
            pill = page.query_selector(".header-status").inner_text()
            assert "PASSED" in pill.upper()
            verdict = page.query_selector(".run-verdict").inner_text()
            assert verdict.lower().startswith("passed")
            assert "failed" not in verdict.lower()

            # Overview headline must agree with the header, not re-derive its own.
            page.click('.outline > .ochip:has-text("Overview")')
            page.wait_for_selector(".outcome h2")
            headline = page.query_selector(".outcome h2").inner_text()
            assert headline.lower().startswith("passed")
            assert "failed" not in headline.lower()

            # Checks tab: the superseded check is visibly tagged, not shown as
            # an indistinguishable live FAILED row. ("C3" is an exact match —
            # match_ids() below excludes "C3b", a substring collision.)
            page.click('.outline > .ochip:has-text("Checks")')
            page.wait_for_selector(".check-row")
            rows = page.query_selector_all(".check-row")
            c3_row = next(r for r in rows
                         if r.query_selector(".check-id").inner_text() == "C3")
            c3b_row = next(r for r in rows
                          if r.query_selector(".check-id").inner_text() == "C3b")
            assert "SUPERSEDED" in c3_row.inner_text()
            assert "SUPERSEDED" not in c3b_row.inner_text()
            assert "PASSED" in c3b_row.inner_text()

            assert errors == []
            browser.close()
