const esbuild = require("esbuild");
Promise.all([
  esbuild.build({ entryPoints: ["src/filePolicy.ts"], outfile: "dist/filePolicy.cjs", bundle: true, platform: "node", target: "node20" }),
  esbuild.build({
    entryPoints: ["src/extension.ts"],
    outfile: "dist/extension.js",
    bundle: true,
    platform: "node",
    target: "node20",
    external: ["vscode"],
    sourcemap: false,
  }),
  esbuild.build({
    entryPoints: ["src/webview.ts"],
    outfile: "dist/webview.js",
    bundle: true,
    platform: "browser",
    target: "es2022",
  }),
  esbuild.build({
    entryPoints: ["src/shared.ts"],
    outfile: "dist/shared.cjs",
    bundle: true,
    platform: "node",
    target: "node20",
  }),
  esbuild.build({
    entryPoints: ["src/editor.ts"],
    outfile: "dist/editor.cjs",
    bundle: true,
    platform: "node",
    target: "node20",
    external: ["vscode"],
  }),
  esbuild.build({
    entryPoints: ["src/rpc.ts"],
    outfile: "dist/rpc.cjs",
    bundle: true,
    platform: "node",
    target: "node20",
  }),
]).catch((e) => {
  console.error(e.message);
  process.exitCode = 1;
});
