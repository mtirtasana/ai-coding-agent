declare function acquireVsCodeApi(): { postMessage(message: unknown): void };
const api = acquireVsCodeApi();
const element = (id: string) => document.getElementById(id)!;
const button = (label: string, action: () => void) => {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", action);
  return b;
};
for (const b of document.querySelectorAll<HTMLButtonElement>("[data-action]"))
  b.addEventListener("click", () =>
    api.postMessage({ type: b.dataset.action }),
  );
element("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const task = element("task") as HTMLTextAreaElement;
  if (task.value.trim()) {
    api.postMessage({ type: "send", text: task.value });
    task.value = "";
  }
});
element("folder").addEventListener("change", () =>
  api.postMessage({
    type: "folder",
    uri: (element("folder") as HTMLSelectElement).value,
  }),
);
let usageSession = "";
element("usage-refresh")?.addEventListener("click", () => {
  const options: Record<string, string> = {};
  for (const key of ["period", "start", "end", "provider", "profile", "model", "interface", "kind"]) {
    const value = (element(`usage-${key}`) as HTMLInputElement | HTMLSelectElement).value;
    if (value && (!["start", "end"].includes(key) || options.period === "custom")) options[key] = value;
  }
  api.postMessage({ type: "usage", options });
});
const usageTokens = (group: { tokens: Record<string, { reported_sum: number | null; responses_with_value: number }>; responses: number }) =>
  ["input_tokens", "output_tokens", "cached_input_tokens"].map(key => {
    const count = group.tokens[key];
    return `${key}: ${count.reported_sum ?? "unknown"} (${count.responses_with_value}/${group.responses} responses)`;
  }).join(" · ");
const showCompletionStats = (stats: { requested: number; reused: number; discarded: number } | undefined) => {
  const container = element("completion-stats");
  if (container && stats) container.textContent =
    `Inline this workspace: ${stats.requested} requests · ${stats.reused} reused · ${stats.discarded} discarded/cancelled results`;
};
window.addEventListener("message", (event) => {
  const data = event.data;
  if (!data || typeof data !== "object") return;
  if (data.type === "progress") {
    element("progress").textContent = String(data.text);
    return;
  }
  if (data.type === "completionStats") { showCompletionStats(data.stats); return; }
  if (data.type === "usage") {
    const container = element("usage-report");
    if (!container) return;
    const report = data.report;
    container.replaceChildren();
    const title = document.createElement("p");
    title.textContent = `Recorded usage · ${report.period} · host timezone: ${report.timezone}`;
    const totals = document.createElement("p");
    totals.textContent = report.totals.calls ? `Model calls: ${report.totals.calls} · responses: ${report.totals.responses} · unfinished: ${report.totals.unfinished} · failed: ${report.totals.failed} · interrupted: ${report.totals.interrupted}\n${usageTokens(report.totals)}` : "No usage recorded for this selection.";
    container.append(title, totals);
    if (report.start) {
      const range = document.createElement("p");
      range.textContent = `UTC boundaries: ${report.start} ≤ call start < ${report.end_exclusive}`;
      container.append(range);
    }
    if (report.storage_incomplete) {
      const warning = document.createElement("p");
      warning.textContent = "History persistence is incomplete; these totals include available current-process metadata.";
      container.append(warning);
    }
    for (const group of report.groups) {
      const row = document.createElement("p");
      row.textContent = `${group.provider} / ${group.profile} / ${group.model} / ${group.interface} / ${group.kind} · calls: ${group.calls}\n${usageTokens(group)}`;
      container.append(row);
    }
    return;
  }
  if (data.type !== "state") return;
  showCompletionStats(data.completionStats);
  if (data.usageSession !== usageSession) {
    usageSession = data.usageSession;
    const report = element("usage-report");
    if (report) report.textContent = "Conversation changed. Refresh usage to view its recorded totals.";
  }
  element("status").textContent =
    `${data.trusted ? "Trusted" : "Restricted"} · ${data.mode} · ${data.provider} ${data.model || ""}${data.busy ? " · Working" : ""}`;
  const selector = element("folder") as HTMLSelectElement;
  selector.replaceChildren();
  for (const folder of data.folders) {
    const option = document.createElement("option");
    option.value = folder.uri;
    option.textContent = folder.name;
    selector.append(option);
  }
  selector.value = data.folder || "";
  (element("send") as HTMLButtonElement).disabled =
    !!data.busy || !data.trusted;
  element("attachments").textContent = data.attachmentCount
    ? "Selection attached (sent with next task)"
    : "";
  element("messages").replaceChildren();
  for (const message of data.messages) {
    const item = document.createElement("article");
    const title = document.createElement("strong");
    title.textContent = message.role;
    const text = document.createElement("pre");
    text.textContent = message.text;
    item.append(title, text);
    element("messages").append(item);
  }
  element("proposals").replaceChildren();
  for (const proposal of data.proposals) {
    const item = document.createElement("article");
    const title = document.createElement("p");
    title.textContent = proposal.path;
    item.append(
      title,
      button("Accept", () =>
        api.postMessage({ type: "decision", id: proposal.id, accept: true }),
      ),
      button("Reject", () =>
        api.postMessage({ type: "decision", id: proposal.id, accept: false }),
      ),
    );
    element("proposals").append(item);
  }
  element("checks").replaceChildren();
  for (const check of data.checks) {
    const item = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = `${check.profile}: ${String(check.result.status || (check.result.passed ? "passed" : check.result.not_run ? "not run" : "failed")).replaceAll("_", " ")}`;
    const body = document.createElement("pre");
    body.textContent = JSON.stringify(check.result, null, 2);
    item.append(title, body);
    element("checks").append(item);
  }
  element("diagnostics").replaceChildren();
  for (const diagnostic of data.diagnostics) {
    const item = document.createElement("article");
    const text = document.createElement("p");
    text.textContent = `${diagnostic.file}:${diagnostic.line} · ${diagnostic.severity} · ${diagnostic.source}\n${diagnostic.message}`;
    item.append(
      text,
      button("Explain", () =>
        api.postMessage({ type: "explain", id: diagnostic.id }),
      ),
      button("Propose fix", () =>
        api.postMessage({ type: "fix", id: diagnostic.id }),
      ),
    );
    element("diagnostics").append(item);
  }
});
api.postMessage({ type: "ready" });
