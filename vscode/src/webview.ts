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
window.addEventListener("message", (event) => {
  const data = event.data;
  if (!data || typeof data !== "object") return;
  if (data.type === "progress") {
    element("progress").textContent = String(data.text);
    return;
  }
  if (data.type !== "state") return;
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
    title.textContent = `${check.profile}: ${check.result.passed ? "passed" : check.result.not_run ? "not run" : "failed"}`;
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
