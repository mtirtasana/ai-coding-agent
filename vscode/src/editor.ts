import * as vscode from "vscode";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { randomUUID } from "node:crypto";
import { FilePolicy } from "./filePolicy";
import {
  relativePath,
  protectedPart,
  replaceUnique,
  replacementDiff,
  sha,
  validateText,
  lineRange,
  validateEditorArguments,
  validateBatch,
} from "./shared";

export interface ToolReply {
  ok: boolean;
  content: string;
}
export interface Proposal {
  id: string;
  path: string;
  before: string;
  after: string;
  version: number;
  exists: boolean;
  document?: vscode.TextDocument;
  resolve: (accept: boolean) => void;
  current: () => boolean;
}
const skip = new Set([
  ".venv",
  "venv",
  "node_modules",
  "__pycache__",
  ".tox",
  ".ruff_cache",
  "build",
  "dist",
]);
export class EditorTools implements vscode.TextDocumentContentProvider {
  private canonical?: string;
  private async canonicalRoot() {
    const current = await fs.realpath(this.root.uri.fsPath);
    if (this.canonical && this.canonical !== current)
      throw Error("Workspace root changed.");
    this.canonical = current;
    return current;
  }
  private async uriFor(file: string) {
    const relative = path.relative(await this.canonicalRoot(), file);
    return vscode.Uri.file(path.join(this.root.uri.fsPath, relative));
  }
  private documents = new Map<string, string>();
  readonly proposals = new Map<string, Proposal>();
  constructor(
    readonly root: vscode.WorkspaceFolder,
    private changed: () => void,
  ) {}
  provideTextDocumentContent(uri: vscode.Uri) {
    return this.documents.get(uri.toString()) || "";
  }
  async guarded(name: string) {
    const root = await this.canonicalRoot();
    const requested = relativePath(root, name);
    let cursor = requested;
    const tail: string[] = [];
    for (;;) {
      try {
        cursor = await fs.realpath(cursor);
        break;
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
        const parent = path.dirname(cursor);
        if (parent === cursor) throw Error("Cannot resolve path.");
        tail.unshift(path.basename(cursor));
        cursor = parent;
      }
    }
    const resolved = path.join(cursor, ...tail);
    relativePath(root, resolved);
    if (path.relative(root, resolved).split(path.sep).some(protectedPart))
      throw Error("Protected path.");
    const policy = new FilePolicy(root);
    const directory = await fs.stat(resolved).then(info => info.isDirectory(), () => false);
    if (await policy.denied(path.relative(root, resolved), directory) || await policy.denied(path.relative(root, requested), directory))
      throw Error("Path denied by agent file policy.");
    return resolved;
  }
  async read(name: string) {
    const file = await this.guarded(name);
    const stat = await fs.stat(file);
    if (!stat.isFile() || stat.size > 400000)
      throw Error("File is not eligible text.");
    const raw = await fs.readFile(file);
    new TextDecoder("utf-8", { fatal: true }).decode(raw);
    const document = await vscode.workspace.openTextDocument(
      await this.uriFor(file),
    );
    const text = document.getText();
    validateText(text);
    return { file, document, text, hash: sha(text) };
  }
  private async diff(proposal: Proposal) {
    const before = vscode.Uri.parse(`generic-agent:${proposal.id}/before`);
    const after = vscode.Uri.parse(`generic-agent:${proposal.id}/after`);
    this.documents.set(before.toString(), proposal.before);
    this.documents.set(after.toString(), proposal.after);
    await vscode.commands.executeCommand(
      "vscode.diff",
      before,
      after,
      `Agent review: ${proposal.path}`,
      { preview: true },
    );
  }
  async decide(id: string, accept: boolean) {
    const p = this.proposals.get(id);
    if (!p) return;
    this.proposals.delete(id);
    p.resolve(accept && p.current());
    this.changed();
  }
  cancel() {
    for (const [id, p] of this.proposals) {
      this.proposals.delete(id);
      p.resolve(false);
    }
    this.documents.clear();
    this.changed();
  }
  private async approve(
    name: string,
    before: string,
    after: string,
    document: vscode.TextDocument | undefined,
    current: () => boolean,
    expectedFile: string,
  ) {
    const id = randomUUID();
    const p: Proposal = {
      id,
      path: name,
      before,
      after,
      version: document?.version ?? 0,
      exists: !!document,
      document,
      current,
      resolve: () => {},
    };
    const decision = new Promise<boolean>((resolve) => {
      p.resolve = resolve;
    });
    this.proposals.set(id, p);
    this.changed();
    try {
      await this.diff(p);
      if (!current() || !(await decision))
        throw Error("Change rejected or cancelled.");
      const file = await this.guarded(name);
      if (file !== expectedFile)
        throw Error("Resolved path changed during review.");
      let appliedText = after;
      if (document) {
        if (
          document.isClosed ||
          document.version !== p.version ||
          sha(document.getText()) !== sha(before)
        )
          throw Error("Stale edit: obtain a new snapshot.");
        const editor = await vscode.window.showTextDocument(document, {
          preview: false,
          preserveFocus: true,
        });
        if (
          !current() ||
          document.version !== p.version ||
          sha(document.getText()) !== sha(before)
        )
          throw Error("Stale edit.");
        const applied = await editor.edit(
          (builder) =>
            builder.replace(
              new vscode.Range(
                document.positionAt(0),
                document.positionAt(document.getText().length),
              ),
              after,
            ),
          { undoStopBefore: true, undoStopAfter: true },
        );
        if (!applied) throw Error("Editor rejected stale edit.");
        appliedText = document.getText();
      } else {
        try {
          await fs.lstat(file);
          throw Error("File now exists; obtain a snapshot.");
        } catch (e) {
          if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
        }
        if (!current()) throw Error("Cancelled.");
        const edit = new vscode.WorkspaceEdit();
        const uri = await this.uriFor(file);
        edit.createFile(uri, { overwrite: false, ignoreIfExists: false });
        edit.insert(uri, new vscode.Position(0, 0), after);
        if (!(await vscode.workspace.applyEdit(edit)))
          throw Error("Could not create file.");
        const created = await vscode.window.showTextDocument(uri, {
          preview: false,
          preserveFocus: true,
        });
        appliedText = created.document.getText();
      }
      return JSON.stringify({
        applied: true,
        path: name,
        sha256: sha(appliedText),
      });
    } finally {
      this.proposals.delete(id);
      this.documents.delete(`generic-agent:${id}/before`);
      this.documents.delete(`generic-agent:${id}/after`);
      this.changed();
    }
  }
  async execute(
    name: string,
    rawArgs: Record<string, unknown>,
    mode: "plan" | "implement",
    current: () => boolean,
  ): Promise<ToolReply> {
    try {
      if (!current() || !vscode.workspace.isTrusted)
        throw Error("Run cancelled or workspace untrusted.");
      validateEditorArguments(name, rawArgs);
      if (name === "batch_inspect") return { ok: true, content: await this.batchInspect(rawArgs.requests, current) };
      const pathArg = rawArgs.path as string;
      if (["write_file", "apply_edit"].includes(name) && mode !== "implement")
        throw Error("Planning cannot edit files.");
      const file = await this.guarded(pathArg);
      if (name === "read_file_range") {
        const snap = await this.read(pathArg);
        return {
          ok: true,
          content: JSON.stringify({
            path: path
              .relative(await this.canonicalRoot(), snap.file)
              .split(path.sep)
              .join("/"),
            sha256: snap.hash,
            documentVersion: snap.document.version,
            ...lineRange(snap.text, rawArgs.start_line, rawArgs.end_line),
          }),
        };
      }
      const args = rawArgs as Record<string, string>;
      if (name === "list_files") {
        const entries = await fs.readdir(file, { withFileTypes: true });
        entries.sort((a, b) => a.name.localeCompare(b.name));
        const policy = new FilePolicy(await this.canonicalRoot());
        const eligible = [];
        for (const entry of entries.slice(0, 500))
          if (!await policy.ignored(path.relative(await this.canonicalRoot(), path.join(file, entry.name)), entry.isDirectory())) eligible.push(entry);
        return {
          ok: true,
          content: eligible
            .map((e) => e.name + (e.isDirectory() ? "/" : ""))
            .join("\n"),
        };
      }
      if (name === "search_text")
        return { ok: true, content: await this.search(pathArg, args.query) };
      if (name === "write_file") {
        validateText(args.content);
        let existing: Awaited<ReturnType<EditorTools["read"]>> | undefined;
        try {
          existing = await this.read(pathArg);
        } catch (e) {
          if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
        }
        return {
          ok: true,
          content: await this.approve(
            pathArg,
            existing?.text || "",
            args.content,
            existing?.document,
            current,
            existing?.file || file,
          ),
        };
      }
      const snap = await this.read(pathArg);
      if (name === "read_file") return { ok: true, content: snap.text };
      if (name === "file_snapshot")
        return {
          ok: true,
          content: JSON.stringify({
            path: pathArg,
            sha256: snap.hash,
            content: snap.text,
            documentVersion: snap.document.version,
          }),
        };
      if (!["preview_edit", "apply_edit"].includes(name))
        throw Error("Unknown editor tool.");
      if (args.expected_sha256 !== snap.hash)
        throw Error("Stale file snapshot.");
      const after = replaceUnique(snap.text, args.old_text, args.new_text);
      if (name === "preview_edit") {
        const diff = replacementDiff(pathArg, snap.text, after);
        return {
          ok: true,
          content: JSON.stringify({
            applied: false,
            path: pathArg,
            sha256: sha(after),
            diff: diff.slice(0, 20000),
            diff_truncated: diff.length > 20000,
          }),
        };
      }
      return {
        ok: true,
        content: await this.approve(
          pathArg,
          snap.text,
          after,
          snap.document,
          current,
          snap.file,
        ),
      };
    } catch (e) {
      return {
        ok: false,
        content: e instanceof Error ? e.message : "Editor operation failed.",
      };
    }
  }
  private async batchInspect(requests: unknown, current: () => boolean) {
    validateBatch(requests);
    const snapshots = new Map<string, Awaited<ReturnType<EditorTools["read"]>>>();
    const versions = new Map<string, number>();
    const results: Record<string, unknown>[] = [];
    let reserved = requests.reduce((sum, item) => sum + JSON.stringify(item.path).length + 200, 0);
    for (const request of requests) {
      if (!current() || !vscode.workspace.isTrusted) throw Error("Inspection cancelled.");
      reserved -= JSON.stringify(request.path).length + 200;
      let item: Record<string, unknown>;
      try {
        let snap = snapshots.get(request.path);
        if (!snap) { snap = await this.read(request.path); snapshots.set(request.path, snap); versions.set(request.path, snap.document.version); }
        item = { ok: true, path: path.relative(await this.canonicalRoot(), snap.file).split(path.sep).join("/"),
          sha256: snap.hash, documentVersion: snap.document.version,
          ...lineRange(snap.text, request.start_line, request.end_line) };
        if (JSON.stringify({ results: [...results, item], stable: true, atomic: false }).length > 24000 - reserved - 1280)
          item = { ok: false, path: request.path, error: "Batch output budget exceeded; request fewer ranges." };
      } catch { item = { ok: false, path: request.path, error: "Range unavailable, denied or outside text limits." }; }
      results.push(item);
    }
    let stable = true;
    for (const [name, original] of snapshots) {
      try {
        const now = await this.read(name);
        if (now.file !== original.file || now.hash !== original.hash || now.document.version !== versions.get(name)) stable = false;
      } catch { stable = false; }
    }
    if (!current() || !vscode.workspace.isTrusted) throw Error("Inspection cancelled.");
    return JSON.stringify({ results, stable, atomic: false, note: "Sequential observations, revalidated at the end; hashes may become stale." });
  }
  private async search(name: string, query: string) {
    if (!query || query.length > 512 || /[\r\n]/.test(query))
      throw Error("Expected a single-line literal query.");
    const canonicalRoot = await this.canonicalRoot();
    const queue = [await this.guarded(name)];
    const matches: { path: string; line: number; snippet: string }[] = [];
    let visited = 0,
      scanned = 0,
      chars = 0,
      truncated = false;
    while (queue.length && !truncated) {
      const file = queue.shift()!;
      const info = await fs.lstat(file);
      if (info.isSymbolicLink()) continue;
      if (info.isDirectory()) {
        const directory = await fs.opendir(file);
        for await (const entry of directory) {
          if (++visited > 10000) {
            truncated = true;
            break;
          }
          if (
            entry.isSymbolicLink() ||
            protectedPart(entry.name) ||
            skip.has(entry.name)
          )
            continue;
          if (await new FilePolicy(canonicalRoot).ignored(path.relative(canonicalRoot, path.join(file, entry.name)), entry.isDirectory())) continue;
          queue.push(
            await this.guarded(
              path.relative(canonicalRoot, path.join(file, entry.name)),
            ),
          );
        }
      } else if (info.isFile()) {
        if (scanned >= 1000) {
          truncated = true;
          break;
        }
        scanned++;
        let text: string;
        const relative = path.relative(canonicalRoot, file);
        try {
          text = (await this.read(relative)).text;
        } catch {
          continue;
        }
        for (const [i, line] of text.split(/\r?\n/).entries()) {
          const offset = line.indexOf(query);
          if (offset < 0) continue;
          const item = {
            path: relative,
            line: i + 1,
            snippet: line.slice(Math.max(0, offset - 60), offset + 180),
          };
          const size = JSON.stringify(item).length;
          if (matches.length >= 100 || chars + size > 20000) {
            truncated = true;
            break;
          }
          matches.push(item);
          chars += size;
        }
      }
    }
    return JSON.stringify({ matches, files_scanned: scanned, truncated });
  }
}
