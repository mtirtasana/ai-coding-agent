import * as vscode from "vscode";
import * as path from "node:path";
import { randomBytes } from "node:crypto";
import { EditorTools } from "./editor";
import { Runtime } from "./runtime";
import { RpcClient, RecordValue } from "./rpc";
import {
  completionContext,
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
  folder: vscode.WorkspaceFolder;
  tools: EditorTools;
  client?: RpcClient;
  completion?: RpcClient;
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
  inlineBusy: boolean;
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
        inlineBusy: false,
      };
      this.sessions.set(id, session);
    }
    return session;
  }
  private async initialize(session: Session, completion = false) {
    this.trusted();
    const generation = session.generation;
    const existing = completion ? session.completion : session.client;
    if (existing?.running) return existing;
    const cfg = vscode.workspace.getConfiguration(
      "genericAgent",
      session.folder.uri,
    );
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
        protocolVersion: 1,
        workspace: session.folder.uri.fsPath,
        provider,
        model,
        baseUrl,
        checks: completion ? {} : validateChecks(cfg.get("checks", {})),
        maxSteps: cfg.get("maxSteps", 15),
        maxContextChars: cfg.get("maxContextChars", 200000),
      };
      if (["openai", "openai-compatible"].includes(provider) && !key)
        throw Error("Use Configure Provider to set an API key.");
      if (key) config.apiKey = key;
      if (!completion && session.checkpoint)
        config.checkpoint = session.checkpoint;
      const reply = record(await client.request("initialize", config));
      if (session.generation !== generation)
        throw Error("Operation cancelled.");
      if (reply.protocolVersion !== 1 || reply.backendVersion !== "0.8.0")
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
      if (Object.values(args).some((v) => typeof v !== "string"))
        throw Error("Invalid arguments.");
      return session.tools.execute(
        params.name,
        args as Record<string, string>,
        session.mode,
        current,
      );
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
      return current();
    }
    if (method === "editor/checkResult") {
      session.checks.push({
        profile: String(params.profile),
        result: params.result,
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
    view.webview.html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${css}"></head><body><header><h2>Coding Agent</h2><select id="folder" aria-label="Workspace folder"></select><p id="status"></p></header><nav><button data-action="setup">Set up backend</button><button data-action="configure">Provider</button><button data-action="plan">Plan</button><button data-action="acceptPlan">Accept plan</button><button data-action="new">New</button><button data-action="inline">Automatic completions</button></nav><main><section id="messages" aria-live="polite"></section><section><h3>Changes to review</h3><div id="proposals"></div></section><section><h3>Checks</h3><div id="checks"></div></section><section><h3>Editor diagnostics</h3><div id="diagnostics"></div></section></main><form id="form"><label for="task">Task</label><textarea id="task" rows="4" maxlength="100000"></textarea><p id="attachments"></p><button data-action="attach" type="button">Attach selection</button><button id="send" type="submit">Send</button><button data-action="stop" type="button">Stop</button></form><p id="progress" role="status"></p><script nonce="${nonce}" src="${js}"></script></body></html>`;
    this.context.subscriptions.push(
      view.webview.onDidReceiveMessage((message: unknown) => {
        void this.action(message).catch((e) => this.error(e));
      }),
    );
    this.publish();
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
        session.completion?.dispose();
        session.client = session.completion = undefined;
        session.checkpoint = undefined;
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
      document.version !== stamp.version ||
      session.inlineBusy
    )
      return [];
    session.inlineBusy = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await session.tools.guarded(
        path.relative(folder.uri.fsPath, document.uri.fsPath),
      );
      const client = await this.initialize(session, true);
      if (token.isCancellationRequested || document.version !== stamp.version)
        return [];
      const request = client.request("completion/generate", {
        ...completionContext(document.getText(), stamp.offset),
        language: document.languageId,
      });
      const result = record(
        await Promise.race([
          request,
          new Promise((_, reject) => {
            timer = setTimeout(() => {
              client.dispose();
              reject(Error("Completion timed out."));
            }, 5000);
          }),
        ]),
      );
      const editor = vscode.window.activeTextEditor;
      const now = {
        uri: editor?.document.uri.toString() || "",
        version: document.version,
        offset: editor ? editor.document.offsetAt(editor.selection.active) : -1,
      };
      if (
        token.isCancellationRequested ||
        !sameStamp(stamp, now) ||
        typeof result.text !== "string" ||
        !result.text
      )
        return [];
      return [
        new vscode.InlineCompletionItem(
          result.text.slice(0, 2000),
          new vscode.Range(position, position),
        ),
      ];
    } catch {
      return [];
    } finally {
      if (timer) clearTimeout(timer);
      session.inlineBusy = false;
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
    });
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
    await this.stopAll();
    for (const s of this.sessions.values()) {
      s.client?.dispose();
      s.completion?.dispose();
      s.client = s.completion = undefined;
      s.permissions.clear();
    }
    this.publish();
  }
  dispose() {
    for (const s of this.sessions.values()) {
      s.tools.cancel();
      if (s.client?.running)
        void s.client.request("cancel").then(
          () => s.client?.dispose(),
          () => s.client?.dispose(),
        );
      s.completion?.dispose();
    }
  }
}
export function activate(context: vscode.ExtensionContext) {
  const controller = new Controller(context);
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
          "inline.model",
        ].some((k) => e.affectsConfiguration("genericAgent." + k))
      )
        void controller.configurationChanged().catch(() => {});
    }),
    vscode.workspace.onDidGrantWorkspaceTrust(() => controller.publish()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => {
      void controller.foldersChanged().catch(() => {});
    }),
  );
  return { controller };
}
