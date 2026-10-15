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
from agr.imports import ImportProblem
from agr.userconfig import save_config
from test_langfuse_browse import FakeLangfuse
from test_ui import _serving, _launch, _page


@pytest.fixture(params=[(6, 3)], ids=["six"])
def fake_langfuse(request):
    fake = FakeLangfuse(count=request.param[0], page=request.param[1], recent=True, version=request.param[2] if len(request.param) > 2 else "4.0.0")
    keys = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            keys.append(self.headers.get("Authorization"))
            # The health endpoint needs no credentials; everything else does.
            if not self.path.startswith("/api/public/health") and self.headers.get("Authorization") != "Basic " + __import__("base64").b64encode(b"pk-ui:sk-ui").decode():
                self.send_response(401)
                self.end_headers()
                return
            try:
                body = json.dumps(fake.get("http://langfuse.test" + self.path, self.headers["Authorization"])).encode()
            except ImportProblem as exc:
                # A server answers 404 for the API version it does not have.
                self.send_response(404 if getattr(exc, "endpoint_missing", False) else 502)
                self.end_headers()
                return
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


@pytest.mark.parametrize("fake_langfuse", [(6, 3, "4.0.0"), (6, 3, "3.12.0")], indirect=True, ids=["langfuse4", "langfuse3"])
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
        # The server's version decides the API, and the connection says which one is in use.
        label = "v1 trace API" if fake.version.startswith("3") else "v2 observations API"
        expect(page.locator(".lf-facts")).to_contain_text("Production agent")
        expect(page.locator(".lf-facts")).to_contain_text("proj-prod-1")
        expect(page.locator(".lf-facts dd").filter(has_text=f"{fake.version} · {label}")).to_have_count(1)

        # Narrow by tag, then widen: the newest traces come first and are all checked.
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


def connect_langfuse(page, base, host, project_name="Traces"):
    page.goto(base + "/?view=welcome")
    page.get_by_label("Project name", exact=True).fill(project_name)
    page.get_by_role("button", name="Create project", exact=True).click()
    page.get_by_role("heading", name="Add runs to " + project_name).wait_for()
    page.get_by_role("button", name=re.compile(r"^Langfuse")).click()
    page.get_by_label("Langfuse host (for example, https://cloud.langfuse.com)").fill(host)
    page.get_by_label(re.compile(r"^Public key")).fill("pk-ui")
    page.get_by_label(re.compile(r"^Secret key")).fill("sk-ui")
    page.get_by_role("button", name="Connect and test access", exact=True).click()
    page.get_by_role("heading", name="2. Find traces").wait_for()


@pytest.mark.parametrize("fake_langfuse", [(6, 3, "4.0.0"), (6, 3, "3.12.0")], indirect=True, ids=["langfuse4", "langfuse3"])
def test_traces_are_presented_the_way_langfuse_shows_them(app, fake_langfuse):
    base, _ = app
    fake, host = fake_langfuse
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        connect_langfuse(page, base, host)
        # The project, as Langfuse names it, is part of the picture.
        expect(page.get_by_label(re.compile(r"^Connection")).locator("option:checked")).to_contain_text("Production agent")

        # Look a trace up by (part of) its ID.
        page.get_by_label("Trace ID", exact=False).first.fill("trace-03")
        page.get_by_role("button", name="Find traces", exact=True).click()
        page.locator(".lf-table .lf-row").first.wait_for()
        assert page.locator(".lf-table .lf-row").count() == 1
        row = page.locator(".lf-table .lf-row").first
        for text in ("trace-03", "config-fixer", "ben", "s-trace-03", "default", "prod", "4.50 s"):
            expect(row).to_contain_text(text)
        assert re.search(r"\$0\.\d+", row.inner_text())
        expect(page.get_by_text(re.compile(r"^1 traces in Production agent"))).to_be_visible()
        head = [h.strip().lower() for h in page.locator(".lf-table thead th").all_inner_texts() if h.strip()]
        assert head == ["timestamp (utc)", "name", "trace id", "user", "session", "environment", "tags", "latency", "cost"]

        # Everything, then narrow without asking Langfuse again.
        page.get_by_role("button", name="Reset filters", exact=True).click()
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.locator(".lf-table .lf-row")).to_have_count(6)
        page.get_by_label("Filter the listed traces").fill("customer acme")
        expect(page.locator(".lf-table .lf-row")).to_have_count(3)
        expect(page.get_by_text("3 of 6 shown")).to_be_visible()
        page.locator(".lf-facet button[data-facet=tag][data-value=staging]").click()
        expect(page.get_by_text("No listed trace matches these filters.")).to_be_visible()
        page.get_by_label("Filter the listed traces").fill("")
        expect(page.locator(".lf-table .lf-row")).to_have_count(3)
        page.locator(".lf-facet button[data-facet=tag][data-value=staging]").click()
        expect(page.locator(".lf-table .lf-row")).to_have_count(6)
        calls = len(fake.calls)

        # Details show what Langfuse's detail pane does: input, output, release, version, metadata.
        page.get_by_role("button", name="Details for trace trace-04").click()
        detail = page.locator(".lf-detail")
        expect(detail).to_contain_text('{"task": "fix the config trace-04"}')
        expect(detail).to_contain_text("done trace-04")
        expect(detail).to_contain_text("rel-1")
        expect(detail.locator(".lf-meta-pair")).to_have_count(2)
        expect(detail).to_contain_text("customer globex")
        assert len(fake.calls) == calls  # all of the above was client-side

        # Metadata is a server-side filter too, and the form's values are suggestions from what was listed.
        page.get_by_label("Metadata key").fill("customer")
        page.get_by_label("Metadata value (exact)").fill("acme")
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.locator(".lf-table .lf-row")).to_have_count(3)
        assert page.locator("datalist#lf-ids option").count() == 3
        assert not errors
        browser.close()


def test_a_trace_the_filter_hides_is_not_previewed(app, fake_langfuse):
    base, _ = app
    fake, host = fake_langfuse
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        connect_langfuse(page, base, host)
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.locator(".lf-table .lf-row")).to_have_count(6)
        expect(page.get_by_role("button", name="Preview 6 selected traces", exact=True)).to_be_enabled()
        # Narrowing the list narrows what Preview acts on, and says what was left out.
        page.get_by_label("Filter the listed traces").fill("customer acme")
        expect(page.locator(".lf-table .lf-row")).to_have_count(3)
        expect(page.get_by_role("button", name="Preview 3 selected traces", exact=True)).to_be_enabled()
        expect(page.get_by_text("3 selected traces are hidden by the filter above and will not be previewed.")).to_be_visible()
        with page.expect_request(lambda r: r.url.endswith("/trace-previews")) as sent:
            page.get_by_role("button", name="Preview 3 selected traces", exact=True).click()
        asked = json.loads(sent.value.post_data)["trace_ids"]
        assert len(asked) == 3 and all(t in {"trace-01", "trace-03", "trace-05"} for t in asked), asked
        browser.close()


@pytest.mark.parametrize("metrics", [False, True], ids=["root-cost", "trace-cost"])
def test_the_cost_column_says_when_it_is_only_the_root_observation(app, fake_langfuse, metrics):
    base, _ = app
    fake, host = fake_langfuse
    fake.metrics = metrics
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        connect_langfuse(page, base, host)
        page.get_by_role("button", name="Find traces", exact=True).click()
        expect(page.locator(".lf-table .lf-row")).to_have_count(6)
        cost_head = page.locator(".lf-table thead th", has_text="Cost")
        if metrics:
            expect(page.locator(".lf-table .lf-row").first).to_contain_text("$0.")
            assert not cost_head.get_attribute("title")
            # Whole-trace totals from the metrics API, not the root rows' own cost.
            assert re.search(r"\$0\.(26|27|28|29|30|31)", page.locator(".lf-table tbody").inner_text())
        else:
            assert "root observation" in cost_head.get_attribute("title")
        assert "root observation" in page.locator(".lf-table thead th", has_text="Latency").get_attribute("title")
        browser.close()
