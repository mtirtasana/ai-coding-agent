const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
test("webview renders model, diagnostic and check text without executing HTML", async () => {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM(
    '<div id="status"></div><select id="folder"></select><button id="send"></button><form id="form"><textarea id="task"></textarea></form><div id="attachments"></div><div id="messages"></div><div id="proposals"></div><div id="checks"></div><div id="diagnostics"></div><div id="progress"></div>',
    { runScripts: "outside-only" },
  );
  const sent = [];
  dom.window.acquireVsCodeApi = () => ({
    postMessage: (value) => sent.push(value),
  });
  dom.window.eval(
    fs.readFileSync(path.join(__dirname, "../dist/webview.js"), "utf8"),
  );
  const attack =
    '<img src=x onerror="window.compromised=true"><script>window.compromised=true</script>';
  dom.window.dispatchEvent(
    new dom.window.MessageEvent("message", {
      data: {
        type: "state",
        trusted: true,
        mode: "plan",
        provider: "demo",
        folders: [],
        messages: [{ role: "assistant", text: attack }],
        proposals: [],
        checks: [
          { profile: "test", result: { passed: false, output: attack } },
        ],
        diagnostics: [
          {
            id: "x",
            file: "a.py",
            line: 1,
            severity: "Warning",
            source: "tool",
            message: attack,
          },
        ],
        attachmentCount: 0,
      },
    }),
  );
  assert.equal(dom.window.compromised, undefined);
  assert.equal(dom.window.document.querySelectorAll("img,script").length, 0);
  assert(dom.window.document.body.textContent.includes(attack));
  assert.equal(sent[0].type, "ready");
  dom.window.close();
});
