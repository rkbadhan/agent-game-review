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
    """A fresh page with the §4.19 first-session overlay pre-dismissed.

    Every ``browser.new_page()`` is an isolated context with empty storage, so the
    onboarding overlay would otherwise open on every test and block the workspace.
    Tests that are not about onboarding start straight in the app; the dedicated
    ``test_first_session_guidance_and_glossary`` uses a raw page to see it appear.
    """
    page = browser.new_page(**kwargs)
    page.add_init_script("window.localStorage.setItem('agr-seen-intro','1')")
    return page


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


def test_version_comparison_surface(versions_server):
    """§4.16: construct a matched slice, read the result, share it, and trace a row
    back to the runs behind it."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(versions_server)
        page.wait_for_selector(".moment-card")

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
        other.wait_for_selector(".moment-card, .chapter-empty")
        assert _query(other.url)["run"]

        browser.close()


def test_workspace_and_full_trace(server):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)

        page.wait_for_selector(".run")
        assert len(page.query_selector_all(".run")) == 2

        page.click('.run[data-run-id="chess_best_move__seed42"]')

        # Guided workspace: unified timeline (§4.8) with 3 lanes + moment markers,
        # and the five-block moment card (§4.7.1).
        page.wait_for_selector(".timeline-wrap")
        assert len(page.query_selector_all(".lane-row")) == 3  # phase / progress / moments
        assert page.query_selector(".moment-mark") is not None
        page.wait_for_selector(".moment-card")
        card = page.query_selector(".moment-card").inner_text().lower()
        for block in ("situation", "observed consequence", "better action"):
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
        assert "situation & action" in panel.lower()

        # The review outline (§4.2/§4.4) lists all seven chapters, with Key moments
        # the default entry and marked current.
        assert len(page.query_selector_all(".outline .ochip")) == 7
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()

        # Open the full-trace slide-over (§4.12): synchronized forensic panels.
        page.click('.review-util button:has-text("Full trace")')
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

        # Outcome chapter (§4.5): a disabled detector is listed under review limits.
        page.click('.ochip:has-text("Outcome")')
        page.wait_for_selector(".check-list")
        assert any("not evaluated" in c.inner_text()
                   for c in page.query_selector_all(".limit-item .chip"))

        # ...and the final environment/artifact summary (§4.5) states the closing
        # observed state plus the capability levels that bound it.
        final = page.query_selector(".final-table").inner_text()
        assert "/solution.txt" in final and "Observed" in final and "g2e4" in final
        block = page.query_selector(".final-block").inner_text()
        assert "shell exited 0" in block                       # last process exit
        assert "Artifact capture: checkpoint_only" in block    # what bounds it

        # Task Opportunities chapter (§4.6): the verify-before-submission window is
        # measured, rendered as a distinct status badge rather than a blank cell.
        page.click('.ochip:has-text("Opportunities")')
        page.wait_for_selector(".opp-table")
        assert page.query_selector(".opp-table .stat-badge.measured") is not None

        # Ability Signature chapter (§4.10): a no-opportunity row stays distinct
        # from the measured rows.
        page.click('.ochip:has-text("Ability signature")')
        page.wait_for_selector(".sig-table")
        assert "No opportunity occurred" in page.query_selector(".sig-table").inner_text()

        # Eval Lesson chapter (§4.11): a deterministic review shows the canonical
        # empty state, never generated filler.
        page.click('.ochip:has-text("Eval lesson")')
        page.wait_for_selector(".chapter-empty")
        assert "No Eval Lesson generated" in page.query_selector(".chapter-empty").inner_text()

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

        # Open a run; the five-block card and its actions render.
        page.click('.run[data-run-id="chess_best_move__seed42"]')
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


def test_entry_preference_chooses_where_a_run_opens(server):
    """§4.3.5 fast/deep entry: the default fast path opens a run on the first key
    moment; switching the workspace preference to Outcome opens the summary instead,
    and the choice is remembered per browser."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(server)
        page.wait_for_selector(".run")

        # Default fast path: opening a run lands on Key moments.
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.wait_for_selector(".ochip.current")
        assert "Key moments" in page.query_selector(".ochip.current").inner_text()

        # Switch the preference to Outcome; the next run opens on the Outcome chapter.
        page.select_option('.sort-row:has-text("Open at") select', "outcome")
        page.click('.run[data-run-id="greeting_report__seed7"]')
        page.wait_for_selector(".ochip.current")
        assert "Outcome" in page.query_selector(".ochip.current").inner_text()

        # The preference is persisted, so a fresh page in the same browser keeps it.
        assert page.evaluate("() => localStorage.getItem('agr-entry-pref')") == "outcome"
        page.reload()
        page.wait_for_selector(".run")
        page.click('.run[data-run-id="chess_best_move__seed42"]')
        page.wait_for_selector(".ochip.current")
        assert "Outcome" in page.query_selector(".ochip.current").inner_text()

        browser.close()


def test_first_session_guidance_and_glossary(server):
    """§4.19: the first session shows a dismissible overlay explaining the four core
    ideas; it is shown once per browser; and a compact glossary is reachable from
    the app bar on every surface and is built from the controlled vocabulary."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        # A raw page (no seeded flag) so the first-session overlay actually appears.
        page = browser.new_page()
        page.goto(server)

        # The overlay explains review mode, evidence grade, attribution, and the
        # fact/interpretation split — the four ideas the spec names.
        page.wait_for_selector("#intro-modal.open")
        intro = page.query_selector("#intro-modal").inner_text().lower()
        for idea in ("review mode", "evidence grade", "attribution", "interpretation"):
            assert idea in intro

        # Dismissing it records the choice, so it does not return on reload.
        page.click("#intro-dismiss")
        page.wait_for_selector("#intro-modal.open", state="hidden")
        assert page.evaluate("() => localStorage.getItem('agr-seen-intro')") == "1"
        page.reload()
        page.wait_for_selector(".run")
        assert page.query_selector("#intro-modal.open") is None

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
        page.wait_for_selector(".moment-card")
        page.click('.review-util button:has-text("Full trace")')
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
        page.wait_for_selector(".moment-card")

        # The default landing position is already addressable.
        q = _query(page.url)
        assert q["run"] == [CHESS] and q["chapter"] == ["moments"] and q["moment"]
        moment_id = q["moment"][0]

        # Chapter navigation and queue filters both move into the URL.
        page.click('.ochip:has-text("Opportunities")')
        page.wait_for_selector(".opp-table")
        assert _query(page.url)["chapter"] == ["opportunities"]
        assert "moment" not in _query(page.url)  # not a moment chapter
        page.click('.fchip:has-text("Failed")')
        page.wait_for_function("() => location.search.includes('filter=failed')")
        assert _query(page.url)["filter"] == ["failed"]

        # A selected trace step is the shareable evidence position.
        page.click('.ochip:has-text("Key moments")')
        page.wait_for_selector(".moment-card")
        page.click('.review-util button:has-text("Full trace")')
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
        page.click('.review-util button:has-text("Full trace")')
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
        stray.wait_for_selector(".moment-card")
        assert _query(stray.url)["run"] == [CHESS]

        browser.close()


def test_compare_surface_names_the_sides_it_is_comparing(compare_server):
    """§4.16: the compare pair is shareable, and the gutter names the reviewer that
    surfaced a moment from the pair actually loaded — not an assumed orientation."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(compare_server)
        page.wait_for_selector(".moment-card")

        # Two reviews exist, so Compare is enabled and defaults to baseline → model.
        page.click('.review-util button:has-text("Compare")')
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
    """§4.11 / §13.2: an accepted moment becomes an approved lesson and then a
    human-approved improvement experiment, all through the chapter's buttons."""
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(lesson_server)
        page.wait_for_selector(".moment-card")

        page.click('.ochip:has-text("Eval lesson")')
        page.wait_for_selector(".lesson-actions")
        # The body is projected from the recommending moment, status proposed.
        assert "proposed" in page.query_selector(".lesson-status").inner_text()
        assert "submission gate" in page.query_selector(".card-pad").inner_text()

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
        page.click('.ochip:has-text("Outcome")')
        page.wait_for_selector(".review-mode-note.not-reviewable")
        note = page.query_selector(".review-mode-note.not-reviewable").inner_text()
        assert "Not reviewable" in note
        assert "tool_calls" in note and "verifier_code" in note
        browser.close()
