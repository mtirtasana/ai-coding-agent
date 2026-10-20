// test-electron expects npm proxy variables; preserve the configured session proxy.
if (!process.env.npm_config_https_proxy && process.env.HTTPS_PROXY)
  process.env.npm_config_https_proxy = process.env.HTTPS_PROXY;
if (!process.env.npm_config_proxy && process.env.HTTP_PROXY)
  process.env.npm_config_proxy = process.env.HTTP_PROXY;
const {
  runTests,
  downloadAndUnzipVSCode,
  resolveCliPathFromVSCodeExecutablePath,
} = require("@vscode/test-electron");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "agent-vscode-native-"));
const version = process.env.VSCODE_TEST_VERSION || "stable";
(async () => {
  let executable = await downloadAndUnzipVSCode({ version });
  // Recent macOS bundles can rename the executable; Info.plist is authoritative.
  if (process.platform === "darwin") {
    const contents = path.dirname(path.dirname(executable));
    const info = spawnSync(
      "/usr/bin/plutil",
      [
        "-extract",
        "CFBundleExecutable",
        "raw",
        "-o",
        "-",
        path.join(contents, "Info.plist"),
      ],
      { encoding: "utf8", shell: false },
    );
    const name = info.stdout?.trim();
    if (info.status !== 0 || !name || path.basename(name) !== name)
      throw Error("Cannot resolve the VS Code application executable.");
    executable = path.join(contents, "MacOS", name);
    if (!fs.existsSync(executable))
      throw Error("VS Code bundle is missing its declared executable: " + name);
  }
  const vsix = path.join(root, "generic-coding-agent-0.2.1.vsix");
  if (fs.existsSync(vsix)) {
    const cli = resolveCliPathFromVSCodeExecutablePath(executable);
    const result = spawnSync(
      cli,
      [
        "--install-extension",
        vsix,
        "--force",
        "--extensions-dir",
        path.join(fixture, "extensions"),
        "--user-data-dir",
        path.join(fixture, "user-data"),
        "--no-sandbox",
      ],
      { encoding: "utf8", shell: process.platform === "win32", timeout: 60000 },
    );
    if (result.status !== 0)
      throw Error(
        "Packaged VSIX installation failed: " +
          result.stdout +
          " " +
          result.stderr,
      );
    console.log("Packaged VSIX installation passed.");
  }
  await runTests({
    vscodeExecutablePath: executable,
    extensionDevelopmentPath: root,
    extensionTestsPath: path.join(root, "test/native.cjs"),
    launchArgs: [
      fixture,
      "--disable-workspace-trust",
      "--no-sandbox",
      "--disable-gpu",
      "--skip-welcome",
      "--skip-release-notes",
      "--user-data-dir",
      path.join(fixture, "test-user"),
      "--extensions-dir",
      path.join(fixture, "test-extensions"),
    ],
    extensionTestsEnv: { AGENT_NATIVE_ROOT: fixture },
  });
})().then(
  () => {
    fs.rmSync(fixture, { recursive: true, force: true });
  },
  (error) => {
    console.error(
      "VS Code integration failed:",
      error.name,
      error.code || "",
      error.message ||
        "Download or launch failed; check network/proxy and display availability.",
    );
    fs.rmSync(fixture, { recursive: true, force: true });
    process.exitCode = 1;
  },
);
