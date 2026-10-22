const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {
  relativePath,
  replaceUnique,
  validateText,
  validateChecks,
  completionContext,
  sameStamp,
  providerKey,
  lineRange,
  replacementDiff,
  CompletionCache,
} = require("../dist/shared.cjs");
test("replacement previews contain bounded context, accurate coordinates and newline markers", () => {
  const before = Array.from({ length: 1000 }, (_, i) => `line ${i}\n`).join("");
  const after = before.replace("line 500\n", "changed 😀\n");
  const diff = replacementDiff("a.txt", before, after);
  assert.match(diff, /@@ -498,7 \+498,7 @@/);
  assert.match(diff, /-line 500\n\+changed 😀\n/);
  assert(!diff.includes("line 0\n"));
  assert(diff.length < 250);
  assert.equal(replacementDiff("a", "same", "same"), "");
  assert.equal(replacementDiff("a", "", "new\n"), "--- a\n+++ a\n@@ -0,0 +1,1 @@\n+new\n");
  assert.equal(replacementDiff("a", "old\n", ""), "--- a\n+++ a\n@@ -1,1 +0,0 @@\n-old\n");
  assert.equal(replacementDiff("a", "old", "new"), "--- a\n+++ a\n@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n");
  assert.match(replacementDiff("a", "one\r\ntwo\r\n", "one\r\nthree\r\n"), / one\r\n-two\r\n\+three\r\n/);
});
function cancellation() {
  const listeners = new Set();
  return {
    isCancellationRequested: false,
    onCancellationRequested(fn) { listeners.add(fn); return { dispose: () => listeners.delete(fn) }; },
    cancel() { this.isCancellationRequested = true; for (const fn of [...listeners]) fn(); },
    listeners,
  };
}
test("identical inline requests share inference and cancellation keeps other consumers alive", async () => {
  const cache = new CompletionCache();
  let resolve, cancelled = 0, started = 0;
  const start = () => { started++; return { result: new Promise(done => { resolve = done; }), cancel: () => cancelled++ }; };
  const first = cancellation(), second = cancellation();
  const a = cache.get("same", first, start), b = cache.get("same", second, start);
  first.cancel();
  assert.equal(await a, undefined);
  assert.equal(cancelled, 0);
  resolve("suggestion");
  assert.equal(await b, "suggestion");
  assert.equal(await cache.get("same", cancellation(), start), "suggestion");
  assert.equal(started, 1);
  assert.deepEqual(cache.stats, { requested: 1, reused: 2, discarded: 1 });
  assert.equal(first.listeners.size + second.listeners.size, 0);
});
test("obsolete inline inference is cancelled and errors and expired results are not reused", async () => {
  const cache = new CompletionCache();
  let cancelled = 0;
  const token = cancellation();
  const pending = cache.get("old", token, () => ({ result: new Promise(() => {}), cancel: () => cancelled++ }));
  token.cancel();
  assert.equal(await pending, undefined);
  assert.equal(cancelled, 1);
  await assert.rejects(cache.get("error", cancellation(), () => ({ result: Promise.reject(Error("offline")), cancel() {} })));
  assert.equal(await cache.get("error", cancellation(), () => ({ result: Promise.resolve("recovered"), cancel() {} })), "recovered");
  const original = Date.now;
  try {
    Date.now = () => original() + 11000;
    assert.equal(await cache.get("error", cancellation(), () => ({ result: Promise.resolve("fresh"), cancel() {} })), "fresh");
  } finally { Date.now = original; }
  cache.clear();
});
test("line ranges preserve logical endings, Unicode and explicit empty/EOF metadata", () => {
  for (const ending of ["\n", "\r\n", "\r"]) {
    const text = `alpha${ending}😀 café${ending}last`;
    assert.deepEqual(lineRange(text, 2, 200), {
      start_line: 2,
      end_line: 3,
      total_lines: 3,
      content: `😀 café${ending}last`,
    });
    assert.equal(lineRange(`one${ending}`, 1, 1).total_lines, 1);
  }
  assert.deepEqual(lineRange("", 1, 1), {
    start_line: 0,
    end_line: 0,
    total_lines: 0,
    content: "",
  });
  assert.equal(lineRange("\n\n", 2, 2).content, "\n");
  assert.equal(lineRange("a\u2028b", 1, 1).total_lines, 1);
  assert.equal([...lineRange("😀".repeat(12000), 1, 1).content].length, 12000);
  assert.equal(lineRange("a\n".repeat(200), 1, 200).end_line, 200);
  for (const [text, start, end] of [
    ["", 2, 2],
    ["one\n", 2, 2],
    ["a", 0, 1],
    ["a", 2, 1],
    ["a", true, 1],
    ["a", "1", 1],
    ["a", 1, 1.5],
    ["a", 1, Number.MAX_SAFE_INTEGER + 1],
    ["a\n".repeat(300), 1, 201],
    ["x".repeat(12001), 1, 1],
  ])
    assert.throws(() => lineRange(text, start, end));
});
test("path containment and protected components", () => {
  const root = path.resolve("fixture");
  for (const p of [
    "../escape",
    ".git/config",
    ".env.local",
    "x/.aws/credentials",
  ])
    assert.throws(() => relativePath(root, p));
  assert.equal(relativePath(root, "src/a.py"), path.join(root, "src/a.py"));
});
test("unique replacement rejects overlaps and invalid UTF-8 without mutation", () => {
  assert.throws(() => replaceUnique("aaa", "aa", "x"));
  assert.throws(() => replaceUnique("hello", "missing", "x"));
  assert.equal(replaceUnique("hello", "ell", "i"), "hio");
  for (const text of ["\ud800", "x\0x", "x".repeat(100001)])
    assert.throws(() => validateText(text));
});
test("commands use explicit argv and finite limits", () => {
  for (const bad of [
    { test: { command: "python" } },
    { unknown: { command: ["x"] } },
    { test: { command: ["x"], timeout: Infinity } },
    { test: { command: ["x"], outputLimit: 0 } },
    { lint_fix: { command: ["x"] } },
  ])
    assert.throws(() => validateChecks(bad));
  assert.deepEqual(
    validateChecks({ test: { command: ["python", "-m", "unittest"] } }).test
      .command,
    ["python", "-m", "unittest"],
  );
});
test("bounded completion context and stale-response identities", () => {
  const value = completionContext("a".repeat(18000), 10000);
  assert.equal(value.prefix.length, 6000);
  assert.equal(value.suffix.length, 2000);
  const a = { uri: "file:a", version: 1, offset: 2 };
  assert(sameStamp(a, { ...a }));
  assert(!sameStamp(a, { ...a, version: 2 }));
  assert(!sameStamp(a, { ...a, offset: 3 }));
});
test("endpoint-specific secrets and URL restrictions", () => {
  assert.equal(
    providerKey("ollama", "http://localhost:11434/"),
    "ollama:http://localhost:11434",
  );
  assert.notEqual(
    providerKey("openai-compatible", "https://a/v1"),
    providerKey("openai-compatible", "https://b/v1"),
  );
  for (const url of [
    "file:///tmp/test",
    "https://name:secret@host",
    "https://host?key=secret",
    "https://host#x",
  ])
    assert.throws(() => providerKey("openai-compatible", url));
});
