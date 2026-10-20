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
  [".git", ".ssh", ".aws"].includes(p) || p.startsWith(".env");
export const sha = (s: string | Buffer) =>
  createHash("sha256").update(s).digest("hex");
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
