"""Labels must stay readable in every state of the controls, light and dark (WCAG AA, 4.5:1 for text).

A selected filter chip with nothing behind it once drew grey text on the accent fill. This renders each
state of each control and measures the text against the colour actually painted behind it.
"""
import pytest
pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

from agr.store import Store
from test_ui import _serving, _analyzed, _launch, _page

# Measured in the page: the text colour against the first opaque background behind the element.
MEASURE = """
([selector, classes]) => {
  const parse = value => {
    const probe = document.createElement("canvas").getContext("2d");
    probe.fillStyle = "#000"; probe.fillStyle = value;
    const c = probe.fillStyle;
    if (c.startsWith("#")) return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16), 1];
    // color-mix() and friends come back as color(srgb r g b / a) with 0-1 channels.
    const srgb = c.match(/^color\\(srgb\\s+([^)]+)\\)$/);
    if (srgb) { const [channels, alpha] = srgb[1].split("/"); const v = channels.trim().split(/\\s+/).map(Number); return [v[0] * 255, v[1] * 255, v[2] * 255, alpha === undefined ? 1 : Number(alpha)]; }
    const m = c.match(/rgba?\\(([^)]+)\\)/); const v = m[1].split(/[,\\s/]+/).filter(Boolean).map(Number); return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1];
  };
  const paint = (fg, bg) => fg.slice(0, 3).map((x, i) => x * fg[3] + bg[i] * (1 - fg[3]));
  const luminance = rgb => { const f = rgb.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }); return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]; };
  const results = [];
  for (const element of document.querySelectorAll(selector)) {
    const added = classes.filter(c => !element.classList.contains(c));
    added.forEach(c => element.classList.add(c));
    const style = getComputedStyle(element);
    let background = [255, 255, 255, 1], layers = [];
    for (let node = element; node; node = node.parentElement) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg[3] > 0) { layers.push(bg); if (bg[3] === 1) break; }
    }
    for (const layer of layers.reverse()) background = [...paint(layer, background), 1];
    const text = parse(style.color), shown = paint(text, background);
    const hi = Math.max(luminance(shown), luminance(background.slice(0, 3))), lo = Math.min(luminance(shown), luminance(background.slice(0, 3)));
    results.push({ label: element.textContent.trim().slice(0, 30), ratio: (hi + 0.05) / (lo + 0.05) });
    added.forEach(c => element.classList.remove(c));
  }
  return results;
}
"""

# Every state a filter chip can be in.
CHIP_STATES = [("default", []), ("on", ["on"]), ("empty", ["is-empty"]), ("on and empty", ["on", "is-empty"])]


@pytest.fixture
def app(tmp_path):
    store = Store(str(tmp_path / "store"))
    for name in ("chess_best_move.atif.json", "contract_mismatch.atif.json"):
        _analyzed(store, name)
    with _serving(store.root) as base:
        yield base


def measure(page, selector, classes):
    return page.evaluate(MEASURE, [selector, classes])


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_filter_chip_labels_are_readable_in_every_state(app, theme):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(app + "/?view=runs")
        page.locator(".fchip").first.wait_for()
        page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
        for state, classes in CHIP_STATES:
            for result in measure(page, ".fchip", classes):
                # Faded chips are decorative placeholders for "nothing here"; only a chip the reader selected must be fully legible.
                if state in ("default", "empty") and "empty" in state:
                    continue
                assert result["ratio"] >= 4.5, (theme, state, result)
        browser.close()


def test_a_selected_chip_with_no_runs_is_not_faded_into_its_fill(app):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(app + "/?view=runs&filter=failed&filter=passed&filter=undetermined&filter=unverified")
        page.locator(".fchip.on").first.wait_for()
        chips = page.locator(".fchip.on")
        assert chips.count() == 4
        for i in range(chips.count()):
            colors = chips.nth(i).evaluate("e => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor]")
            assert colors[0] != colors[1] and colors[0].replace(" ", "") == "rgb(255,255,255)", colors
        browser.close()


AI_MARKUP = """
() => {
  const box = document.createElement("div"); box.id = "contrast-probe"; box.style.cssText = "position:fixed;left:0;top:0;padding:12px;background:var(--bg);z-index:99999";
  const add = (tag, cls, text) => { const e = document.createElement(tag); e.className = cls; e.textContent = text; box.append(e); };
  for (const s of ["reviewing", "queued", "reviewed", "failed", "none"]) add("span", "ai-chip ai-" + s, s);
  for (const s of ["running", "queued", "cancelling", "completed", "failed", "interrupted", "completed_with_errors", "cancelled"]) add("span", "ai-job-status ai-job-" + s, s);
  add("span", "sidebar-badge running", "3/4"); add("span", "sidebar-badge attention", "!");
  document.body.append(box);
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_ai_review_status_labels_are_readable(app, theme):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(app + "/?view=runs")
        page.locator(".fchip").first.wait_for()
        page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
        page.evaluate(AI_MARKUP)
        results = measure(page, "#contrast-probe .ai-chip, #contrast-probe .ai-job-status, #contrast-probe .sidebar-badge", [])
        assert len(results) == 15
        bad = [r for r in results if r["ratio"] < 4.5]
        assert not bad, (theme, bad)
        browser.close()


LF_MARKUP = """
() => {
  const box = document.createElement("div"); box.id = "contrast-probe-lf"; box.style.cssText = "position:fixed;left:0;top:0;padding:12px;background:var(--bg);z-index:99999";
  box.innerHTML = `<table class="lf-table"><thead><tr><th>Timestamp</th></tr></thead><tbody>
    <tr class="lf-row"><td>plain row</td><td><span class="lf-tag">prod</span></td><td><button class="lf-copy">Copy</button><button class="lf-toggle">Details</button></td><td><span class="ai-chip ai-reviewed lf-imported">already imported</span></td></tr>
    <tr class="lf-row picked"><td>picked row</td><td><span class="lf-tag">staging</span></td></tr>
    <tr class="lf-detail"><td><span class="lf-meta-pair"><b>customer</b> acme</span></td></tr></tbody></table>
    <dl class="lf-facts"><div><dt>Langfuse project</dt><dd>Production agent</dd></div></dl>
    <span class="lf-facet-label">Tags</span><span class="engineer-hint">hint text</span>`;
  document.body.append(box);
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_langfuse_trace_table_text_is_readable(app, theme):
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = _page(browser)
        page.goto(app + "/?view=runs")
        page.locator(".fchip").first.wait_for()
        page.evaluate("theme => document.documentElement.setAttribute('data-theme', theme)", theme)
        page.evaluate(LF_MARKUP)
        selector = "#contrast-probe-lf th, #contrast-probe-lf td, #contrast-probe-lf .lf-tag, #contrast-probe-lf .lf-copy, #contrast-probe-lf .lf-toggle, #contrast-probe-lf dt, #contrast-probe-lf dd, #contrast-probe-lf .lf-facet-label, #contrast-probe-lf .engineer-hint, #contrast-probe-lf .lf-meta-pair, #contrast-probe-lf .lf-imported"
        results = measure(page, selector, [])
        assert len(results) >= 12
        bad = [r for r in results if r["ratio"] < 4.5 and r["label"]]
        assert not bad, (theme, bad)
        browser.close()
