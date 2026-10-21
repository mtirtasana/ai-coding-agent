import * as vscode from "vscode";
import * as path from "node:path";
import * as fs from "node:fs/promises";
import { randomBytes, randomUUID } from "node:crypto";
import { EditorTools } from "./editor";
import { Runtime } from "./runtime";
import { RpcClient, RecordValue } from "./rpc";
import {
  completionContext,
  CompletionCache,
  providerKey,
  relativePath,
  sameStamp,
  sha,
  validateChecks,
} from "./shared";

const record = (value: unknown): RecordValue => {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw Error("Invalid backend response.");
  return value as RecordValue;
};
interface Session {
  usageId: string;
  revision: number;
  checkRevisions: Map<string, number>;
  connectionSignature?: string;
  folder: vscode.WorkspaceFolder;
  tools: EditorTools;
  client?: RpcClient;
  completion?: RpcClient;
  completionInitializing?: Promise<RpcClient>;
  checkpoint?: RecordValue;
  instructions?: string;
  mode: "plan" | "implement";
  busy: boolean;
  cancelled: boolean;
  cancelling: boolean;
  generation: number;
  stopTask?: Promise<void>;
  permissions: Set<string>;
  messages: { role: string; text: string }[];
  checks: { profile: string; result: unknown }[];
  completionCache: CompletionCache;
}
export class Controller
  implements vscode.WebviewViewProvider, vscode.Disposable
{
  private view?: vscode.WebviewView;
  private sessions = new Map<string, Session>();
  private selected?: Session;
  private diagnostics = new Map<
    string,
    { uri: vscode.Uri; diagnostic: vscode.Diagnostic }
  >();
  private runtime: Runtime;
  private manualInline = false;
  private attachments: string[] = [];
  private usageClient?: RpcClient;
  private usageGeneration = 0;
  private usageStorageIncomplete = false;
  constructor(private context: vscode.ExtensionContext) {
    this.runtime = new Runtime(context);
  }
  private trusted() {
    if (!vscode.workspace.isTrusted)
      throw Error("Trust the workspace before using the agent.");
  }
  private session(folder?: vscode.WorkspaceFolder) {
    folder =
      folder || this.selected?.folder || vscode.workspace.workspaceFolders?.[0];
    if (!folder) throw Error("Open a filesystem workspace folder first.");
    if (
      !vscode.workspace.workspaceFolders?.some(
        (f) => f.uri.toString() === folder!.uri.toString(),
      )
    )
      throw Error("Selected workspace folder was removed.");
    const id = folder.uri.toString();
    let session = this.sessions.get(id);
    if (!session) {
      const tools = new EditorTools(folder, () => this.publish());
      session = {
        usageId: randomUUID(),
        revision: 0,
        checkRevisions: new Map(),
        folder,
        tools,
        mode: "plan",
        busy: false,
        cancelled: false,
        cancelling: false,
        generation: 0,
        permissions: new Set(),
        messages: [],
        checks: [],
        completionCache: new CompletionCache(),
      };
      this.sessions.set(id, session);
    }
    return session;
  }
  private async initialize(session: Session, completion = false) {
    if (!completion) return this.startClient(session, false);
    if (session.completionInitializing) return session.completionInitializing;
    const pending = this.startClient(session, true);
    session.completionInitializing = pending;
    try { return await pending; }
    finally { if (session.completionInitializing === pending) session.completionInitializing = undefined; }
  }
  private async startClient(session: Session, completion = false) {
    this.trusted();
    const generation = session.generation;
    const existing = completion ? session.completion : session.client;
    if (existing?.running) return existing;
    const cfg = vscode.workspace.getConfiguration(
      "genericAgent",
      session.folder.uri,
    );
    session.connectionSignature ??= JSON.stringify([cfg.get("provider", "demo"), cfg.get("model", ""), cfg.get("baseUrl", "")]);
    const provider = cfg.get<string>("provider") || "demo";
    if (
      provider !== "demo" &&
      provider !== "openai" &&
      !cfg.get<string>("model")
    )
      throw Error("Configure a model name first.");
    if (session.instructions === undefined)
      session.instructions = await this.loadInstructions(session);
    const python = await this.runtime.ready(provider);
    if (session.generation !== generation) throw Error("Operation cancelled.");
    const client = new RpcClient(
      python,
      this.context.globalStorageUri.fsPath,
      async (method, params) => {
        const current = () =>
          session.client === client &&
          session.busy &&
          !session.cancelled &&
          vscode.workspace.isTrusted &&
          !!vscode.workspace.workspaceFolders?.some(
            (f) => f.uri.toString() === session.folder.uri.toString(),
          );
        if (completion || !current()) throw Error("Stale editor request.");
        return this.bridge(session, method, params, current);
      },
      (params) => {
        if (!completion && session.client === client && params.kind === "verification") {
          const data = record(params.data);
          if (Array.isArray(data.results)) {
            for (const value of data.results) {
              const item = record(value);
              if (item.status === "passed" && session.checkRevisions.get(String(item.profile)) !== session.revision) item.status = "stale";
              session.checks = session.checks.filter(check => check.profile !== item.profile);
              session.checks.push({ profile: String(item.profile), result: { status: item.status, passed: item.status === "passed" } });
            }
            this.publish();
          }
          return;
        }
        if (params.kind === "usage_warning" || params.kind === "recovery_warning") {
          if (params.kind === "usage_warning") this.usageStorageIncomplete = true;
          const data = record(params.data);
          if (typeof data.message === "string")
            void this.view?.webview.postMessage({ type: "progress", text: data.message });
          return;
        }
        if (!completion && session.client === client && !session.cancelled) {
          const kind =
            typeof params.kind === "string" ? params.kind : "progress";
          void this.view?.webview.postMessage({ type: "progress", text: kind });
        }
      },
      () => {
        if (completion) {
          if (session.completion === client) session.completion = undefined;
        } else if (session.client === client) {
          session.client = undefined;
          this.publish();
        }
      },
    );
    if (completion) session.completion = client;
    else session.client = client;
    try {
      const baseUrl = cfg.get<string>("baseUrl") || "";
      const keyName = providerKey(provider, baseUrl);
      const key = await this.context.secrets.get(keyName);
      if (session.generation !== generation)
        throw Error("Operation cancelled.");
      const model =
        (completion ? cfg.get<string>("inline.model") : undefined) ||
        cfg.get<string>("model") ||
        "";
      const config: RecordValue = {
        sessionId: session.usageId,
        protocolVersion: 2,
        workspace: session.folder.uri.fsPath,
        provider,
        model,
        baseUrl,
        checks: completion ? {} : validateChecks(cfg.get("checks", {})),
        maxSteps: cfg.get("maxSteps", 15),
        maxContextChars: cfg.get("maxContextChars", 200000),
        contextMode: cfg.get("contextMode", "recent"),
        requestTimeout: cfg.get("requestTimeout", 120),
        maxOutputTokens: cfg.get("maxOutputTokens", 4096),
        persistSession: !completion && cfg.get("persistSession", false),
      };
      for (const setting of ["maxContextTokens", "maxRunTokens"]) {
        const value = cfg.get<number>(setting, 0);
        if (value) config[setting] = value;
      }
      if (["openai", "openai-compatible"].includes(provider) && !key)
        throw Error("Use Configure Provider to set an API key.");
      if (key) config.apiKey = key;
      if (!completion && session.checkpoint)
        config.checkpoint = session.checkpoint;
      const reply = record(await client.request("initialize", config));
      if (session.generation !== generation)
        throw Error("Operation cancelled.");
      if (reply.protocolVersion !== 2 || reply.backendVersion !== "0.12.0")
        throw Error("Incompatible backend. Run backend setup again.");
      return client;
    } catch (e) {
      client.dispose();
      throw e;
    }
  }
  private async loadInstructions(session: Session) {
    try {
      return (await session.tools.read("AGENTS.md")).text;
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") {
        const fs = await import("node:fs/promises");
        try {
          if (
            (
              await fs.lstat(path.join(session.folder.uri.fsPath, "AGENTS.md"))
            ).isSymbolicLink()
          )
            throw Error("Broken AGENTS.md symlink.");
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
        return "";
      }
      throw e;
    }
  }
  private async bridge(
    session: Session,
    method: string,
    params: RecordValue,
    current: () => boolean,
  ) {
    if (method === "editor/tool") {
      if (typeof params.name !== "string") throw Error("Invalid tool name.");
      const args = record(params.arguments);
      return session.tools.execute(params.name, args, session.mode, current);
    }
    if (method === "editor/instructions") return session.instructions || "";
    if (method === "editor/beforeCheck") {
      if (session.mode !== "implement") return false;
      const profile = String(params.profile);
      const checks = validateChecks(
        vscode.workspace
          .getConfiguration("genericAgent", session.folder.uri)
          .get("checks", {}),
      );
      const check = checks[profile];
      if (!check) return false;
      const fingerprint = sha(JSON.stringify({ profile, check }));
      if (!session.permissions.has(fingerprint)) {
        const answer = await vscode.window.showWarningMessage(
          `Allow ${profile} for this session on the workspace host? ${JSON.stringify(check.command)}`,
          "Allow check",
          "Skip",
        );
        if (answer !== "Allow check" || !current()) return false;
        session.permissions.add(fingerprint);
      }
      const dirty = vscode.workspace.textDocuments.filter(
        (d) =>
          d.isDirty &&
          vscode.workspace.getWorkspaceFolder(d.uri)?.uri.toString() ===
            session.folder.uri.toString(),
      );
      if (dirty.length) {
        const answer = await vscode.window.showWarningMessage(
          `Save ${dirty.length} unsaved workspace document(s) before running ${profile}?`,
          "Save and run",
          "Skip",
        );
        if (answer !== "Save and run" || !current()) return false;
        for (const doc of dirty) {
          if (!current() || !(await doc.save())) return false;
        }
      }
      if (current()) {
        session.checkRevisions?.set(profile, session.revision);
        return true;
      }
      return false;
    }
    if (method === "editor/checkResult") {
      const checked = record(params.result);
      if (checked.passed && session.checkRevisions?.get(String(params.profile)) !== session.revision)
        Object.assign(checked, { passed: false, status: "stale" });
      session.checks.push({
        profile: String(params.profile),
        result: checked,
      });
      session.checks = session.checks.slice(-30);
      this.publish();
      return true;
    }
    throw Error("Unknown editor callback.");
  }
  async resolveWebviewView(view: vscode.WebviewView) {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.context.extensionUri, "dist"),
        vscode.Uri.joinPath(this.context.extensionUri, "media"),
      ],
    };
    const nonce = randomBytes(24).toString("hex");
    const js = view.webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, "dist/webview.js"),
    );
    const css = view.webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, "media/style.css"),
    );
    view.webview.html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${css}"></head><body><header><h2>Coding Agent</h2><select id="folder" aria-label="Workspace folder"></select><p id="status"></p></header><nav><button data-action="setup">Set up backend</button><button data-action="configure">Provider</button><button data-action="plan">Plan</button><button data-action="acceptPlan">Accept plan</button><button data-action="new">New</button><button data-action="inline">Automatic completions</button></nav><main><section id="messages" aria-live="polite"></section><section><h3>Changes to review</h3><div id="proposals"></div></section><section><h3>Checks</h3><div id="checks"></div></section><section aria-label="Recorded token usage"><h3>Token usage</h3><label>Period <select id="usage-period"><option value="session">This conversation</option><option value="today">Today</option><option value="week">This week</option><option value="month">This month</option><option value="7d">Last 7 days (rolling)</option><option value="30d">Last 30 days (rolling)</option><option value="all">All recorded usage</option><option value="custom">Custom dates</option></select></label><label>From <input id="usage-start" type="date"></label><label>To (inclusive) <input id="usage-end" type="date"></label><label>Interface <select id="usage-interface"><option value="">All</option><option value="cli">CLI</option><option value="vscode">VS Code</option></select></label><label>Category <select id="usage-kind"><option value="">All</option><option value="agent">Agent</option><option value="completion">Completion</option></select></label><label>Provider <input id="usage-provider" maxlength="256"></label><label>Profile <input id="usage-profile" maxlength="256"></label><label>Model <input id="usage-model" maxlength="256"></label><button id="usage-refresh" type="button">Refresh usage</button><p>Filters apply on Refresh. Reports use this execution host’s local timezone.</p><p id="completion-stats"></p><div id="usage-report" aria-live="polite">Refresh to view recorded usage.</div></section><section><h3>Editor diagnostics</h3><div id="diagnostics"></div></section></main><form id="form"><label for="task">Task</label><textarea id="task" rows="4" maxlength="100000"></textarea><p id="attachments"></p><button data-action="attach" type="button">Attach selection</button><button id="send" type="submit">Send</button><button data-action="stop" type="button">Stop</button></form><p id="progress" role="status"></p><script nonce="${nonce}" src="${js}"></script></body></html>`;
    this.context.subscriptions.push(
      view.webview.onDidReceiveMessage((message: unknown) => {
        void this.action(message).catch((e) => this.error(e));
      }),
    );
    this.publish();
  }
  async showUsage(options: RecordValue = {}) {
    this.trusted();
    const generation = ++this.usageGeneration;
    const session = this.session();
    const sessionId = session.usageId;
    if (!this.usageClient?.running) {
      const python = await this.runtime.ready("demo");
      if (generation !== this.usageGeneration) return;
      this.usageClient = new RpcClient(python, this.context.globalStorageUri.fsPath,
        async () => { throw Error("Usage queries have no editor callbacks."); }, () => {});
    }
    const query: RecordValue = { period: "session", ...options, sessionId };
    const allowed = ["period", "start", "end", "provider", "profile", "model", "interface", "kind", "sessionId"];
    if (Object.keys(query).some(key => !allowed.includes(key))) throw Error("Invalid usage query.");
    for (const value of Object.values(query))
      if (typeof value !== "string" || !value || value.length > 256) throw Error("Invalid usage filter.");
    const volatileCalls: unknown[] = [];
    const workers = [...this.sessions.values()].flatMap(item => [item.client, item.completion]);
    await Promise.all(workers.filter(worker => worker?.running).map(async worker => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const pending = await Promise.race([
          worker!.request("usage/pending"),
          new Promise((_, reject) => { timer = setTimeout(() => reject(Error("Usage snapshot timed out.")), 2000); }),
        ]);
        if (!Array.isArray(pending)) throw Error("Invalid usage snapshot.");
        volatileCalls.push(...pending);
      } catch {
        this.usageStorageIncomplete = true;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }));
    let timer: ReturnType<typeof setTimeout> | undefined;
    let result: RecordValue;
    const usageClient = this.usageClient;
    if (volatileCalls.length > 10000) this.usageStorageIncomplete = true;
    try {
      result = record(await Promise.race([
        usageClient.request("usage/query", { ...query, volatileCalls: volatileCalls.slice(0, 10000) }),
        new Promise((_, reject) => { timer = setTimeout(() => { usageClient.dispose(); reject(Error("Usage query timed out. Refresh to retry.")); }, 5000); }),
      ]));
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (this.usageStorageIncomplete) result.storage_incomplete = true;
    if (generation === this.usageGeneration && this.selected?.usageId === sessionId)
      void this.view?.webview.postMessage({ type: "usage", report: result });
  }
  private error(error: unknown) {
    const text =
      error instanceof Error ? error.message : "Agent operation failed.";
    void vscode.window.showErrorMessage(text);
    void this.view?.webview.postMessage({ type: "progress", text });
  }
  async action(raw: unknown) {
    const message = record(raw);
    if (typeof message.type !== "string") return;
    if (message.type === "ready") {
      this.publish();
      return;
    }
    if (message.type === "setup") {
      this.trusted();
      await this.stopAll();
      await this.runtime.setup(this.session().folder.uri);
      return;
    }
    if (message.type === "configure") {
      await this.configure();
      return;
    }
    if (message.type === "usage") {
      await this.showUsage(message.options === undefined ? {} : record(message.options));
      return;
    }
    if (message.type === "folder") {
      if (typeof message.uri !== "string") return;
      const folder = vscode.workspace.workspaceFolders?.find(
        (f) => f.uri.toString() === message.uri,
      );
      if (!folder) return;
      await this.stop();
      this.selected = this.session(folder);
      this.attachments = [];
      this.publish();
      return;
    }
    if (message.type === "stop") {
      await this.stop();
      return;
    }
    if (message.type === "inline") {
      await this.toggleInline();
      return;
    }
    this.trusted();
    const session = this.session();
    this.selected = session;
    if (message.type === "decision") {
      if (typeof message.id === "string" && typeof message.accept === "boolean")
        await session.tools.decide(message.id, message.accept);
      return;
    }
    if (message.type === "explain" || message.type === "fix") {
      if (typeof message.id === "string")
        await this.diagnostic(message.id, message.type === "fix");
      return;
    }
    if (session.busy)
      throw Error("Stop the current run before starting another action.");
    if (message.type === "plan") {
      session.mode = "plan";
      this.publish();
      return;
    }
    if (message.type === "acceptPlan") {
      const generation = ++session.generation;
      session.busy = true;
      this.publish();
      try {
        const client = await this.initialize(session);
        if (session.generation !== generation) return;
        const result = record(await client.request("session/acceptPlan"));
        if (session.generation !== generation) return;
        session.checkpoint = record(result.checkpoint);
        session.mode = "implement";
      } finally {
        if (session.generation === generation) session.busy = false;
        this.publish();
      }
      return;
    }
    if (message.type === "new") {
      const generation = ++session.generation;
      session.busy = true;
      try {
        const instructions = await this.loadInstructions(session);
        if (session.generation !== generation) return;
        session.instructions = instructions;
        session.client?.dispose();
        session.completionCache.clear();
    session.completion?.dispose();
        session.client = session.completion = undefined;
        session.checkpoint = undefined;
        session.usageId = randomUUID();
        ++this.usageGeneration;
        session.messages = [];
        session.checks = [];
        session.permissions.clear();
        session.mode = "plan";
        this.attachments = [];
      } finally {
        if (session.generation === generation) session.busy = false;
      }
      this.publish();
      return;
    }
    if (message.type === "attach") {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.selection.isEmpty) return;
      if (
        vscode.workspace
          .getWorkspaceFolder(editor.document.uri)
          ?.uri.toString() !== session.folder.uri.toString()
      )
        throw Error("Select text inside the selected workspace.");
      await session.tools.guarded(
        path.relative(session.folder.uri.fsPath, editor.document.uri.fsPath),
      );
      const text = editor.document.getText(editor.selection);
      if (text.length > 10000)
        throw Error("Selection is too large (10,000 characters maximum).");
      this.attachments = [
        `Selection from ${path.relative(session.folder.uri.fsPath, editor.document.uri.fsPath)}:\n${text}`,
      ];
      this.publish();
      return;
    }
    if (
      message.type === "send" &&
      typeof message.text === "string" &&
      message.text.trim() &&
      message.text.length <= 100000
    )
      await this.run(message.text);
  }
  async run(task: string) {
    this.trusted();
    const session = this.session();
    this.selected = session;
    if (session.busy) throw Error("Agent is busy.");
    const full = [task, ...this.attachments].join("\n\n");
    if (full.length > 100000)
      throw Error("Task and attachments exceed 100,000 characters.");
    const generation = ++session.generation;
    session.busy = true;
    session.cancelled = false;
    this.attachments = [];
    session.messages.push({ role: "user", text: task });
    this.publish();
    try {
      const client = await this.initialize(session);
      if (session.cancelled || session.generation !== generation) return;
      const result = record(
        await client.request("session/run", { task: full, mode: session.mode }),
      );
      if (session.cancelled || session.generation !== generation) return;
      session.checkpoint = record(result.checkpoint);
      if (Array.isArray(result.verification)) {
        const evidence = result.verification.map(record);
        for (const item of evidence) {
          if (item.status === "passed" && session.checkRevisions.get(String(item.profile)) !== session.revision) item.status = "stale";
          session.checks = session.checks.filter(check => check.profile !== item.profile);
          session.checks.push({ profile: String(item.profile), result: { status: item.status, passed: item.status === "passed" } });
        }
      }
      session.messages.push({ role: "assistant", text: String(result.text) });
    } catch (e) {
      if (session.generation !== generation) return;
      session.messages.push({
        role: "status",
        text: session.cancelled
          ? "Run cancelled; accepted edits may remain."
          : e instanceof Error
            ? e.message
            : "Run failed.",
      });
    } finally {
      if (session.generation === generation && !session.cancelling)
        session.busy = false;
      this.publish();
    }
  }
  async stop() {
    const session =
      this.selected || [...this.sessions.values()].find((s) => s.busy);
    if (!session) return;
    if (session.stopTask) return session.stopTask;
    const task = this.cancelSession(session);
    session.stopTask = task;
    try {
      await task;
    } finally {
      if (session.stopTask === task) session.stopTask = undefined;
    }
  }
  private async cancelSession(session: Session) {
    const wasBusy = session.busy;
    session.generation++;
    session.cancelling = true;
    session.busy = true;
    session.cancelled = true;
    session.tools.cancel();
    const client = session.client;
    if (client?.running) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const result = record(
          await Promise.race([
            client.request("cancel"),
            new Promise((_, reject) => {
              timer = setTimeout(
                () =>
                  reject(
                    Error(
                      "Cancellation timed out. Check workspace host processes before retrying.",
                    ),
                  ),
                10000,
              );
            }),
          ]),
        );
        if (result.cleanupComplete !== true)
          throw Error(
            "Command cleanup incomplete. Check workspace host processes before retrying.",
          );
        client.dispose();
        session.client = undefined;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
    session.completionCache.clear();
    session.completion?.dispose();
    session.completion = undefined;
    session.cancelling = false;
    session.busy = false;
    if (wasBusy)
      session.messages.push({
        role: "status",
        text: "Run cancelled; accepted edits may remain.",
      });
    this.publish();
  }
  private async stopAll() {
    const selected = this.selected;
    try {
      for (const session of this.sessions.values()) {
        this.selected = session;
        await this.stop();
      }
    } finally {
      this.selected = selected;
    }
  }
  async configure() {
    this.trusted();
    const session = this.session();
    this.selected = session;
    await this.stopAll();
    this.selected = session;
    const cfg = vscode.workspace.getConfiguration(
      "genericAgent",
      session.folder.uri,
    );
    const provider = await vscode.window.showQuickPick(
      ["demo", "ollama", "openai", "openai-compatible"],
      { title: "Model provider" },
    );
    if (!provider) return;
    let baseUrl = "";
    if (provider === "ollama" || provider === "openai-compatible") {
      const entered = await vscode.window.showInputBox({
        title: "Model server HTTP(S) base URL",
        value:
          cfg.get<string>("baseUrl") ||
          (provider === "ollama" ? "http://localhost:11434" : ""),
      });
      if (entered === undefined) return;
      baseUrl = entered;
    }
    const keyName = providerKey(provider, baseUrl);
    let model = "";
    if (provider !== "demo") {
      const entered = await vscode.window.showInputBox({
        title: "Model name",
        value:
          cfg.get<string>("model") || (provider === "openai" ? "gpt-4.1" : ""),
      });
      if (entered === undefined) return;
      model = entered;
      if (!model) throw Error("A model name is required.");
    }
    if (["openai", "openai-compatible"].includes(provider)) {
      const existing = await this.context.secrets.get(keyName);
      const key = await vscode.window.showInputBox({
        title: existing
          ? "API key (leave empty to keep existing key)"
          : "API key",
        password: true,
        ignoreFocusOut: true,
      });
      if (key === undefined) return;
      if (key) await this.context.secrets.store(keyName, key);
      else if (!existing) throw Error("An API key is required.");
    }
    for (const [key, value] of Object.entries({ provider, model, baseUrl }))
      await cfg.update(key, value, vscode.ConfigurationTarget.WorkspaceFolder);
    for (const s of this.sessions.values()) {
      s.client?.dispose();
      s.completionCache.clear();
      s.completion?.dispose();
      s.permissions.clear();
      s.client = s.completion = undefined;
    }
    this.publish();
  }
  async toggleInline() {
    this.trusted();
    const session = this.session();
    const id = `inline:${session.folder.uri}`;
    const enabled = !(
      this.context.workspaceState.get<boolean>(id, false) &&
      vscode.workspace
        .getConfiguration("genericAgent", session.folder.uri)
        .get<boolean>("inline.enabled")
    );
    await this.context.workspaceState.update(id, enabled);
    await vscode.workspace
      .getConfiguration("genericAgent", session.folder.uri)
      .update(
        "inline.enabled",
        enabled,
        vscode.ConfigurationTarget.WorkspaceFolder,
      );
    void vscode.window.showInformationMessage(
      enabled
        ? "Automatic completions enabled: active-document context will be sent to the selected provider."
        : "Automatic completions disabled.",
    );
    this.publish();
  }
  async triggerCompletion() {
    this.trusted();
    this.manualInline = true;
    try {
      await vscode.commands.executeCommand(
        "editor.action.inlineSuggest.trigger",
      );
    } finally {
      this.manualInline = false;
    }
  }
  async complete(
    document: vscode.TextDocument,
    position: vscode.Position,
    context: vscode.InlineCompletionContext,
    token: vscode.CancellationToken,
  ): Promise<vscode.InlineCompletionItem[]> {
    if (!vscode.workspace.isTrusted) return [];
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) return [];
    const session = this.session(folder);
    const cfg = vscode.workspace.getConfiguration("genericAgent", folder.uri);
    const automatic =
      cfg.get<boolean>("inline.enabled") &&
      this.context.workspaceState.get<boolean>(`inline:${folder.uri}`, false);
    if (
      !automatic &&
      !this.manualInline &&
      context.triggerKind !== vscode.InlineCompletionTriggerKind.Invoke
    )
      return [];
    const stamp = {
      uri: document.uri.toString(),
      version: document.version,
      offset: document.offsetAt(position),
    };
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (
      token.isCancellationRequested ||
      document.version !== stamp.version
    )
      return [];
    try {
      await session.tools.guarded(
        path.relative(folder.uri.fsPath, document.uri.fsPath),
      );
      if (token.isCancellationRequested || document.version !== stamp.version) return [];
      const generation = session.generation;
      const params = { ...completionContext(document.getText(), stamp.offset), language: document.languageId };
      const key = sha(JSON.stringify([generation, stamp, params, cfg.get("provider"), cfg.get("model"), cfg.get("baseUrl"), cfg.get("inline.model")]));
      const text = await session.completionCache.get(key, token, () => {
        let client: RpcClient | undefined;
        let cancelled = false;
        const cancel = () => { cancelled = true; client?.dispose(); };
        const result = (async () => {
          client = await this.initialize(session, true);
          if (cancelled || generation !== session.generation || document.version !== stamp.version) return "";
          let timer: ReturnType<typeof setTimeout> | undefined;
          try {
            const reply = record(await Promise.race([
              client.request("completion/generate", params),
              new Promise((_, reject) => { timer = setTimeout(() => { cancel(); reject(Error("Completion timed out.")); }, 5000); }),
            ]));
            return typeof reply.text === "string" ? reply.text : "";
          } finally { if (timer) clearTimeout(timer); }
        })();
        return { result, cancel };
      });
      const editor = vscode.window.activeTextEditor;
      const now = {
        uri: editor?.document.uri.toString() || "",
        version: document.version,
        offset: editor ? editor.document.offsetAt(editor.selection.active) : -1,
      };
      if (
        token.isCancellationRequested ||
        !sameStamp(stamp, now) ||
        typeof text !== "string" ||
        !text || generation !== session.generation
      )
        { if (!token.isCancellationRequested && text) session.completionCache.stats.discarded++; return []; }
      return [
        new vscode.InlineCompletionItem(
          text.slice(0, 2000),
          new vscode.Range(position, position),
        ),
      ];
    } catch {
      return [];
    } finally {
      if (this.selected === session) void this.view?.webview.postMessage({
        type: "completionStats", stats: session.completionCache.stats,
      });
    }
  }
  content(uri: vscode.Uri) {
    for (const session of this.sessions.values()) {
      const value = session.tools.provideTextDocumentContent(uri);
      if (value) return value;
    }
    return "";
  }
  refreshDiagnostics() {
    this.publish();
  }
  private diagnosticItems() {
    this.diagnostics.clear();
    const session =
      this.selected ||
      (vscode.workspace.workspaceFolders?.[0] && this.session());
    if (!session) return [];
    const items: {
      id: string;
      file: string;
      message: string;
      source: string;
      line: number;
      severity: string;
    }[] = [];
    for (const [uri, diagnostics] of vscode.languages.getDiagnostics()) {
      if (
        vscode.workspace.getWorkspaceFolder(uri)?.uri.toString() !==
        session.folder.uri.toString()
      )
        continue;
      let file: string;
      try {
        file = path.relative(
          session.folder.uri.fsPath,
          relativePath(session.folder.uri.fsPath, uri.fsPath),
        );
      } catch {
        continue;
      }
      for (const diagnostic of diagnostics) {
        if (items.length >= 200) return items;
        const id = sha(
          JSON.stringify([
            uri.toString(),
            diagnostic.range,
            diagnostic.message,
            diagnostic.source,
          ]),
        );
        this.diagnostics.set(id, { uri, diagnostic });
        items.push({
          id,
          file,
          message: diagnostic.message.slice(0, 2000),
          source: diagnostic.source || "language tool",
          line: diagnostic.range.start.line + 1,
          severity: vscode.DiagnosticSeverity[diagnostic.severity],
        });
      }
    }
    return items;
  }
  async diagnostic(id: string, fix: boolean) {
    this.trusted();
    const item = this.diagnostics.get(id);
    if (!item) throw Error("Diagnostic changed; refresh and try again.");
    const folder = vscode.workspace.getWorkspaceFolder(item.uri);
    if (!folder) return;
    const session = this.session(folder);
    if (session.busy) throw Error("Stop the current run first.");
    this.selected = session;
    await session.tools.guarded(
      path.relative(folder.uri.fsPath, item.uri.fsPath),
    );
    session.mode = fix ? "implement" : "plan";
    await this.run(
      `${fix ? "Propose a targeted fix for" : "Explain"} this existing editor diagnostic. ${fix ? "All edits require individual review." : "Do not edit files."}\nFile: ${path.relative(folder.uri.fsPath, item.uri.fsPath)}, line ${item.diagnostic.range.start.line + 1}\nSource: ${item.diagnostic.source || "language tool"}\n${item.diagnostic.message}`,
    );
  }
  async chooseDiagnostic(fix: boolean) {
    const items = this.diagnosticItems();
    const selected = await vscode.window.showQuickPick(
      items.map((x) => ({
        label: `${x.file}:${x.line}`,
        description: x.message,
        id: x.id,
      })),
      { title: fix ? "Propose diagnostic fix" : "Explain diagnostic" },
    );
    if (selected) await this.diagnostic(selected.id, fix);
  }
  publish() {
    const session =
      this.selected ||
      (vscode.workspace.workspaceFolders?.[0] && this.session());
    if (session) this.selected = session;
    const cfg = vscode.workspace.getConfiguration(
      "genericAgent",
      session?.folder.uri,
    );
    void this.view?.webview.postMessage({
      type: "state",
      trusted: vscode.workspace.isTrusted,
      folder: session?.folder.uri.toString(),
      folders: (vscode.workspace.workspaceFolders || []).map((f) => ({
        uri: f.uri.toString(),
        name: f.name,
      })),
      mode: session?.mode || "plan",
      busy: session?.busy || false,
      provider: cfg.get("provider", "demo"),
      model: cfg.get("model", ""),
      messages: session?.messages || [],
      proposals: session
        ? [...session.tools.proposals.values()].map((p) => ({
            id: p.id,
            path: p.path,
          }))
        : [],
      checks: session?.checks || [],
      diagnostics: this.diagnosticItems(),
      attachmentCount: this.attachments.length,
      usageSession: session?.usageId,
      completionStats: session?.completionCache.stats,
    });
  }
  invalidateVerification() {
    for (const session of this.sessions.values()) {
      ++session.revision;
      for (const check of session.checks) {
        const result = record(check.result);
        if (result.passed) check.result = { ...result, passed: false, status: "stale" };
      }
    }
    this.publish();
  }
  private async metadata(method: string, params: RecordValue = {}) {
    this.trusted();
    if (!this.usageClient?.running) {
      const python = await this.runtime.ready("demo");
      this.usageClient = new RpcClient(python, this.context.globalStorageUri.fsPath,
        async () => { throw Error("Metadata requests have no editor callbacks."); }, () => {});
    }
    return this.usageClient.request(method, params);
  }
  async diagnose(exportFile = false) {
    const checks = validateChecks(vscode.workspace.getConfiguration("genericAgent", this.session().folder.uri).get("checks", {}));
    const report = record(await this.metadata("diagnostics/query", { checks: Object.fromEntries(Object.entries(checks).map(([name, check]) => [name, check.command])) }));
    report.backendMatches = report.backend === "0.12.0" && report.protocol === 2;
    const text = JSON.stringify(report, null, 2);
    if (exportFile) {
      const destination = await vscode.window.showSaveDialog({ title: "Export redacted agent diagnostics", filters: { JSON: ["json"] } });
      if (destination) await fs.writeFile(destination.fsPath, text + "\n", { flag: "wx", mode: 0o600 });
    } else {
      void this.view?.webview.postMessage({ type: "progress", text });
      await vscode.window.showInformationMessage("Diagnostics complete. " + text);
    }
  }
  async savedSessions(remove = false) {
    this.trusted();
    const session = this.session();
    if (session.busy) throw Error("Stop the current run before managing saved sessions.");
    const params = { workspace: session.folder.uri.fsPath };
    const saved = await this.metadata("session/listSaved", params);
    if (!Array.isArray(saved)) throw Error("Invalid saved-session list.");
    const choice = await vscode.window.showQuickPick(saved.map(value => {
      const item = record(value), connection = record(item.connection);
      return { label: String(item.id), description: `${connection.provider} / ${connection.model}`, id: String(item.id) };
    }), { title: remove ? "Delete saved conversation" : "Resume conversation in Plan" });
    if (!choice) return;
    if (remove) {
      const answer = await vscode.window.showWarningMessage("Delete this saved conversation?", "Delete", "Keep");
      if (answer !== "Delete") return;
      await this.metadata("session/deleteSaved", { ...params, sessionId: choice.id });
      if (session.usageId === choice.id) {
        await this.stop();
        session.client?.dispose(); session.client = undefined;
        session.usageId = randomUUID();
      }
      return;
    }
    if (!vscode.workspace.getConfiguration("genericAgent", session.folder.uri).get("persistSession", false))
      throw Error("Enable genericAgent.persistSession in user settings to opt into local prompt/source recovery. Snapshots expire after 30 inactive days.");
    const instructions = await this.loadInstructions(session);
    const result = record(await this.metadata("session/resumeSaved", { ...params, sessionId: choice.id }));
    await this.stop();
    session.client?.dispose(); session.completionCache.clear();
    session.completion?.dispose();
    session.client = session.completion = undefined;
    session.checkpoint = record(result.checkpoint);
    session.instructions = instructions;
    session.usageId = choice.id;
    session.mode = "plan";
    session.permissions.clear(); session.checks = [];
    this.selected = session; this.attachments = [];
    session.messages = [{ role: "status", text: "Resumed completed turns in Plan. Plan acceptance and verification reset; no actions replayed. The current provider configuration remains active." }];
    if (result.workspaceChanged) session.messages.push({ role: "status", text: "Workspace changed or its previous fingerprint is unavailable. Inspect current files before implementation." });
    ++this.usageGeneration;
    this.publish();
  }
  async foldersChanged() {
    const selected = this.selected;
    const roots = new Set(
      (vscode.workspace.workspaceFolders || []).map((f) => f.uri.toString()),
    );
    for (const [id, session] of this.sessions) {
      if (!roots.has(id)) {
        this.selected = session;
        await this.stop();
        this.sessions.delete(id);
      }
    }
    this.selected =
      selected && roots.has(selected.folder.uri.toString())
        ? selected
        : undefined;
    this.attachments = [];
    this.publish();
  }
  async configurationChanged() {
    this.invalidateVerification();
    await this.stopAll();
    for (const s of this.sessions.values()) {
      s.client?.dispose();
      s.completionCache.clear();
      s.completion?.dispose();
      s.client = s.completion = undefined;
      s.permissions.clear();
      const cfg = vscode.workspace.getConfiguration("genericAgent", s.folder.uri);
      const signature = JSON.stringify([cfg.get("provider", "demo"), cfg.get("model", ""), cfg.get("baseUrl", "")]);
      if (s.connectionSignature !== undefined && signature !== s.connectionSignature) {
        s.usageId = randomUUID();
        s.checkpoint = undefined;
        s.messages = [];
        s.checks = [];
        s.mode = "plan";
        if (this.selected === s) this.attachments = [];
        ++this.usageGeneration;
      }
      s.connectionSignature = signature;
    }
    this.publish();
  }
  dispose() {
    this.usageClient?.dispose();
    for (const s of this.sessions.values()) {
      s.tools.cancel();
      if (s.client?.running)
        void s.client.request("cancel").then(
          () => s.client?.dispose(),
          () => s.client?.dispose(),
        );
      s.completionCache.clear();
      s.completion?.dispose();
    }
  }
}
export function activate(context: vscode.ExtensionContext) {
  const controller = new Controller(context);
  const watcher = vscode.workspace.createFileSystemWatcher("**/*");
  context.subscriptions.push(watcher,
    watcher.onDidChange(() => controller.invalidateVerification()),
    watcher.onDidCreate(() => controller.invalidateVerification()),
    watcher.onDidDelete(() => controller.invalidateVerification()));
  context.subscriptions.push(
    controller,
    vscode.window.registerWebviewViewProvider(
      "genericAgent.sidebar",
      controller,
    ),
    vscode.workspace.registerTextDocumentContentProvider("generic-agent", {
      provideTextDocumentContent: (uri) => controller.content(uri),
    }),
  );
  const command = (name: string, fn: () => unknown) =>
    context.subscriptions.push(
      vscode.commands.registerCommand(`genericAgent.${name}`, () =>
        Promise.resolve()
          .then(fn)
          .catch((e) =>
            vscode.window.showErrorMessage(
              e instanceof Error ? e.message : "Agent operation failed.",
            ),
          ),
      ),
    );
  command("open", () =>
    vscode.commands.executeCommand("genericAgent.sidebar.focus"),
  );
  command("setup", () => controller.action({ type: "setup" }));
  command("configure", () => controller.configure());
  command("diagnose", () => controller.diagnose());
  command("exportDiagnostics", () => controller.diagnose(true));
  command("sessions", () => controller.savedSessions());
  command("deleteSession", () => controller.savedSessions(true));
  command("usage", async () => {
    await vscode.commands.executeCommand("genericAgent.sidebar.focus");
    await controller.showUsage();
  });
  command("stop", () => controller.stop());
  command("complete", () => controller.triggerCompletion());
  command("toggleInline", () => controller.toggleInline());
  command("explainDiagnostic", () => controller.chooseDiagnostic(false));
  command("fixDiagnostic", () => controller.chooseDiagnostic(true));
  context.subscriptions.push(
    vscode.languages.registerInlineCompletionItemProvider(
      { pattern: "**/*" },
      {
        provideInlineCompletionItems: (...args) => controller.complete(...args),
      },
    ),
    vscode.languages.onDidChangeDiagnostics(() =>
      controller.refreshDiagnostics(),
    ),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (
        [
          "provider",
          "model",
          "baseUrl",
          "pythonPath",
          "checks",
          "maxSteps",
          "maxContextChars",
          "contextMode", "maxContextTokens", "maxRunTokens",
          "inline.model",
          "requestTimeout", "maxOutputTokens", "persistSession",
        ].some((k) => e.affectsConfiguration("genericAgent." + k))
      )
        void controller.configurationChanged().catch(() => {});
    }),
    vscode.workspace.onDidGrantWorkspaceTrust(() => controller.publish()),
    vscode.workspace.onDidChangeTextDocument(() => controller.invalidateVerification()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      void controller.foldersChanged().catch(() => {});
    }),
  );
  return { controller };
}
