// Requiring test modules directly uses node:test's in-process runner on Node 22+.
// This also works in restricted hosts that cannot use child test-process isolation.
const fs = require("node:fs");
const path = require("node:path");
const directory = path.resolve(__dirname, "../test");
for (const file of fs
  .readdirSync(directory)
  .filter((name) => name.endsWith(".test.cjs"))
  .sort()) {
  require(path.join(directory, file));
}
