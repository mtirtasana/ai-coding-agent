const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { RpcClient } = require("../dist/rpc.cjs");
const python =
  process.env.AGENT_TEST_PYTHON ||
  (process.platform === "win32" ? "python" : "python3");
test(
  "real Python JSON-RPC demo, callbacks, cancellation and redacted errors",
  { timeout: 15000 },
  async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-rpc-"));
    const priorState = process.env.AGENT_STATE_DIR;
    process.env.AGENT_STATE_DIR = path.join(root, "state");
    let hold = false;
    let waiting;
    const seen = [];
    const client = new RpcClient(
      python,
      os.tmpdir(),
      async (method, params) => {
        seen.push(method);
        if (method === "editor/instructions") {
          if (hold)
            return new Promise((resolve) => {
              waiting = resolve;
            });
          return "";
        }
        if (method === "editor/tool") return { ok: true, content: "sample.py" };
        throw Error("Unexpected callback");
      },
      () => {},
    );
    try {
      const empty = await client.request("usage/query", { period: "all" });
      assert.equal(empty.totals.calls, 0);
      assert.equal(fs.existsSync(path.join(root, "state")), false);
      const sessionId = require("node:crypto").randomUUID();
      const init = await client.request("initialize", {
        protocolVersion: 2,
        workspace: root,
        provider: "demo",
        sessionId,
      });
      assert.equal(init.protocolVersion, 2);
      const result = await client.request("session/run", {
        task: "Inspect",
        mode: "plan",
      });
      assert.match(result.text, /sample.py/);
      assert(result.checkpoint.messages.length > 0);
      assert(seen.includes("editor/tool"));
      const usage = await client.request("usage/query", { period: "session", sessionId });
      assert(usage.totals.calls > 0);
      assert.equal(usage.groups[0].interface, "vscode");
      assert.equal(usage.totals.tokens.input_tokens.reported_sum, null);
      await assert.rejects(
        client.request("initialize", { protocolVersion: 99 }),
        /Invalid request/,
      );
      hold = true;
      const run = client.request("session/run", { task: "Wait", mode: "plan" });
      const caught = assert.rejects(run, /cancelled/);
      for (let i = 0; i < 200 && !waiting; i++)
        await new Promise((r) => setTimeout(r, 5));
      assert(waiting);
      const busyUsage = await client.request("usage/query", { period: "session", sessionId });
      assert.equal(busyUsage.totals.calls, usage.totals.calls);
      const cancelled = await client.request("cancel");
      assert.equal(cancelled.cleanupComplete, true);
      await caught;
      waiting("");
    } finally {
      client.dispose();
      if (priorState === undefined) delete process.env.AGENT_STATE_DIR;
      else process.env.AGENT_STATE_DIR = priorState;
      fs.rmSync(root, { recursive: true, force: true });
    }
  },
);

test("an expired RPC deadline rejects and disposes the backend without replay", { timeout: 5000 }, async () => {
  const client = new RpcClient(python, os.tmpdir(), async () => {}, () => {});
  try {
    await assert.rejects(client.request("usage/query", { period: "all" }, 1), /timed out/);
    assert.equal(client.running, false);
    await assert.rejects(client.request("usage/query", { period: "all" }), /not running/);
  } finally { client.dispose(); }
});
