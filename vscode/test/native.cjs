const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const crypto = require("node:crypto");
const hash = (s) => crypto.createHash("sha256").update(s).digest("hex");
exports.run = async () => {
  const ext = vscode.extensions.getExtension("mtirtasana.generic-coding-agent");
  assert(ext, "Extension is installed");
  const { controller } = await ext.activate();
  assert(controller);
  const folder = vscode.workspace.workspaceFolders[0];
  const session = controller.session(folder);
  controller.selected = session;
  const tools = session.tools;
  const file = path.join(folder.uri.fsPath, "review.py");
  await fs.writeFile(file, "value = 1\n");
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  let editor = await vscode.window.showTextDocument(doc);
  await editor.edit((builder) =>
    builder.insert(new vscode.Position(0, 0), "# unsaved\n"),
  );
  const snapshot = JSON.parse(
    (
      await tools.execute(
        "file_snapshot",
        { path: "review.py" },
        "plan",
        () => true,
      )
    ).content,
  );
  assert(snapshot.content.startsWith("# unsaved"));
  assert.equal(snapshot.sha256, hash(doc.getText()));
  const rangeReply = await tools.execute(
    "read_file_range",
    { path: "./review.py", start_line: 2, end_line: 2 },
    "plan",
    () => true,
  );
  assert.equal(rangeReply.ok, true, rangeReply.content);
  const range = JSON.parse(rangeReply.content);
  assert.equal(range.path, "review.py");
  assert.equal(range.content, "value = 1\n");
  assert.equal(range.total_lines, 2);
  assert.equal(range.sha256, snapshot.sha256);
  const denied = await tools.execute(
    "apply_edit",
    {
      path: "review.py",
      expected_sha256: snapshot.sha256,
      old_text: "value = 1",
      new_text: "value = 2",
    },
    "plan",
    () => true,
  );
  assert.equal(denied.ok, false);
  const pending = tools.execute(
    "apply_edit",
    {
      path: "review.py",
      expected_sha256: range.sha256,
      old_text: "value = 1",
      new_text: "value = 2",
    },
    "implement",
    () => true,
  );
  for (let i = 0; i < 200 && !tools.proposals.size; i++)
    await new Promise((r) => setTimeout(r, 10));
  assert.equal(tools.proposals.size, 1);
  assert(doc.getText().includes("value = 1"));
  await tools.decide([...tools.proposals.keys()][0], true);
  const applied = await pending;
  assert.equal(applied.ok, true, applied.content);
  assert(doc.getText().includes("value = 2"));
  assert(doc.isDirty);
  assert.equal(await fs.readFile(file, "utf8"), "value = 1\n");
  editor = await vscode.window.showTextDocument(doc);
  await vscode.commands.executeCommand("undo");
  assert(doc.getText().includes("value = 1"));
  const second = tools.execute(
    "write_file",
    { path: "review.py", content: "do not write\n" },
    "implement",
    () => true,
  );
  for (let i = 0; i < 200 && !tools.proposals.size; i++)
    await new Promise((r) => setTimeout(r, 10));
  tools.cancel();
  assert.equal((await second).ok, false);
  assert(!doc.getText().includes("do not write"));
  const collection =
    vscode.languages.createDiagnosticCollection("agent-native-test");
  collection.set(doc.uri, [
    new vscode.Diagnostic(
      new vscode.Range(0, 0, 0, 1),
      "Example diagnostic",
      vscode.DiagnosticSeverity.Warning,
    ),
  ]);
  const items = controller.diagnosticItems();
  assert(items.some((d) => d.message === "Example diagnostic"));
  collection.dispose();
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
  const crlfFile = path.join(folder.uri.fsPath, "crlf.py");
  await fs.writeFile(crlfFile, "value = 1\r\n");
  const crlfDoc = await vscode.workspace.openTextDocument(
    vscode.Uri.file(crlfFile),
  );
  const crlfSnapshot = JSON.parse(
    (
      await tools.execute(
        "read_file_range",
        { path: "crlf.py", start_line: 1, end_line: 1 },
        "plan",
        () => true,
      )
    ).content,
  );
  assert.equal(crlfSnapshot.content, crlfDoc.getText());
  const normalized = tools.execute(
    "apply_edit",
    {
      path: "crlf.py",
      expected_sha256: crlfSnapshot.sha256,
      old_text: "value = 1",
      new_text: "value = 2\nextra = 3",
    },
    "implement",
    () => true,
  );
  for (let i = 0; i < 200 && !tools.proposals.size; i++)
    await new Promise((r) => setTimeout(r, 10));
  assert.equal(tools.proposals.size, 1);
  await tools.decide([...tools.proposals.keys()][0], true);
  const normalizedResult = await normalized;
  assert.equal(normalizedResult.ok, true, normalizedResult.content);
  assert.equal(
    JSON.parse(normalizedResult.content).sha256,
    hash(crlfDoc.getText()),
    "Edit result must hash the actual buffer after native EOL normalization",
  );
  await vscode.window.showTextDocument(crlfDoc);
  await vscode.commands.executeCommand("workbench.action.files.revert");
  console.log(
    "Native VS Code tests passed: activation, dirty snapshots and range reads, planning denial, reviewed apply, unsaved buffers, Undo, rejection, diagnostics and path guards.",
  );
  await vscode.window.showTextDocument(doc);
  await vscode.commands.executeCommand("workbench.action.files.revert");
  await vscode.commands.executeCommand("workbench.action.closeAllEditors");
  const commands = await vscode.commands.getCommands(true);
  assert(commands.includes("genericAgent.usage"));
  const oldState = process.env.AGENT_STATE_DIR;
  process.env.AGENT_STATE_DIR = path.join(folder.uri.fsPath, "isolated-usage");
  const ready = controller.runtime.ready;
  await fs.mkdir(controller.context.globalStorageUri.fsPath, { recursive: true });
  controller.runtime.ready = async () => process.env.AGENT_TEST_PYTHON || (process.platform === "win32" ? "python" : "python3");
  try {
    session.busy = true;
    await controller.showUsage({ period: "all" });
    assert(controller.usageClient.running);
    session.busy = false;
    const oldSession = session.usageId;
    await controller.action({ type: "new" });
    assert.notEqual(session.usageId, oldSession);
    assert.equal(session.mode, "plan");
    console.log("Native usage tests passed: command registration, credential-free report worker, query while busy and New conversation identity.");
  } finally {
    session.busy = false;
    controller.usageClient?.dispose();
    controller.runtime.ready = ready;
    if (oldState === undefined) delete process.env.AGENT_STATE_DIR;
    else process.env.AGENT_STATE_DIR = oldState;
  }
};
