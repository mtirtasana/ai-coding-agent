const { test } = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const docs = new Map();
const replies = [];
let root;
let saves = 0;
class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}
class Range {
  constructor(start, end) {
    this.start = start;
    this.end = end;
  }
}
const uri = (file) => ({ fsPath: file, toString: () => `file:${file}` });
const vscode = {
  Uri: { file: uri, parse: (value) => ({ toString: () => value }) },
  Position,
  Range,
  workspace: {
    isTrusted: true,
    textDocuments: [],
    getWorkspaceFolder: () => root,
    getConfiguration: () => ({
      get: (name, fallback) =>
        name === "checks"
          ? { test: { command: ["python", "-m", "unittest"] } }
          : fallback,
    }),
    openTextDocument: async (target) => {
      let doc = docs.get(target.fsPath);
      if (!doc) {
        doc = {
          uri: target,
          version: 1,
          isClosed: false,
          isDirty: false,
          text: await fs.readFile(target.fsPath, "utf8"),
          getText() {
            return this.text;
          },
          positionAt(n) {
            return n;
          },
          async save() {
            saves++;
            return true;
          },
        };
        docs.set(target.fsPath, doc);
      }
      return doc;
    },
  },
  window: {
    showTextDocument: async (doc) => ({
      edit: async (action) => {
        let value;
        action({
          replace: (_range, text) => {
            value = text;
          },
        });
        doc.text = value;
        doc.version++;
        doc.isDirty = true;
        return true;
      },
    }),
    showWarningMessage: async () => replies.shift(),
  },
  commands: { executeCommand: async () => {} },
  languages: { getDiagnostics: () => [] },
};
const load = Module._load;
Module._load = function (name, ...rest) {
  if (name === "vscode") return vscode;
  return load.call(this, name, ...rest);
};
const { EditorTools } = require("../dist/editor.cjs");
const { Controller } = require("../dist/extension.js");
Module._load = load;
const { sha } = require("../dist/shared.cjs");
async function fixture(run) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "agent-editor-"));
  root = { uri: uri(directory), name: "fixture" };
  docs.clear();
  vscode.workspace.isTrusted = true;
  vscode.workspace.textDocuments = [];
  replies.length = 0;
  saves = 0;
  try {
    await fs.writeFile(path.join(directory, "a.py"), "value = 1\n");
    await run(new EditorTools(root, () => {}), directory);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
}
async function proposal(tools) {
  for (let i = 0; i < 100 && !tools.proposals.size; i++)
    await new Promise((r) => setTimeout(r, 2));
  assert.equal(tools.proposals.size, 1);
  return [...tools.proposals.keys()][0];
}
test("dirty snapshots, per-diff approval, disk preservation and stale review rejection", async () =>
  fixture(async (tools, dir) => {
    const doc = (await tools.read("a.py")).document;
    doc.text = "# dirty\nvalue = 1\n";
    doc.version++;
    const snap = JSON.parse(
      (
        await tools.execute(
          "file_snapshot",
          { path: "a.py" },
          "plan",
          () => true,
        )
      ).content,
    );
    assert.equal(snap.sha256, sha(doc.text));
    assert(snap.content.startsWith("# dirty"));
    const result = tools.execute(
      "apply_edit",
      {
        path: "a.py",
        expected_sha256: snap.sha256,
        old_text: "value = 1",
        new_text: "value = 2",
      },
      "implement",
      () => true,
    );
    const id = await proposal(tools);
    assert(doc.text.includes("value = 1"));
    await tools.decide(id, true);
    assert.equal((await result).ok, true);
    assert(doc.text.includes("value = 2"));
    assert.equal(
      await fs.readFile(path.join(dir, "a.py"), "utf8"),
      "value = 1\n",
    );
    const stale = tools.execute(
      "write_file",
      { path: "a.py", content: "overwrite" },
      "implement",
      () => true,
    );
    const next = await proposal(tools);
    doc.text = "newer user edit";
    doc.version++;
    await tools.decide(next, true);
    assert.equal((await stale).ok, false);
    assert.equal(doc.text, "newer user edit");
  }));
test("planning, cancellation, trust and symlink escapes cannot write", async () =>
  fixture(async (tools, dir) => {
    assert.equal(
      (
        await tools.execute(
          "write_file",
          { path: "a.py", content: "bad" },
          "plan",
          () => true,
        )
      ).ok,
      false,
    );
    const rejected = tools.execute(
      "write_file",
      { path: "a.py", content: "bad" },
      "implement",
      () => true,
    );
    await proposal(tools);
    tools.cancel();
    assert.equal((await rejected).ok, false);
    vscode.workspace.isTrusted = false;
    assert.equal(
      (await tools.execute("read_file", { path: "a.py" }, "plan", () => true))
        .ok,
      false,
    );
    vscode.workspace.isTrusted = true;
    assert.equal(
      (
        await tools.execute(
          "read_file",
          { path: "../outside" },
          "plan",
          () => true,
        )
      ).ok,
      false,
    );
    if (process.platform !== "win32") {
      await fs.symlink(os.tmpdir(), path.join(dir, "outside"));
      await assert.rejects(tools.guarded("outside/file"));
    }
    assert.equal(
      await fs.readFile(path.join(dir, "a.py"), "utf8"),
      "value = 1\n",
    );
  }));
test("checks need session permission and saved buffers; decline never saves", async () =>
  fixture(async () => {
    const controller = new Controller({ subscriptions: [] });
    const session = { folder: root, mode: "implement", permissions: new Set() };
    replies.push("Skip");
    assert.equal(
      await controller.bridge(
        session,
        "editor/beforeCheck",
        { profile: "test" },
        () => true,
      ),
      false,
    );
    assert.equal(session.permissions.size, 0);
    replies.push("Allow check", "Skip");
    vscode.workspace.textDocuments = [
      {
        uri: uri(path.join(root.uri.fsPath, "dirty.py")),
        isDirty: true,
        save: async () => {
          saves++;
          return true;
        },
      },
    ];
    assert.equal(
      await controller.bridge(
        session,
        "editor/beforeCheck",
        { profile: "test" },
        () => true,
      ),
      false,
    );
    assert.equal(saves, 0);
    replies.push("Save and run");
    assert.equal(
      await controller.bridge(
        session,
        "editor/beforeCheck",
        { profile: "test" },
        () => true,
      ),
      true,
    );
    assert.equal(saves, 1);
    session.mode = "plan";
    assert.equal(
      await controller.bridge(
        session,
        "editor/beforeCheck",
        { profile: "test" },
        () => true,
      ),
      false,
    );
  }));
