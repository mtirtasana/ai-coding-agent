import * as path from "node:path";
import { createHash } from "node:crypto";
export type Profile = "test" | "lint" | "security" | "dependency_audit";
interface CompletionCancellation {
  isCancellationRequested: boolean;
  onCancellationRequested(callback: () => void): { dispose(): void };
}
interface CompletionEntry {
  key: string;
  result: Promise<string>;
  cancel: () => void;
  settled: boolean;
  completedAt: number;
  consumers: number;
}
export class CompletionCache {
  private entry?: CompletionEntry;
  readonly stats = { requested: 0, reused: 0, discarded: 0 };
  clear() {
    const previous = this.entry;
    this.entry = undefined;
    if (previous && !previous.settled) previous.cancel();
  }
  async get(key: string, token: CompletionCancellation,
    start: () => { result: Promise<string>; cancel: () => void }): Promise<string | undefined> {
    if (token.isCancellationRequested) return undefined;
    let entry = this.entry;
    if (!entry || entry.key !== key || (entry.settled && Date.now() - entry.completedAt > 10000)) {
      this.clear();
      const task = start();
      this.stats.requested++;
      entry = { key, result: task.result, cancel: task.cancel, settled: false, completedAt: 0, consumers: 0 };
      this.entry = entry;
      const current = entry;
      entry.result = task.result.then(text => {
        current.settled = true;
        current.completedAt = Date.now();
        if (!text && this.entry === current) this.entry = undefined;
        return text;
      }, error => {
        current.settled = true;
        if (this.entry === current) this.entry = undefined;
        throw error;
      });
    } else this.stats.reused++;
    const current = entry;
    current.consumers++;
    let cancelled = false;
    let listener: { dispose(): void } | undefined;
    const cancellation = new Promise<undefined>(resolve => {
      listener = token.onCancellationRequested(() => { cancelled = true; resolve(undefined); });
      if (token.isCancellationRequested) { cancelled = true; resolve(undefined); }
    });
    try {
      return await Promise.race([current.result, cancellation]);
    } finally {
      listener?.dispose();
      current.consumers--;
      if (cancelled) this.stats.discarded++;
      if (!current.settled && current.consumers === 0) {
        current.cancel();
        if (this.entry === current) this.entry = undefined;
      }
    }
  }
}
export interface Check {
  command: string[];
  timeout?: number;
  outputLimit?: number;
}
export const profiles: Profile[] = [
  "test",
  "lint",
  "security",
  "dependency_audit",
];
export const protectedPart = (p: string) =>
  [".git", ".ssh", ".aws", ".azure", ".config", ".agentignore", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"].includes(p.toLowerCase()) ||
  p.toLowerCase().startsWith(".env") || /\.(pem|key|p12|pfx)$/i.test(p);
export const sha = (s: string | Buffer) =>
  createHash("sha256").update(s).digest("hex");
export function replacementDiff(name: string, before: string, after: string): string {
  const lines = (text: string) => (text.match(/[^\n]*\n|[^\n]+$/g) || []);
  const oldLines = lines(before), newLines = lines(after);
  let prefix = 0, suffix = 0;
  while (prefix < oldLines.length && prefix < newLines.length &&
    oldLines[prefix] === newLines[prefix]) prefix++;
  if (prefix === oldLines.length && prefix === newLines.length) return "";
  while (suffix < oldLines.length - prefix && suffix < newLines.length - prefix &&
    oldLines[oldLines.length - suffix - 1] === newLines[newLines.length - suffix - 1]) suffix++;
  const start = Math.max(0, prefix - 3);
  const oldEnd = Math.min(oldLines.length, oldLines.length - suffix + 3);
  const newEnd = Math.min(newLines.length, newLines.length - suffix + 3);
  const emit = (sign: string, line: string) => sign + line +
    (line.endsWith("\n") ? "" : "\n\\ No newline at end of file\n");
  let diff = `--- ${name}\n+++ ${name}\n@@ -${oldEnd === start ? start : start + 1},${oldEnd - start} +${newEnd === start ? start : start + 1},${newEnd - start} @@\n`;
  for (let i = start; i < prefix; i++) diff += emit(" ", oldLines[i]);
  for (let i = prefix; i < oldLines.length - suffix; i++) diff += emit("-", oldLines[i]);
  for (let i = prefix; i < newLines.length - suffix; i++) diff += emit("+", newLines[i]);
  for (let i = oldLines.length - suffix; i < oldEnd; i++) diff += emit(" ", oldLines[i]);
  return diff;
}
export function lineRange(text: string, start: unknown, end: unknown) {
  if (
    typeof start !== "number" ||
    typeof end !== "number" ||
    !Number.isSafeInteger(start) ||
    !Number.isSafeInteger(end) ||
    start < 1 ||
    end < start
  )
    throw Error(
      "Expected positive integer line numbers with start_line <= end_line.",
    );
  if (end - start + 1 > 200)
    throw Error("Range exceeds 200 lines; request a narrower range.");
  const lines = (text.match(/[^\r\n]*(?:\r\n|\r|\n|$)/g) || []).filter(Boolean);
  if (start > lines.length && (lines.length || start !== 1))
    throw Error("start_line is beyond the end of the file.");
  const content = lines.slice(start - 1, end).join("");
  if ([...content].length > 12000)
    throw Error("Range exceeds 12000 characters; request a narrower range.");
  return {
    start_line: lines.length ? start : 0,
    end_line: Math.min(end, lines.length),
    total_lines: lines.length,
    content,
  };
}
export function validateEditorArguments(
  name: string,
  args: Record<string, unknown>,
) {
  if (name === "batch_inspect") {
    if (Object.keys(args).length !== 1 || !Object.hasOwn(args, "requests")) throw Error("Expected batch requests.");
    validateBatch(args.requests);
    return;
  }
  const fields: Record<string, string[]> = {
    list_files: ["path"],
    read_file: ["path"],
    file_snapshot: ["path"],
    read_file_range: ["path", "start_line", "end_line"],
    search_text: ["path", "query"],
    write_file: ["path", "content"],
    preview_edit: ["path", "expected_sha256", "old_text", "new_text"],
    apply_edit: ["path", "expected_sha256", "old_text", "new_text"],
  };
  const expected = Object.hasOwn(fields, name) ? fields[name] : undefined;
  if (
    !expected ||
    Object.keys(args).length !== expected.length ||
    expected.some((key) => !Object.hasOwn(args, key))
  )
    throw Error("Missing or unknown editor tool arguments.");
  for (const key of expected) {
    if (
      name === "read_file_range" &&
      ["start_line", "end_line"].includes(key)
    ) {
      const value = args[key];
      if (
        typeof value !== "number" ||
        !Number.isSafeInteger(value) ||
        value < 1
      )
        throw Error("Expected positive integer line numbers.");
    } else if (typeof args[key] !== "string")
      throw Error("Expected string argument.");
  }
}
export function validateBatch(value: unknown): asserts value is { path: string; start_line: number; end_line: number }[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 10) throw Error("Batch inspection requires 1–10 ranges.");
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw Error("Invalid batch range.");
    validateEditorArguments("read_file_range", item);
    if (!item.path.length || item.path.length > 1024 || item.end_line < item.start_line || item.end_line - item.start_line >= 200)
      throw Error("Invalid batch range or path limit.");
  }
}
export function relativePath(root: string, name: string): string {
  if (
    typeof name !== "string" ||
    name.includes("\0") ||
    name.split(/[\\/]/).some(protectedPart)
  )
    throw Error("Protected or invalid path.");
  const result = path.resolve(root, name);
  const rel = path.relative(root, result);
  if (rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel))
    throw Error("Path is outside selected workspace.");
  return result;
}
export function validateText(text: string): void {
  if (
    text.length > 100000 ||
    text.includes("\0") ||
    Buffer.from(text, "utf8").toString("utf8") !== text
  )
    throw Error("File is not eligible UTF-8 text.");
}
export function replaceUnique(
  text: string,
  oldText: string,
  newText: string,
): string {
  const index = text.indexOf(oldText);
  if (!oldText || index < 0 || text.indexOf(oldText, index + 1) >= 0)
    throw Error("Original text must occur exactly once.");
  const result =
    text.slice(0, index) + newText + text.slice(index + oldText.length);
  validateText(result);
  return result;
}
export function validateChecks(value: unknown): Record<string, Check> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw Error("Checks must be an object.");
  const out: Record<string, Check> = {};
  for (const [name, raw] of Object.entries(value)) {
    if (
      !profiles.includes(name as Profile) ||
      !raw ||
      typeof raw !== "object" ||
      Array.isArray(raw)
    )
      throw Error("Invalid check profile.");
    const item = raw as Record<string, unknown>;
    if (
      Object.keys(item).some(
        (k) => !["command", "timeout", "outputLimit"].includes(k),
      ) ||
      !Array.isArray(item.command) ||
      !item.command.length ||
      typeof item.command[0] !== "string" ||
      !item.command[0] ||
      item.command.some((x) => typeof x !== "string" || x.includes("\0"))
    )
      throw Error("Check command must be an executable and argument array.");
    if (
      item.timeout !== undefined &&
      (typeof item.timeout !== "number" ||
        !Number.isFinite(item.timeout) ||
        item.timeout <= 0)
    )
      throw Error("Invalid check timeout.");
    if (
      item.outputLimit !== undefined &&
      (typeof item.outputLimit !== "number" ||
        !Number.isSafeInteger(item.outputLimit) ||
        item.outputLimit < 1)
    )
      throw Error("Invalid output limit.");
    out[name] = item as unknown as Check;
  }
  return out;
}
export function completionContext(text: string, offset: number) {
  return {
    prefix: text.slice(Math.max(0, offset - 6000), offset),
    suffix: text.slice(offset, offset + 2000),
  };
}
export interface Stamp {
  uri: string;
  version: number;
  offset: number;
}
export const sameStamp = (a: Stamp, b: Stamp) =>
  a.uri === b.uri && a.version === b.version && a.offset === b.offset;
export function providerKey(provider: string, baseUrl: string): string {
  if (provider === "ollama" || provider === "openai-compatible") {
    if (provider === "openai-compatible" && !baseUrl)
      throw Error("A compatible API base URL is required.");
    const url = new URL(baseUrl || "http://localhost:11434");
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw Error(
        "Endpoint must be HTTP(S) without credentials, query or fragment.",
      );
    return `${provider}:${url.toString().replace(/\/$/, "")}`;
  }
  return provider;
}
