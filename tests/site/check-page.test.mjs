// node --test tests/site/check-page.test.mjs: the repo checker's page (site/check/index.html) and the part of
// check.mjs that shows and hides its sections. check.mjs needs a browser, so its function is run on stand-in elements.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const read = (name) => fs.readFileSync(new URL(`../../site/check/${name}`, import.meta.url), "utf8");
const TOOL = "https://github.com/wheelgap/wheelgap";

test("the line under a result links the tool's repo and its GitHub Action, as static text", () => {
  const html = read("index.html");
  const line = html.match(/<p class="note star-line" id="star-line" hidden>(.*?)<\/p>/s);
  assert.ok(line, "the line is in the page, hidden until a result is shown");
  const out = html.slice(html.indexOf('<main class="wrap" id="out" hidden>'), html.indexOf("</main>"));
  assert.ok(out.lastIndexOf("</section>") < out.indexOf('id="star-line"'), "it comes after the result's sections");
  const links = [...line[1].matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(links, [
    [`${TOOL}/blob/main/docs/ci.md#github-action`, "GitHub Action"],
    [TOOL, '<span aria-hidden="true">★</span> star the project on GitHub'],
  ]);
  assert.match(line[1], /^wheelgap is open source\. /);
  // Plain links: the page's Content-Security-Policy allows no remote images or frames, so no star-count widget.
  assert.doesNotMatch(line[1], /<(img|iframe|script)\b/);
  assert.doesNotMatch(read("check.mjs"), /star-line[^\n]*innerHTML/);
});

test("the line shows with a result (gaps or none) and hides when there is nothing to show", () => {
  const source = read("check.mjs").match(/^function hideResultSections\(.*?^}$/ms);
  assert.ok(source, "hideResultSections is in check.mjs");
  const made = new Map();
  const element = (key) => {
    if (!made.has(key)) made.set(key, { hidden: null, classList: { remove() {} }, closest: () => element(`${key} section`) });
    return made.get(key);
  };
  const hideResultSections = new Function("$", `${source[0]}; return hideResultSections;`)(element);
  hideResultSections(false);        // render(): a result, whether it found gaps or every wheel
  assert.equal(element("star-line").hidden, false);
  assert.equal(element("gaps-title section").hidden, false);
  hideResultSections(true);         // no lockfile found
  assert.equal(element("star-line").hidden, true);
  hideResultSections(true, true);   // a lockfile the browser can't check
  assert.equal(element("star-line").hidden, true);
  assert.equal(element("setup-title section").hidden, false);
});

test("a result with no package checked shows the verdict and the setup, but no bars or table to read as a pass", () => {
  const source = read("check.mjs").match(/^function render\(\) \{.*?^}$/ms);
  assert.ok(source, "render is in check.mjs");
  const parts = ["hideResultSections", "renderVerdict", "renderSetup", "renderTargets", "renderTable", "renderNext"];
  const calls = [];
  const render = (packages) => new Function("state", ...parts, `${source[0]}; return render;`)(
    { result: { packages } },
    ...parts.map((name) => (...args) => { calls.push([name, ...args]); }),
  );
  render(0)(); // every package skipped as a private index's, or a file that lists none
  assert.deepEqual(calls, [["hideResultSections", false], ["renderVerdict"], ["renderSetup"], ["hideResultSections", true, true]]);
  calls.length = 0;
  render(3)();
  assert.deepEqual(calls.map(([name]) => name), parts);
});

// worker.mjs needs a browser and Pyodide, so its check() is run on stand-ins for both: `needs` is what the driver asks
// to be looked up, `fetch` answers for pypi.org, and `checked` collects what the driver's check() is handed.
function workerCheck({ needs, fetch, releases = new Map(), lockfile = "requirements.txt", fetched = [lockfile], gone = [] }) {
  const source = read("worker.mjs");
  const fn = (name) => {
    const found = source.match(new RegExp(`^(?:async )?function ${name}\\(.*?^}$`, "ms"));
    assert.ok(found, `${name} is in worker.mjs`);
    return found[0];
  };
  const checked = [];
  const removed = [];
  // `gone`: paths the worker believes it wrote that aren't in Pyodide's files any more, so removing them throws.
  const unlink = (path) => { removed.push(path); if (gone.includes(path)) throw new Error(`ENOENT: ${path}`); };
  const driver = {
    needs: () => JSON.stringify(needs),
    check: (...args) => { checked.push(args); return JSON.stringify({ rows: [] }); },
  };
  const repo = { root: "/repos/1", ref: "HEAD", fetched: new Set(fetched) };
  const env = {
    fetch, releases, MAX_LOOKUPS: 300,
    ready: Promise.resolve({ py: { FS: { unlink } }, driver }),
    loaded: new Map([[repo.root, repo]]),
    post: () => {},
  };
  const made = new Function("env", `
    const { fetch, releases, MAX_LOOKUPS, ready, loaded, post } = env;
    let latestCheck = 7;
    class Failure extends Error { constructor(kind, message) { super(message); this.kind = kind; } }
    ${fn("pool")}
    ${fn("lookups")}
    ${fn("dropOtherProjects")}
    ${fn("check")}
    return { check: (lockfile) => check(7, "/repos/1", lockfile, { pythons: ["3.14"] }), supersede: () => { latestCheck = 8; } };
  `)(env);
  return { check: () => made.check(lockfile), supersede: made.supersede, checked, releases, removed, repo };
}

test("the worker looks up what the driver names on pypi.org and hands the answers to the driver's check", async () => {
  const urls = [];
  const fetch = async (url) => {
    urls.push(url);
    if (url.includes("/gone/")) return { ok: false, status: 404 };
    // As PyPI answers: a release's JSON has no `releases`, a project's JSON does.
    const doc = url.endsWith("/numpy/2.3.4/json") ? { info: {}, urls: [] } : { info: {}, urls: [], releases: { "2.0.0": [] } };
    return { ok: true, status: 200, json: async () => doc };
  };
  const needs = { lookups: [{ name: "numpy", version: "2.3.4" }, { name: "fastlib", version: null }, { name: "gone", version: "1.0" }], total: 3 };
  const { check, checked, releases } = workerCheck({ needs, fetch });
  await check();
  assert.deepEqual(urls.sort(), [
    "https://pypi.org/pypi/fastlib/json",
    "https://pypi.org/pypi/gone/1.0/json",
    "https://pypi.org/pypi/numpy/2.3.4/json",
  ]);
  assert.equal(checked.length, 1);
  const [root, file, , lookups] = checked[0];
  assert.deepEqual([root, file], ["/repos/1", "requirements.txt"]);
  assert.deepEqual(JSON.parse(lookups), {
    "numpy==2.3.4": { info: {}, urls: [] },
    fastlib: { info: {}, urls: [], releases: { "2.0.0": [] } },
    "gone==1.0": null, // a 404: the driver reports it as unknown
  });
  // fixes() asks PyPI for a project's release list unless this cache has its name. A pinned release's JSON has no
  // release list, so caching it under the name would leave every gap of a pinned file "not looked up".
  assert.deepEqual([...releases], [["fastlib", { "2.0.0": [] }]]);
});

test("a lockfile that lists its files makes no request to PyPI, and the driver's check is still handed JSON text", async () => {
  const fetch = async () => assert.fail("no lookup was asked for");
  const { check, checked } = workerCheck({ needs: { lookups: [], total: 0 }, fetch });
  await check();
  assert.equal(checked[0][3], "{}"); // never a JS null, which Pyodide may not turn into None
});

test("the worker counts the lookups that got no answer, for the page to say why packages are unknown", async () => {
  const signals = [];
  const fetch = async (url, init) => {
    signals.push(init?.signal);
    if (url.includes("/down/")) throw new TypeError("Failed to fetch");
    if (url.includes("/slow/")) throw new DOMException("The operation timed out.", "TimeoutError");
    if (url.includes("/busy/")) return { ok: false, status: 503 };
    if (url.includes("/gone/")) return { ok: false, status: 404 };
    if (url.includes("/html/")) return { ok: true, status: 200, json: async () => { throw new SyntaxError("Unexpected token <"); } };
    return { ok: true, status: 200, json: async () => ({ info: {}, urls: [] }) };
  };
  const names = ["fine", "down", "slow", "busy", "gone", "html"];
  const needs = { lookups: names.map((name) => ({ name, version: "1.0" })), total: names.length };
  const { check, checked } = workerCheck({ needs, fetch });
  const result = await check();
  assert.deepEqual(result.missed, { cap: 300, over_cap: 0, not_found: 1, failed: 4 });
  // Only an answer, or PyPI's "no such release", reaches the driver: the rest have no key, and stay "not looked up".
  assert.deepEqual(JSON.parse(checked[0][3]), { "fine==1.0": { info: {}, urls: [] }, "gone==1.0": null });
  // Every lookup can be given up on after a while: a stalled one would otherwise hold the whole check.
  assert.equal(signals.length, names.length);
  assert.ok(signals.every((signal) => signal instanceof AbortSignal));
});

test("lookups over the cap aren't made, and the result says how many there were", async () => {
  const urls = [];
  const fetch = async (url) => {
    urls.push(url);
    return { ok: true, status: 200, json: async () => ({ info: {}, urls: [] }) };
  };
  const needs = { lookups: Array.from({ length: 301 }, (_, i) => ({ name: `pkg${i}`, version: "1.0" })), total: 301 };
  const { check, checked } = workerCheck({ needs, fetch });
  const result = await check();
  assert.equal(urls.length, 300);
  assert.deepEqual(result.missed, { cap: 300, over_cap: 1, not_found: 0, failed: 0 });
  const lookups = JSON.parse(checked[0][3]);
  assert.equal(Object.keys(lookups).length, 300);
  assert.equal("pkg300==1.0" in lookups, false); // no key: the driver reports it as unknown, never as a gap
});

test("a check superseded while it looks packages up stops asking PyPI and never reaches the driver's check", async () => {
  let supersede = () => {};
  const urls = [];
  const fetch = async (url) => {
    urls.push(url);
    supersede(); // a newer check arrives while this one is in flight
    return { ok: true, status: 200, json: async () => ({ info: {}, urls: [] }) };
  };
  const needs = { lookups: Array.from({ length: 20 }, (_, i) => ({ name: `pkg${i}`, version: "1.0" })), total: 20 };
  const worker = workerCheck({ needs, fetch });
  supersede = worker.supersede;
  await assert.rejects(worker.check(), (e) => e.kind === "stale");
  assert.equal(worker.checked.length, 0); // the driver keeps the last check's targets for fixes(): not a stale one's
  assert.ok(urls.length <= 6, `the six in flight at most, not all twenty: ${urls.length}`);
});

test("a pyproject.toml that was fetched to be checked is gone again before another file is read", async () => {
  // wheelgap reads the pyproject.toml beside a uv.lock for [tool.uv] settings when it's there: left behind, a uv.lock
  // checked after the pyproject.toml could answer differently from the same uv.lock checked first.
  const fetch = async () => assert.fail("no lookup was asked for");
  const fetched = ["uv.lock", "pyproject.toml", "backend/pyproject.toml", "requirements.txt"];
  const lock = workerCheck({ needs: { lookups: [], total: 0 }, fetch, lockfile: "uv.lock", fetched });
  await lock.check();
  assert.deepEqual(lock.removed, ["/repos/1/pyproject.toml", "/repos/1/backend/pyproject.toml"]);
  assert.deepEqual([...lock.repo.fetched], ["uv.lock", "requirements.txt"]); // fetched again if it's picked again
  // The one being checked stays, of course.
  const project = workerCheck({ needs: { lookups: [], total: 0 }, fetch, lockfile: "pyproject.toml", fetched });
  await project.check();
  assert.deepEqual(project.removed, ["/repos/1/backend/pyproject.toml"]);
  assert.equal(project.checked[0][1], "pyproject.toml");
  // A check that's already superseded removes nothing: the newer check may be about to read that file.
  const stale = workerCheck({ needs: { lookups: [], total: 0 }, fetch, lockfile: "uv.lock", fetched });
  stale.supersede();
  await assert.rejects(stale.check(), (e) => e.kind === "stale");
  assert.deepEqual([stale.removed, stale.checked], [[], []]);
  // One that's already gone from the worker's files isn't an error, and isn't tried again on the next check.
  const missing = workerCheck({ needs: { lookups: [], total: 0 }, fetch, lockfile: "uv.lock", fetched, gone: ["/repos/1/pyproject.toml"] });
  await missing.check();
  assert.deepEqual(missing.removed, ["/repos/1/pyproject.toml", "/repos/1/backend/pyproject.toml"]);
  assert.deepEqual([...missing.repo.fetched], ["uv.lock", "requirements.txt"]);
  assert.equal(missing.checked.length, 1);
});
