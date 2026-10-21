const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
test("webview renders model, diagnostic and check text without executing HTML", async () => {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM(
    '<div id="status"></div><select id="folder"></select><button id="send"></button><form id="form"><textarea id="task"></textarea></form><div id="attachments"></div><div id="messages"></div><div id="proposals"></div><div id="checks"></div><div id="diagnostics"></div><div id="progress"></div><select id="usage-period"><option value="today">Today</option><option value="custom">Custom</option></select><input id="usage-start"><input id="usage-end"><input id="usage-provider"><input id="usage-profile"><input id="usage-model"><input id="usage-interface"><input id="usage-kind"><button id="usage-refresh"></button><div id="usage-report"></div>',
    { runScripts: "outside-only" },
  );
  const sent = [];
  const completionStats = dom.window.document.createElement('p');
  completionStats.id = 'completion-stats';
  dom.window.document.body.append(completionStats);
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
        completionStats: { requested: 3, reused: 2, discarded: 1 },
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
  assert.match(completionStats.textContent, /3 requests · 2 reused · 1 discarded/);
  const document = dom.window.document;
  document.getElementById("usage-start").value = "2026-01-01";
  document.getElementById("usage-end").value = "2026-01-31";
  document.getElementById("usage-kind").value = "completion";
  document.getElementById("usage-refresh").click();
  assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))), { type: "usage", options: { period: "today", kind: "completion" } });
  document.getElementById("usage-period").value = "custom";
  document.getElementById("usage-refresh").click();
  assert.equal(sent.at(-1).options.start, "2026-01-01");
  const group = { calls: 2, responses: 2, unfinished: 0, failed: 0, interrupted: 0,
    tokens: { input_tokens: { reported_sum: 0, responses_with_value: 1 },
      output_tokens: { reported_sum: null, responses_with_value: 0 },
      cached_input_tokens: { reported_sum: null, responses_with_value: 0 } } };
  dom.window.dispatchEvent(new dom.window.MessageEvent("message", { data: { type: "usage", report: {
    period: "today", timezone: "UTC", storage_incomplete: true, totals: group,
    groups: [{ ...group, provider: attack, profile: "editor", model: "local", interface: "vscode", kind: "completion" }],
  } } }));
  assert.match(document.getElementById("usage-report").textContent, /input_tokens: 0 \(1\/2 responses\)/);
  assert.match(document.getElementById("usage-report").textContent, /output_tokens: unknown/);
  assert.match(document.getElementById("usage-report").textContent, /incomplete/);
  assert.equal(document.querySelectorAll("img,script").length, 0);
  dom.window.close();
});
