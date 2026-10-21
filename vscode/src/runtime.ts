import * as vscode from "vscode";
import * as path from "node:path";
import * as fs from "node:fs/promises";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { sha } from "./shared";
export class Runtime {
  constructor(private context: vscode.ExtensionContext) {}
  private get directory() {
    const root = this.context.globalStorageUri.fsPath;
    try {
      const active = JSON.parse(readFileSync(path.join(root, "backend-0.12.0-active.json"), "utf8"));
      if (typeof active.directory !== "string" || !/^backend-0\.12\.0-[a-f0-9-]{36}$/.test(active.directory)) throw Error("Invalid runtime pointer.");
      return path.join(root, active.directory);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      return path.join(root, "backend-0.12.0");
    }
  }
  get python() {
    return path.join(
      this.directory,
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
  }
  private async manifest() {
    const value: unknown = JSON.parse(
      await fs.readFile(
        path.join(this.context.extensionPath, "backend/manifest.json"),
        "utf8",
      ),
    );
    if (!value || typeof value !== "object")
      throw Error("Backend wheel is missing; use the packaged VSIX.");
    const m = value as Record<string, unknown>;
    if (
      typeof m.file !== "string" ||
      path.basename(m.file) !== m.file ||
      typeof m.sha256 !== "string"
    )
      throw Error("Invalid bundled backend manifest.");
    return m as { file: string; sha256: string; locks?: Record<string, string> };
  }
  async ready(provider: string) {
    try {
      await fs.access(this.python);
      const expected = await this.manifest();
      const marker = JSON.parse(
        await fs.readFile(path.join(this.directory, "ready.json"), "utf8"),
      ) as { sha256?: string; sdk?: boolean };
      if (
        marker.sha256 !== expected.sha256 ||
        (["openai", "openai-compatible"].includes(provider) && !marker.sdk)
      )
        throw Error();
      return this.python;
    } catch {
      throw Error("Run “Coding Agent: Set Up / Update Backend” first.");
    }
  }
  async setup(resource?: vscode.Uri) {
    if (!vscode.workspace.isTrusted)
      throw Error("Trust the workspace before backend setup.");
    const choice = await vscode.window.showWarningMessage(
      "Create/update the managed Python environment and install the bundled backend plus provider dependencies? This may download packages.",
      "Set up backend",
    );
    if (choice !== "Set up backend") return;
    const cfg = vscode.workspace.getConfiguration("genericAgent", resource);
    const configured = cfg.get<string>("pythonPath") || "";
    const python = await vscode.window.showInputBox({
      title: "Python 3.11+ executable on the workspace host",
      value:
        configured || (process.platform === "win32" ? "python" : "python3"),
      ignoreFocusOut: true,
    });
    if (!python) return;
    const manifest = await this.manifest();
    const wheel = path.join(
      this.context.extensionPath,
      "backend",
      manifest.file,
    );
    if (sha(await fs.readFile(wheel)) !== manifest.sha256)
      throw Error("Bundled wheel integrity check failed.");
    for (const file of ["runtime-base.txt", "runtime-openai.txt", "dependencies.json"])
      if (sha(await fs.readFile(path.join(this.context.extensionPath, "backend", file))) !== manifest.locks?.[file])
        throw Error("Bundled runtime lock integrity check failed.");
    const sdk = ["openai", "openai-compatible"].includes(
      cfg.get<string>("provider") || "demo",
    );
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Setting up Coding Agent backend",
      },
      async () => {
        const storage = this.context.globalStorageUri.fsPath;
        await fs.mkdir(storage, {
          recursive: true,
        });
        const lock = path.join(storage, "backend-setup.lock");
        const handle = await fs.open(lock, "wx", 0o600).catch(() => { throw Error("Backend setup is already running, or a stale backend-setup.lock remains. Stop other setup processes before removing it."); });
        const candidate = path.join(storage, "backend-0.12.0-" + randomUUID());
        const candidatePython = path.join(candidate, process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
        let committed = false;
        let temporary: string | undefined;
        try {
        await this.command(python, [
          "-I",
          "-c",
          "import sys; assert (3,11) <= sys.version_info[:2] <= (3,14)",
        ]);
        await this.command(python, ["-I", "-m", "venv", candidate]);
        const requirements = path.join(this.context.extensionPath, "backend", sdk ? "runtime-openai.txt" : "runtime-base.txt");
        await this.command(candidatePython, ["-I", "-m", "pip", "install", "--require-hashes", "--only-binary=:all:", "-r", requirements]);
        await this.command(candidatePython, [
          "-I",
          "-m",
          "pip",
          "install",
          "--no-deps",
          wheel,
        ]);
        await this.command(candidatePython, [
          "-I",
          "-c",
          "import agent.server; assert agent.server.PROTOCOL_VERSION == 2",
        ]);
        await fs.writeFile(
          path.join(candidate, "ready.json"),
          JSON.stringify({ sha256: manifest.sha256, sdk }),
        );
        temporary = path.join(storage, ".active-" + randomUUID());
        let previous = "unavailable";
        try { previous = path.basename(this.directory); } catch { previous = "invalid-pointer-preserved-as-old-directory"; }
        await fs.writeFile(temporary, JSON.stringify({ directory: path.basename(candidate), previous }), { mode: 0o600 });
        await fs.rename(temporary, path.join(storage, "backend-0.12.0-active.json"));
        temporary = undefined;
        committed = true;
        } finally {
          await handle.close();
          await fs.rm(lock, { force: true });
          if (temporary) await fs.rm(temporary, { force: true });
          if (!committed) await fs.rm(candidate, { recursive: true, force: true });
        }
      },
    );
    void vscode.window.showInformationMessage("Coding Agent backend is ready.");
  }
  private command(binary: string, args: string[]): Promise<void> {
    return new Promise((resolve, reject) => {
      const child = spawn(binary, args, {
        cwd: this.context.globalStorageUri.fsPath,
        stdio: "ignore",
        shell: false,
        windowsHide: true,
      });
      let finished = false;
      const done = (error?: Error) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        if (error) reject(error);
        else resolve();
      };
      const timer = setTimeout(() => {
        child.kill();
        done(
          Error(
            "Backend setup timed out. Check network/proxy access and Python, then retry setup.",
          ),
        );
      }, 300000);
      child.on("error", () =>
        done(
          Error(
            "Could not launch Python. Select a valid Python 3.11+ executable.",
          ),
        ),
      );
      child.on("exit", (code) =>
        done(
          code === 0
            ? undefined
            : Error(
                "Backend installation failed. Check Python, network/proxy access and package compatibility, then retry.",
              ),
        ),
      );
    });
  }
}
