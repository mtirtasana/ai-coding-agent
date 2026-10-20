const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const root = path.resolve(__dirname, "../..");
const dest = path.join(root, "vscode/backend");
fs.mkdirSync(dest, { recursive: true });
for (const name of fs.readdirSync(dest))
  if (name.endsWith(".whl")) fs.unlinkSync(path.join(dest, name));
const result = spawnSync(
  process.env.AGENT_BUILD_PYTHON ||
    (process.platform === "win32" ? "python" : "python3"),
  ["-m", "pip", "wheel", "--no-deps", "--wheel-dir", dest, root],
  { stdio: "inherit", shell: false },
);
if (result.status !== 0) process.exit(result.status || 1);
const wheels = fs.readdirSync(dest).filter((x) => x.endsWith(".whl"));
if (wheels.length !== 1) throw Error("Expected one backend wheel");
const name = wheels[0];
fs.writeFileSync(
  path.join(dest, "manifest.json"),
  JSON.stringify({
    file: name,
    sha256: crypto
      .createHash("sha256")
      .update(fs.readFileSync(path.join(dest, name)))
      .digest("hex"),
    protocolVersion: 1,
    backendVersion: "0.8.0",
  }),
);
