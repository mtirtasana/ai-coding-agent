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
} = require("../dist/shared.cjs");
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
