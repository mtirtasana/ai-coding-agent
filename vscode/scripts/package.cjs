const { spawnSync } = require("node:child_process");
const path = require("node:path");
const result = spawnSync(
  process.env.AGENT_BUILD_PYTHON ||
    (process.platform === "win32" ? "python" : "python3"),
  [path.join(__dirname, "package.py")],
  { cwd: path.resolve(__dirname, ".."), stdio: "inherit", shell: false },
);
process.exitCode = result.status || 0;
