// Optional browser gate: PLAYWRIGHT_MODULE can point at a bundled installation.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const path = require("path");
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  const base = process.env.AGR_CHECK_URL || "http://127.0.0.1:8137";
  try {
    await page.goto(base + "?project=existing&view=welcome");
    await page.getByRole("heading", { name: "Understand what your agent did" }).waitFor();
    await page.getByLabel("Project name", { exact: true }).fill("Browser test project");
    await page.getByRole("button", { name: "Create project", exact: true }).click();
    await page.getByRole("heading", { name: "Add runs to Browser test project" }).waitFor();
    const project = new URL(page.url()).searchParams.get("project");
    assert(project);
    await page.getByRole("button", { name: /^Custom ATIF/ }).click();
    await page.locator('input[type="file"]').first().setInputFiles(path.resolve(__dirname, "../agr/demo_fixtures/clean_pass.atif.json"));
    await page.getByRole("button", { name: "Preview selected runs", exact: true }).click();
    await page.getByRole("heading", { name: "Preview · 1 logical runs" }).waitFor();
    await page.getByRole("button", { name: "Import selected runs", exact: true }).click();
    await page.locator(".engineer-status").filter({ hasText: /^completed ·/ }).waitFor();
    await page.getByRole("button", { name: "Open imported run", exact: true }).click();
    await page.locator(".run-header").waitFor();
    await page.locator("summary").filter({ hasText: /^Save next action$/ }).click();
    const originalCapture = await page.evaluate(() => state.review.capture.capture_id);
    const originalRun = await page.evaluate(() => state.runId);
    const replacement = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../agr/demo_fixtures/clean_pass.atif.json"), "utf8"));
    replacement.run.model = "newer-browser-capture";
    async function jsonRequest(method, route, data) {
      const response = await page.request.fetch(base + route, { method, data });
      assert(response.ok(), await response.text());
      return response.json();
    }
    const prefix = "/projects/" + project;
    const staged = await jsonRequest("POST", prefix + "/import-previews", { source: "atif" });
    const upload = await page.request.put(base + prefix + "/import-previews/" + staged.id + "/files?name=newer.json", { data: Buffer.from(JSON.stringify(replacement)), headers: { "content-type": "application/octet-stream" } });
    assert(upload.ok(), await upload.text());
    const manifest = await jsonRequest("POST", prefix + "/import-previews/" + staged.id + "/inspect", {});
    let newer = await jsonRequest("POST", prefix + "/imports", { preview_id: staged.id, manifest_hash: manifest.manifest_hash, selected: manifest.items.map(i => i.id), idempotency_key: "browser-stale-view-" + staged.id });
    for (let attempts = 0; ["queued", "importing"].includes(newer.status) && attempts < 200; attempts++) {
      await new Promise(resolve => setTimeout(resolve, 50));
      newer = await jsonRequest("GET", prefix + "/imports/" + newer.id);
    }
    assert.equal(newer.status, "completed");
    assert.notEqual(newer.results[0].capture_id, originalCapture);
    await page.getByLabel("Investigation title", { exact: true }).fill("Inspect a captured behavior");
    await page.getByLabel("Next action", { exact: true }).fill("Bring in another run after changing the configuration");
    await page.getByRole("button", { name: "Save next action", exact: true }).click();
    await page.locator("#investigations-button").click();
    await page.getByText("Inspect a captured behavior", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Open evidence", exact: true }).click();
    await page.getByText(/Saved evidence snapshot/).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("capture"), originalCapture);
    assert.equal(await page.evaluate(() => state.review.capture.capture_id), originalCapture);
    const snapshotHeaders = await page.evaluate(() => ({ target: projectHeaders("/runs/" + encodeURIComponent(state.runId) + "/divergence"), unrelated: projectHeaders("/runs/another-run") }));
    assert.equal(snapshotHeaders.target["x-agr-capture"], originalCapture);
    assert.equal(snapshotHeaders.unrelated["x-agr-capture"], undefined);
    await page.getByRole("button", { name: "Open latest capture", exact: true }).click();
    await page.waitForFunction(capture => !state.loading && state.review && state.review.capture.capture_id === capture, newer.results[0].capture_id);
    await page.locator(".run-header").waitFor();
    assert.equal(await page.evaluate(() => state.runId), originalRun);
    assert.equal(await page.evaluate(() => state.review.capture.capture_id), newer.results[0].capture_id);
    await page.locator("#settings-button").click();
    await page.getByLabel("Reviewer display name").fill("Browser Engineer");
    await page.getByRole("button", { name: "Save settings", exact: true }).click();
    await page.locator("#reviewer-avatar[title^='Browser Engineer']").waitFor();
    await page.locator("#project-switcher").selectOption("new");
    await page.getByLabel("Project name", { exact: true }).fill("Second browser project");
    await page.getByRole("button", { name: "Create project", exact: true }).click();
    await page.getByRole("heading", { name: "Add runs to Second browser project" }).waitFor();
    await page.locator("#home-button").click();
    await page.getByText("No runs yet. Add a run from your tools to start investigating.", { exact: true }).waitFor();
    await page.locator("#project-switcher").selectOption(project);
    await page.getByRole("heading", { name: "Browser test project", exact: true }).waitFor();
    await page.getByRole("button", { name: "Continue investigation", exact: true }).click();
    await page.locator(".run-header").waitFor();
    await page.reload();
    await page.locator(".run-header").waitFor();
    assert.equal(new URL(page.url()).searchParams.get("project"), project);
    await page.locator("#sources-button").click();
    await page.getByRole("heading", { name: "Bring runs in from your tools", exact: true }).waitFor();
    assert.equal(await page.locator(".source-card").count(), 7);
    await page.screenshot({ path: process.env.AGR_SCREENSHOT || path.resolve(__dirname, "../.agr-store/onboarding-check/sources.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    await page.screenshot({ path: process.env.AGR_MOBILE_SCREENSHOT || path.resolve(__dirname, "../.agr-store/onboarding-check/sources-mobile.png"), fullPage: true });
    assert.deepEqual(errors, [], "Browser runtime errors");
    console.log("Browser onboarding verified, including a saved action after a newer capture arrives, snapshot header scope, project isolation, reload, and narrow layout.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
