import * as fs from "node:fs/promises";
import * as path from "node:path";
import ignore from "ignore";
import { protectedPart } from "./shared";

export class FilePolicy {
  constructor(private root: string) {}
  private async rules(directory: string, name: string) {
    const file = path.join(directory, name);
    try {
      const info = await fs.lstat(file);
      if (info.isSymbolicLink() || info.size > 262144) throw Error("Invalid ignore policy file.");
      const text = new TextDecoder("utf-8", { fatal: true }).decode(await fs.readFile(file));
      if ([...text].length > 65536) throw Error("Ignore policy is too large.");
      return ignore().add(text);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return ignore();
      throw error;
    }
  }
  async denied(relative: string, directory = false) {
    const name = relative.split(path.sep).join("/");
    if (!name || name === ".") return false;
    if (name.split("/").some(protectedPart)) return true;
    return (await this.rules(this.root, ".agentignore")).ignores(name + (directory ? "/" : ""));
  }
  async ignored(relative: string, directory = false) {
    if (await this.denied(relative, directory)) return true;
    const parts = relative.split(/[\\/]/).filter(Boolean);
    for (let end = 1; end <= parts.length; end++) {
      let excluded = false;
      for (let depth = 0; depth < end; depth++) {
        const name = parts.slice(depth, end).join("/") + (end < parts.length || directory ? "/" : "");
        const result = (await this.rules(path.join(this.root, ...parts.slice(0, depth)), ".gitignore")).test(name);
        if (result.ignored) excluded = true;
        if (result.unignored) excluded = false;
      }
      if (excluded) return true;
    }
    return false;
  }
}
