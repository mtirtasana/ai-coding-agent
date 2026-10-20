import * as vscode from "vscode";
import * as path from "node:path";
import * as fs from "node:fs/promises";
import { spawn } from "node:child_process";
import { sha } from "./shared";
export class Runtime {
  constructor(private context: vscode.ExtensionContext) {}
  private get directory() {
    return path.join(this.context.globalStorageUri.fsPath, "backend-0.8.0");
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
    return m as { file: string; sha256: string };
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
    const sdk = ["openai", "openai-compatible"].includes(
      cfg.get<string>("provider") || "demo",
    );
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Setting up Coding Agent backend",
      },
      async () => {
        await fs.mkdir(this.context.globalStorageUri.fsPath, {
          recursive: true,
        });
        await this.command(python, [
          "-I",
          "-c",
          "import sys; assert sys.version_info >= (3,11)",
        ]);
        await fs.rm(path.join(this.directory, "ready.json"), { force: true });
        await this.command(python, ["-I", "-m", "venv", this.directory]);
        await this.command(this.python, [
          "-I",
          "-m",
          "pip",
          "install",
          "--upgrade",
          "pip",
          "setuptools",
        ]);
        await this.command(this.python, [
          "-I",
          "-m",
          "pip",
          "install",
          "--upgrade",
          wheel + (sdk ? "[openai]" : ""),
        ]);
        await this.command(this.python, [
          "-I",
          "-c",
          "import agent.server; assert agent.server.PROTOCOL_VERSION == 1",
        ]);
        await fs.writeFile(
          path.join(this.directory, "ready.json"),
          JSON.stringify({ sha256: manifest.sha256, sdk }),
        );
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
