import * as path from "node:path";
import { createHash } from "node:crypto";
export type Profile = "test" | "lint" | "security" | "dependency_audit";
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
