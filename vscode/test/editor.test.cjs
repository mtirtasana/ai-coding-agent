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
  ProgressLocation: { Notification: 15 },
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
  vscode.workspace.workspaceFolders = [root];
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
test("preview uses a small unified diff from the dirty buffer without editing", async () =>
  fixture(async (tools) => {
    const doc = (await tools.read("a.py")).document;
    doc.text = Array.from({ length: 1000 }, (_, i) => `value_${i} = ${i}\n`).join("");
    doc.version++;
    const before = doc.text;
    const reply = await tools.execute("preview_edit", { path: "a.py", expected_sha256: sha(before),
      old_text: "value_500 = 500", new_text: "value_500 = 501" }, "plan", () => true);
    assert.equal(reply.ok, true, reply.content);
    const preview = JSON.parse(reply.content);
    assert(preview.diff.length < 400);
    assert.equal(preview.diff_truncated, false);
    assert.match(preview.diff, /-value_500 = 500\n\+value_500 = 501/);
    assert.equal(doc.text, before);
    assert.equal(tools.proposals.size, 0);
  }));
test("range reads use dirty buffers and full hashes; stale range edits and invalid arguments fail", async () =>
  fixture(async (tools, dir) => {
    const doc = (await tools.read("a.py")).document;
    doc.text = "# unsaved 😀\r\nvalue = 1\r\nlast\r\n";
    doc.version++;
    const controller = new Controller({ subscriptions: [] });
    const session = { tools, mode: "plan" };
    const response = await controller.bridge(
      session,
      "editor/tool",
      {
        name: "read_file_range",
        arguments: { path: "./a.py", start_line: 2, end_line: 2 },
      },
      () => true,
    );
    assert.equal(response.ok, true, response.content);
    const range = JSON.parse(response.content);
    assert.equal(range.path, "a.py");
    assert.equal(range.content, "value = 1\r\n");
    assert.equal(range.sha256, sha(doc.text));
    assert.equal(range.documentVersion, doc.version);
    assert.equal(range.total_lines, 3);
    const args = {
      path: "a.py",
      expected_sha256: range.sha256,
      old_text: "value = 1",
      new_text: "value = 2",
    };
    const edit = tools.execute("apply_edit", args, "implement", () => true);
    await tools.decide(await proposal(tools), true);
    assert.equal((await edit).ok, true);
    assert.equal(
      await fs.readFile(path.join(dir, "a.py"), "utf8"),
      "value = 1\n",
    );
    assert.equal(
      (await tools.execute("apply_edit", args, "implement", () => true)).ok,
      false,
    );
    for (const patch of [
      { start_line: "1" },
      { start_line: true },
      { end_line: 1.5 },
      { end_line: Number.MAX_SAFE_INTEGER + 1 },
      { end_line: 202 },
      { extra: "x" },
      { path: "../outside" },
      { path: ".git/config" },
      { start_line: 5, end_line: 5 },
    ]) {
      assert.equal(
        (
          await tools.execute(
            "read_file_range",
            {
              path: "a.py",
              start_line: 1,
              end_line: 2,
              ...patch,
            },
            "plan",
            () => true,
          )
        ).ok,
        false,
        JSON.stringify(patch),
      );
    }
    assert.equal(
      (await tools.execute("read_file", { path: 1 }, "plan", () => true)).ok,
      false,
    );
    doc.text = "x".repeat(12001);
    assert.equal(
      (
        await tools.execute(
          "read_file_range",
          {
            path: "a.py",
            start_line: 1,
            end_line: 1,
          },
          "plan",
          () => true,
        )
      ).ok,
      false,
    );
  }));
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

test("cancel during backend startup cannot submit a late task or overwrite the next checkpoint", async () =>
  fixture(async (tools) => {
    const controller = new Controller({ subscriptions: [] });
    const session = controller.session(root);
    controller.selected = session;
    let release;
    let firstRequests = 0;
    const firstClient = {
      request: async () => {
        firstRequests++;
        return {};
      },
    };
    const nextCheckpoint = {
      messages: [],
      latest_plan: "second plan",
      accepted_plan: null,
    };
    let count = 0;
    controller.initialize = async () => {
      if (count++ === 0)
        return new Promise((resolve) => {
          release = () => resolve(firstClient);
        });
      return {
        request: async () => ({
          text: "second result",
          checkpoint: nextCheckpoint,
        }),
      };
    };
    const first = controller.run("first task");
    while (!release) await new Promise((r) => setTimeout(r, 1));
    await controller.stop();
    await controller.run("second task");
    release();
    await first;
    assert.equal(firstRequests, 0);
    assert.deepEqual(session.checkpoint, nextCheckpoint);
    assert.equal(session.busy, false);
    assert.equal(
      session.messages.filter((m) => m.role === "assistant").length,
      1,
    );
  }));

test("managed runtime requires matching wheel marker and SDK; corrupt setup never launches Python", async () =>
  fixture(async (_tools, dir) => {
    const storage = path.join(dir, "storage");
    const backend = path.join(dir, "backend");
    await fs.mkdir(backend);
    await fs.mkdir(
      path.join(
        storage,
        "backend-0.12.0",
        process.platform === "win32" ? "Scripts" : "bin",
      ),
      { recursive: true },
    );
    const bytes = Buffer.from("test wheel");
    await fs.writeFile(path.join(backend, "backend.whl"), bytes);
    await fs.writeFile(
      path.join(backend, "manifest.json"),
      JSON.stringify({ file: "backend.whl", sha256: sha(bytes) }),
    );
    const controller = new Controller({
      subscriptions: [],
      extensionPath: dir,
      globalStorageUri: uri(storage),
    });
    const runtime = controller.runtime;
    await fs.writeFile(runtime.python, "not executed");
    await fs.writeFile(
      path.join(storage, "backend-0.12.0/ready.json"),
      JSON.stringify({ sha256: sha(bytes), sdk: false }),
    );
    assert.equal(await runtime.ready("demo"), runtime.python);
    await assert.rejects(runtime.ready("openai"), /Set Up/);
    await fs.writeFile(
      path.join(storage, "backend-0.12.0/ready.json"),
      JSON.stringify({ sha256: "mismatch", sdk: true }),
    );
    await assert.rejects(runtime.ready("demo"), /Set Up/);
    await fs.writeFile(path.join(backend, "backend.whl"), "corrupted");
    let launches = 0;
    runtime.command = async () => {
      launches++;
    };
    const oldInput = vscode.window.showInputBox;
    vscode.window.showInputBox = async () => "python";
    replies.push("Set up backend");
    try {
      await assert.rejects(runtime.setup(root.uri), /integrity/);
      assert.equal(launches, 0);
      vscode.workspace.isTrusted = false;
      await assert.rejects(runtime.setup(root.uri), /Trust/);
      assert.equal(launches, 0);
    } finally {
      vscode.window.showInputBox = oldInput;
    }
  }));

test("usage refresh works while busy without provider setup, overlays worker memory, and rejects stale reports", async () =>
  fixture(async () => {
    const controller = new Controller({ subscriptions: [] });
    const session = controller.session(root);
    controller.selected = session;
    session.busy = true;
    const posted = [];
    const requests = [];
    controller.view = { webview: { postMessage: value => posted.push(value) } };
    controller.usageClient = { running: true, request: async (method, params) => {
      requests.push([method, params]);
      return { totals: { calls: 1 }, groups: [], storage_incomplete: false };
    } };
    session.client = { running: true, request: async method => {
      assert.equal(method, "usage/pending");
      return [{ request_id: "volatile-call" }];
    } };
    await controller.action({ type: "usage", options: { period: "all", kind: "completion" } });
    assert.equal(posted.at(-1).type, "usage");
    assert.equal(requests[0][1].sessionId, session.usageId);
    assert.equal(requests[0][1].volatileCalls[0].request_id, "volatile-call");
    assert.equal(session.busy, true);
    session.client = undefined;
    let release;
    controller.usageClient.request = async () => new Promise(resolve => { release = resolve; });
    const stale = controller.showUsage({ period: "session" });
    while (!release) await new Promise(resolve => setTimeout(resolve, 1));
    session.usageId = require("node:crypto").randomUUID();
    release({ totals: {}, groups: [] });
    await stale;
    assert.equal(posted.filter(value => value.type === "usage").length, 1);
    await assert.rejects(controller.showUsage({ invalidFilter: "x" }), /Invalid usage/);
  }));

test("agentignore denial and gitignore discovery match the Python golden cases", async () =>
  fixture(async (tools, directory) => {
    const { FilePolicy } = require("../dist/filePolicy.cjs");
    const fixtures = JSON.parse(await fs.readFile(path.join(__dirname, "../../tests/fixtures/ignore-policy.json"), "utf8"));
    for (const [name, text] of Object.entries(fixtures.rules)) {
      await fs.mkdir(path.dirname(path.join(directory, name)), { recursive: true });
      await fs.writeFile(path.join(directory, name), text);
    }
    const policy = new FilePolicy(directory);
    for (const item of fixtures.cases) {
      await fs.mkdir(path.dirname(path.join(directory, item.path)), { recursive: true });
      await fs.writeFile(path.join(directory, item.path), "sample content");
      assert.equal(await policy.denied(item.path), item.denied, item.path);
      assert.equal(await policy.ignored(item.path), item.ignored, item.path);
      if (item.denied) await assert.rejects(tools.guarded(item.path));
      else assert(await tools.guarded(item.path));
    }
    const listing = await tools.execute("list_files", { path: "." }, "plan", () => true);
    assert.equal(listing.ok, true);
    assert(!listing.content.includes("trace.log"));
    assert(!listing.content.includes("private"));
    assert.equal((await tools.execute("read_file", { path: "trace.log" }, "plan", () => true)).ok, true);
    await fs.writeFile(path.join(directory, ".agentignore"), Buffer.from([0xff]));
    await assert.rejects(tools.guarded("main.py"));
  }));

test("backend updates stage a separate environment and preserve the last working runtime on failure", async () =>
  fixture(async (_tools, directory) => {
    const storage = path.join(directory, "storage"), backend = path.join(directory, "backend");
    await fs.mkdir(backend);
    const bytes = Buffer.from("wheel");
    await fs.writeFile(path.join(backend, "backend.whl"), bytes);
    const locks = {};
    for (const file of ["runtime-base.txt", "runtime-openai.txt", "dependencies.json"]) {
      await fs.writeFile(path.join(backend, file), "locked"); locks[file] = sha("locked");
    }
    await fs.writeFile(path.join(backend, "manifest.json"), JSON.stringify({ file: "backend.whl", sha256: sha(bytes), locks }));
    const controller = new Controller({ subscriptions: [], extensionPath: directory, globalStorageUri: uri(storage) });
    const runtime = controller.runtime;
    const oldPython = runtime.python;
    await fs.mkdir(path.dirname(oldPython), { recursive: true });
    await fs.writeFile(oldPython, "old interpreter");
    await fs.writeFile(path.join(storage, "backend-0.12.0/ready.json"), JSON.stringify({ sha256: sha(bytes), sdk: false }));
    let fail = true;
    runtime.command = async (_binary, args) => {
      if (args.includes("venv")) {
        const target = args.at(-1);
        const binary = path.join(target, process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
        await fs.mkdir(path.dirname(binary), { recursive: true });
        await fs.writeFile(binary, "candidate");
      }
      if (fail && args.includes("--require-hashes")) throw Error("installation interrupted");
    };
    const previousInput = vscode.window.showInputBox;
    vscode.window.showInputBox = async () => "python";
    vscode.window.withProgress = async (_options, action) => action();
    vscode.window.showInformationMessage = async () => {};
    try {
      replies.push("Set up backend");
      await assert.rejects(runtime.setup(root.uri), /interrupted/);
      assert.equal(await runtime.ready("demo"), oldPython);
      assert.equal(await fs.readFile(oldPython, "utf8"), "old interpreter");
      assert(!await fs.stat(path.join(storage, "backend-setup.lock")).catch(() => false));
      fail = false; replies.push("Set up backend");
      await runtime.setup(root.uri);
      assert.notEqual(runtime.python, oldPython);
      assert.equal(await runtime.ready("demo"), runtime.python);
      assert.equal(await fs.readFile(oldPython, "utf8"), "old interpreter");
    } finally { vscode.window.showInputBox = previousInput; }
  }));
