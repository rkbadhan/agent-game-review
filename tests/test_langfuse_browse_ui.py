"""An engineer with many Langfuse traces onboards through the browser alone.

A local HTTP server stands in for Langfuse and an offline provider stands in for
the AI reviewer, so nothing leaves the machine.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import threading

import pytest
pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright, expect

from agr import model_reviewer as models
from agr.userconfig import save_config
from test_langfuse_browse import FakeLangfuse
from test_ui import _serving, _launch, _page


@pytest.fixture(params=[(6, 3)], ids=["six"])
def fake_langfuse(request):
    fake = FakeLangfuse(count=request.param[0], page=request.param[1], recent=True)
    keys = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            keys.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Basic " + __import__("base64").b64encode(b"pk-ui:sk-ui").decode():
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps(fake.get("http://langfuse.test" + self.path, self.headers["Authorization"])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake, "http://127.0.0.1:%d" % server.server_port
    server.shutdown()
    server.server_close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGR_CONFIG", str(tmp_path / "ai-config.json"))
    for key in ("AGR_REVIEW_PROVIDER", "AGR_REVIEW_MODEL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "AGR_REVIEW_COST_BUDGET_USD", "AGR_REVIEW_TIME_BUDGET_S",
                "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ui-environment-key")
    control = {"calls": []}

    class Offline(models._LazyModelReviewer):
        provider = "openai"

        def _complete(self, system, user_json):
            control["calls"].append(json.loads(user_json))
            return {"moments": []}

    monkeypatch.setattr(models, "make_reviewer", lambda provider, model, base_url=None, **kwargs: Offline(model, base_url, **kwargs))
    save_config("openai", "ui-model", "https://offline.invalid/v1", cost_budget_usd=0, time_budget_s=0)
    with _serving(str(tmp_path / "store")) as base:
        yield base, control


def test_browse_select_import_and_review_with_ai(app, fake_langfuse):
    base, control = app
    fake, host = fake_langfuse
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/?view=welcome")
        page.get_by_label("Project name", exact=True).fill("Production traces")
        page.get_by_role("button", name="Create project", exact=True).click()
        page.get_by_role("heading", name="Add runs to Production traces").wait_for()
        page.get_by_role("button", name=re.compile(r"^Langfuse")).click()

        # Wrong keys are reported and the form keeps what was typed.
        page.get_by_label("Langfuse host (for example, https://cloud.langfuse.com)").fill(host)
        page.get_by_label(re.compile(r"^Public key")).fill("pk-ui")
        page.get_by_label(re.compile(r"^Secret key")).fill("wrong")
        page.get_by_role("button", name="Connect and test access", exact=True).click()
        expect(page.get_by_text(re.compile(r"Authentication failed"))).to_be_visible()
        expect(page.get_by_label(re.compile(r"^Public key"))).to_have_value("pk-ui")
        page.get_by_label(re.compile(r"^Secret key")).fill("sk-ui")
        page.get_by_role("button", name="Connect and test access", exact=True).click()
        page.get_by_role("heading", name="2. Find traces").wait_for()
        assert page.evaluate("state.workspace.connections.length") == 1

        # Narrow by tag, then widen: the newest traces come first and are all checked.
        page.get_by_text("Narrow by name, tag, user, session or environment").click()
        page.get_by_label("Tag", exact=True).fill("prod")
        page.get_by_role("button", name="Find traces", exact=True).click()
        page.get_by_role("heading", name="3. Choose traces to preview").wait_for()
        expect(page.get_by_text(re.compile(r"^3 traces"))).to_be_visible()
        page.get_by_label("Tag", exact=True).fill("")
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.get_by_text(re.compile(r"^6 traces"))).to_be_visible()
        boxes = page.get_by_role("checkbox", name=re.compile(r"^Select trace"))
        assert boxes.count() == 6 and all(boxes.nth(i).is_checked() for i in range(6))
        boxes.nth(0).uncheck()
        page.get_by_role("button", name="Preview 5 selected traces", exact=True).click()

        page.get_by_role("heading", name="Preview · 5 logical runs").wait_for()
        page.get_by_role("button", name="Import selected runs", exact=True).click()
        expect(page.locator(".engineer-status").filter(has_text=re.compile(r"^completed · 5/5"))).to_be_visible(timeout=20000)
        assert page.evaluate("state.workspace.job.results.filter(r => r.run_id).length") == 5

        # The same traces are flagged as imported next time, so a repeat import is incremental.
        page.get_by_role("button", name="Import more", exact=True).click()
        page.get_by_role("button", name=re.compile(r"^Langfuse")).click()
        # The list is refreshed from the import itself, without searching again.
        expect(page.get_by_text("already imported", exact=True)).to_have_count(5)
        expect(page.get_by_role("button", name="Preview 0 selected traces", exact=True)).to_be_disabled()
        page.get_by_role("button", name="Select first 100 new", exact=True).click()
        expect(page.get_by_role("button", name="Preview 1 selected trace", exact=True)).to_be_enabled()

        # Review the imported batch with AI straight from the import.
        page.go_back()
        page.get_by_role("button", name="Import more", exact=True).wait_for()
        page.get_by_role("button", name=re.compile(r"^Review 5 imported runs with AI")).click()
        expect(page.get_by_text("5 selected", exact=True)).to_be_visible()
        assert not control["calls"]
        page.get_by_role("button", name="Preview AI review", exact=True).click()
        page.get_by_role("button", name="Start AI review · uses tokens", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="completed · 5/5")).to_be_visible(timeout=30000)
        assert len(control["calls"]) == 5
        assert not errors
        browser.close()


@pytest.mark.parametrize("fake_langfuse", [(130, 500)], indirect=True, ids=["many"])
def test_preview_is_limited_to_one_hundred_traces(app, fake_langfuse):
    base, _ = app
    _, host = fake_langfuse
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=welcome")
        page.get_by_label("Project name", exact=True).fill("Many traces")
        page.get_by_role("button", name="Create project", exact=True).click()
        page.get_by_role("heading", name="Add runs to Many traces").wait_for()
        page.get_by_role("button", name=re.compile(r"^Langfuse")).click()
        page.get_by_label("Langfuse host (for example, https://cloud.langfuse.com)").fill(host)
        page.get_by_label(re.compile(r"^Public key")).fill("pk-ui")
        page.get_by_label(re.compile(r"^Secret key")).fill("sk-ui")
        page.get_by_role("button", name="Connect and test access", exact=True).click()
        page.get_by_role("heading", name="2. Find traces").wait_for()
        page.get_by_label(re.compile(r"^Maximum traces to list")).select_option("250")
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.get_by_text(re.compile(r"^130 traces"))).to_be_visible()
        expect(page.get_by_role("button", name="Preview 100 selected traces", exact=True)).to_be_enabled()
        box = page.get_by_role("checkbox", name=re.compile(r"^Select trace")).nth(100)
        box.check()
        # Toggling updates in place: the checkbox keeps focus, which a full re-render would drop.
        expect(box).to_be_focused()
        expect(page.get_by_role("button", name="Preview 101 selected traces", exact=True)).to_be_disabled()
        expect(page.get_by_text("Preview up to 100 traces at a time. Uncheck 1 to continue.", exact=True)).to_be_visible()
        browser.close()


def test_imported_marks_match_the_exact_run_id(app):
    base, _ = app
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(base + "/?view=welcome")
        page.get_by_label("Project name", exact=True).wait_for()
        marked = page.evaluate("""() => {
          state.workspace.lf = { result: { traces: [{ trace_id: "c", already_imported: false }, { trace_id: "b__c", already_imported: false }] }, picked: new Set(["c", "b__c"]) };
          markLangfuseImported({ results: [{ run_id: "langfuse__langfuse-trace-b__c__b__c" }] });
          return state.workspace.lf.result.traces.map(t => t.already_imported);
        }""")
        assert marked == [False, True]
        browser.close()
