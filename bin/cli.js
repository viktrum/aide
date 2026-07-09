#!/usr/bin/env node
/* agentaide — installs AIDE (real-time coach for Claude Code).
 * Copies the bundled Python hooks to ~/.aide and registers them in
 * ~/.claude/settings.json (backed up first). Everything stays local. */
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const HOME = process.env.HOME || os.homedir();
const AIDE_HOME = process.env.AIDE_HOME || path.join(HOME, ".aide");
const PKG_ROOT = path.resolve(__dirname, "..");

const say = (m) => console.log(`\x1b[1;32m[aide]\x1b[0m ${m}`);
const fail = (m) => { console.error(`\x1b[1;31m[aide]\x1b[0m ${m}`); process.exit(1); };

if (!["darwin", "linux"].includes(process.platform)) {
  fail(`Unsupported OS: ${process.platform}. AIDE currently supports macOS and Linux.`);
}
const py = spawnSync("python3", ["-c",
  "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"]);
if (py.error || py.status !== 0) {
  fail("Python 3.9+ is required. Install it (macOS: xcode-select --install / brew install python3) and re-run.");
}

const installer = path.join(AIDE_HOME, "prototype", "judge", "install_hooks.py");
const run = (args) => spawnSync("python3", args, { stdio: "inherit", env: process.env });

if (process.argv.includes("--uninstall")) {
  if (!fs.existsSync(installer)) { say(`Nothing installed at ${AIDE_HOME}.`); process.exit(0); }
  const r = run([installer, "--uninstall"]);
  say("Hooks removed. Your data is untouched.");
  say(`To delete everything, remove '${AIDE_HOME}' and '${path.join(HOME, ".claude-judge")}'.`);
  process.exit(r.status ?? 1);
}

say(`Installing AIDE into ${AIDE_HOME}`);
for (const dir of ["prototype", "prompts", "docs"]) {
  const src = path.join(PKG_ROOT, dir);
  if (fs.existsSync(src)) fs.cpSync(src, path.join(AIDE_HOME, dir), { recursive: true });
}
if (run([installer]).status !== 0) fail("Hook registration failed.");
console.log("");
run([path.join(AIDE_HOME, "prototype", "judge", "doctor.py")]);
console.log("");
say("Done. Open a new Claude Code session — AIDE is active.");
say("Health check:  /aide status");
say("Bypass once:   prefix a prompt with *");
say("Uninstall:     npx agentaide --uninstall");
