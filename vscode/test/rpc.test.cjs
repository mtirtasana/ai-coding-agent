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
      const init = await client.request("initialize", {
        protocolVersion: 1,
        workspace: root,
        provider: "demo",
      });
      assert.equal(init.protocolVersion, 1);
      const result = await client.request("session/run", {
        task: "Inspect",
        mode: "plan",
      });
      assert.match(result.text, /sample.py/);
      assert(result.checkpoint.messages.length > 0);
      assert(seen.includes("editor/tool"));
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
      const cancelled = await client.request("cancel");
      assert.equal(cancelled.cleanupComplete, true);
      await caught;
      waiting("");
    } finally {
      client.dispose();
      fs.rmSync(root, { recursive: true, force: true });
    }
  },
);
