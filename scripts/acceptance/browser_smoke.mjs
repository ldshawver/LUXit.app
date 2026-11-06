// Real-browser smoke of pages whose buttons POST with fetch(): run by
// isolated_browser_rig.sh against an isolated instance (no network).
// Each step prints PASS/FAIL; exit 1 on any FAIL.
import { chromium } from "playwright";

const BASE = process.env.RIG_BASE || "http://localhost:8099";
const PASSWORD = process.env.RIG_PASSWORD;
const results = [];
const step = async (name, fn) => {
  try { const detail = await fn(); results.push(["PASS", name, detail || ""]); }
  catch (e) { results.push(["FAIL", name, String(e.message || e).split("\n")[0].slice(0, 300)]); }
};
const expect = (cond, msg) => { if (!cond) throw new Error(msg); };

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await (await browser.newContext()).newPage();
page.on("dialog", d => d.accept());

await step("sign in", async () => {
  await page.goto(`${BASE}/auth/login`);
  await page.fill("#username", "rig-owner");
  await page.fill("#password", PASSWORD);
  await Promise.all([page.waitForLoadState("networkidle"), page.click("button[type=submit]")]);
  expect(!page.url().includes("/auth/login"), `still on login: ${page.url()}`);
  return page.url();
});

await step("billing: subscribe button reaches the checkout endpoint", async () => {
  await page.goto(`${BASE}/app/billing`);
  const [r] = await Promise.all([
    page.waitForResponse(r => r.url().includes("/api/stripe/create-checkout-session"), { timeout: 10000 }),
    page.click("[data-action='checkout']"),
  ]);
  const body = await r.json().catch(() => ({}));
  expect(body.code !== "csrf_failed" && r.status() !== 302, `CSRF rejected (${r.status()})`);
  const err = await page.textContent("#billing-error");
  expect(!/No checkout URL returned|Security check failed/.test(err || ""), `button error: ${err}`);
  return `endpoint ${r.status()}: ${(err || "").trim().slice(0, 80)}`;
});

await step("segments: create (form) then copy (fetch)", async () => {
  await page.goto(`${BASE}/segments`);
  const title = await page.title();
  expect(!/<script|document\./.test(title), `title contains script text: ${title.slice(0, 60)}`);
  await page.click("[data-bs-target='#createSegmentModal']");
  await page.fill("#createSegmentModal #name", "Rig Segment");
  await Promise.all([page.waitForLoadState("networkidle"), page.click("#createSegmentModal button[type=submit]")]);
  await page.goto(`${BASE}/segments`);
  const before = await page.locator(".segment-copy").count();
  expect(before >= 1, "segment was not created");
  const [r] = await Promise.all([
    page.waitForResponse(r => /\/api\/segments\/\d+\/copy/.test(r.url()), { timeout: 10000 }),
    page.locator(".segment-copy").first().click(),
  ]);
  expect(r.ok(), `copy returned ${r.status()}`);
  await page.waitForLoadState("networkidle");
  await page.goto(`${BASE}/segments`);
  const after = await page.locator(".segment-copy").count();
  expect(after === before + 1, `segments ${before} -> ${after}`);
  return `segments ${before} -> ${after}`;
});

await step("segments: active toggle (fetch PATCH) persists", async () => {
  const toggle = page.locator(".segment-active-toggle").first();
  const was = await toggle.isChecked();
  const [r] = await Promise.all([
    page.waitForResponse(r => /\/api\/segments\/\d+$/.test(r.url()) && r.request().method() === "PATCH", { timeout: 10000 }),
    toggle.click(),
  ]);
  expect(r.ok(), `PATCH returned ${r.status()}`);
  await page.goto(`${BASE}/segments`);
  const now = await page.locator(".segment-active-toggle").first().isChecked();
  expect(now === !was, `toggle did not persist (${was} -> ${now})`);
  return `${was} -> ${now}`;
});

await step("account: export downloads a ZIP", async () => {
  await page.goto(`${BASE}/app/billing`);
  const [d] = await Promise.all([
    page.waitForEvent("download", { timeout: 10000 }),
    page.click("#account-data a", { timeout: 10000 }),
  ]);
  expect(/\.zip$/.test(d.suggestedFilename()), `download ${d.suggestedFilename()}`);
  return d.suggestedFilename();
});

await step("account: close workspace from the billing page", async () => {
  await page.goto(`${BASE}/app/billing`);
  await page.fill("#close-confirm", "Browser Rig Co", { timeout: 10000 });
  await Promise.all([page.waitForURL(/\/account\/closed/, { timeout: 15000 }), page.click("#close-workspace")]);
  await page.goto(`${BASE}/dashboard`);
  expect(page.url().includes("/account/closed"), `dashboard after close: ${page.url()}`);
  return page.url();
});

await browser.close();
for (const [s, n, d] of results) console.log(`${s}  ${n}${d ? "  — " + d : ""}`);
const failed = results.filter(r => r[0] === "FAIL").length;
console.log(failed ? `SUMMARY FAIL ${failed}/${results.length}` : `SUMMARY PASS ${results.length}/${results.length}`);
process.exit(failed ? 1 : 0);
