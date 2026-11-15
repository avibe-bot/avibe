#!/usr/bin/env node
"use strict";

const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const packageJson = require("../package.json");

const INSTALL_SH_URL =
  process.env.AVIBE_INSTALL_SH_URL ||
  "https://avibe.bot/install.sh";
const INSTALL_PS1_URL =
  process.env.AVIBE_INSTALL_PS1_URL ||
  "https://avibe.bot/install.ps1";

function printHelp() {
  console.log(`avibe ${packageJson.version}

NPM entrypoint for Avibe, the local-first Agent OS.

Usage:
  npx @avibe/cli        Install Avibe if needed, then start the setup wizard
  avibe                 Same as above after npm install -g @avibe/cli
  vibe                  Same as above after npm install -g @avibe/cli
  avibe install         Install or refresh the underlying avibe-os Python CLI
  avibe init            Start the setup wizard
  avibe start           Start Avibe
  avibe status          Show runtime status
  avibe doctor          Diagnose local setup issues
  avibe remote          Configure remote Web UI access
  avibe upgrade         Upgrade the underlying avibe-os Python package

After bootstrap, Avibe delegates to the real 'vibe' command.
Docs: https://docs.avibe.bot`);
}

function printVersion() {
  console.log(`avibe ${packageJson.version}`);
}

function invokedAsVibe() {
  const candidates = [process.env.AVIBE_ENTRYPOINT, process.argv0, process.argv[1]];
  return candidates.some((candidate) => {
    const invokedName = path.basename(candidate || "").toLowerCase();
    return invokedName === "vibe" || invokedName === "vibe.cmd" || invokedName === "vibe.bat" || invokedName === "vibe.exe";
  });
}

function prependPathEntries(env, entries) {
  const delimiter = path.delimiter;
  const currentPath = env.PATH || env.Path || "";
  const existing = new Set(currentPath.split(delimiter).filter(Boolean));
  const nextEntries = [];

  for (const entry of entries) {
    if (entry && !existing.has(entry)) {
      nextEntries.push(entry);
    }
  }

  return {
    ...env,
    PATH: [...nextEntries, currentPath].filter(Boolean).join(delimiter),
  };
}

function candidateBinDirs() {
  const home = os.homedir();
  const dirs = [];

  if (process.platform === "win32") {
    const profile = process.env.USERPROFILE || home;
    dirs.push(path.join(profile, ".local", "bin"));
    dirs.push(path.join(profile, ".cargo", "bin"));
  } else {
    dirs.push(path.join(home, ".local", "bin"));
    dirs.push(path.join(home, ".cargo", "bin"));
  }

  return dirs;
}

function executableNames(name) {
  if (process.platform === "win32") {
    return [`${name}.exe`, `${name}.cmd`, `${name}.bat`, name];
  }
  return [name];
}

function isExecutable(filePath) {
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile()) {
      return false;
    }
    if (process.platform === "win32") {
      return true;
    }
    fs.accessSync(filePath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function realpathOrNull(filePath) {
  try {
    return fs.realpathSync(filePath);
  } catch {
    return null;
  }
}

function isSelfWrapperCandidate(filePath) {
  const currentScriptPath = realpathOrNull(__filename);
  const candidatePath = realpathOrNull(filePath);

  if (currentScriptPath && candidatePath && currentScriptPath === candidatePath) {
    return true;
  }

  if (process.platform !== "win32") {
    return false;
  }

  const extension = path.extname(filePath).toLowerCase();
  if (extension !== ".cmd" && extension !== ".bat") {
    return false;
  }

  try {
    const content = fs.readFileSync(filePath, "utf8").toLowerCase();
    return content.includes("\\node_modules\\avibe\\bin\\avibe.js") || content.includes("/node_modules/avibe/bin/avibe.js");
  } catch {
    return false;
  }
}

function resolveFromPath(command, env = process.env) {
  const pathValue = env.PATH || env.Path || "";
  for (const dir of pathValue.split(path.delimiter)) {
    if (!dir) {
      continue;
    }
    for (const executableName of executableNames(command)) {
      const candidate = path.join(dir, executableName);
      if (isExecutable(candidate) && !isSelfWrapperCandidate(candidate)) {
        return candidate;
      }
    }
  }
  return null;
}

function findVibeBinary() {
  if (process.env.AVIBE_VIBE_BIN) {
    return process.env.AVIBE_VIBE_BIN;
  }

  const envWithCommonBins = prependPathEntries(process.env, candidateBinDirs());
  const fromPath = resolveFromPath("vibe", envWithCommonBins);
  if (fromPath) {
    return fromPath;
  }

  for (const dir of candidateBinDirs()) {
    for (const executableName of executableNames("vibe")) {
      const candidate = path.join(dir, executableName);
      if (isExecutable(candidate) && !isSelfWrapperCandidate(candidate)) {
        return candidate;
      }
    }
  }

  return null;
}

function runCommand(command, args, options = {}) {
  if (!options.shell && shouldRunThroughShell(command)) {
    return runWindowsBatchCommand(command, args, options);
  }

  const result = childProcess.spawnSync(command, args, {
    stdio: "inherit",
    shell: options.shell || false,
    env: options.env || prependPathEntries(process.env, candidateBinDirs()),
  });

  return normalizeSpawnResult(result);
}

function normalizeSpawnResult(result) {
  if (result.error) {
    throw result.error;
  }

  if (typeof result.status === "number") {
    return result.status;
  }

  return result.signal ? 1 : 0;
}

function runWindowsBatchCommand(command, args, options = {}) {
  const shellCommand = [quoteWindowsCmdArgument(command), ...args.map(quoteWindowsCmdArgument)].join(" ");
  const result = childProcess.spawnSync(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", shellCommand], {
    stdio: "inherit",
    env: options.env || prependPathEntries(process.env, candidateBinDirs()),
  });

  return normalizeSpawnResult(result);
}

function quoteWindowsCmdArgument(value) {
  const rawValue = String(value);
  const escapedValue = rawValue.replace(/"/g, '\\"');
  return `"${escapedValue}"`;
}

function shouldRunThroughShell(command) {
  if (process.platform !== "win32") {
    return false;
  }

  const extension = path.extname(command).toLowerCase();
  return extension === ".cmd" || extension === ".bat";
}

function hasCommand(command) {
  return Boolean(resolveFromPath(command, prependPathEntries(process.env, candidateBinDirs())));
}

function installWithCustomCommand() {
  if (!process.env.AVIBE_INSTALL_COMMAND) {
    return false;
  }

  const status = runCommand(process.env.AVIBE_INSTALL_COMMAND, [], { shell: true });
  if (status !== 0) {
    process.exit(status);
  }
  return true;
}

function installOnWindows() {
  const powershell = resolveFromPath("powershell", process.env) || resolveFromPath("pwsh", process.env);
  if (!powershell) {
    console.error("PowerShell is required to install Avibe on Windows.");
    console.error("Manual install: https://docs.avibe.bot/quickstart");
    process.exit(1);
  }

  const script = `irm ${JSON.stringify(INSTALL_PS1_URL)} | iex`;
  const status = runCommand(powershell, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]);
  if (status !== 0) {
    process.exit(status);
  }
}

function installOnPosix() {
  let fetchCommand;
  if (hasCommand("curl")) {
    fetchCommand = `curl -fsSL ${shellQuote(INSTALL_SH_URL)} | bash`;
  } else if (hasCommand("wget")) {
    fetchCommand = `wget -qO- ${shellQuote(INSTALL_SH_URL)} | bash`;
  } else {
    console.error("curl or wget is required to install Avibe.");
    console.error("Manual install: https://docs.avibe.bot/quickstart");
    process.exit(1);
  }

  const status = runCommand(fetchCommand, [], { shell: true });
  if (status !== 0) {
    process.exit(status);
  }
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function installVibe() {
  console.log("Installing Avibe...");

  if (installWithCustomCommand()) {
    return;
  }

  if (process.platform === "win32") {
    installOnWindows();
  } else {
    installOnPosix();
  }
}

function ensureVibeInstalled() {
  const existing = findVibeBinary();
  if (existing) {
    return existing;
  }

  installVibe();

  const installed = findVibeBinary();
  if (installed) {
    return installed;
  }

  console.error("Avibe was installed, but the 'vibe' command was not found on PATH.");
  console.error("Open a new terminal, or add the uv tool bin directory to PATH.");
  console.error("Common paths:");
  for (const dir of candidateBinDirs()) {
    console.error(`  ${dir}`);
  }
  process.exit(1);
}

function normalizeArgs(args) {
  if (args[0] === "init" || args[0] === "start") {
    return args.slice(1);
  }
  return args;
}

function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  const shouldDelegateHelpAndVersion = invokedAsVibe();

  if (!shouldDelegateHelpAndVersion && (command === "--help" || command === "-h" || command === "help")) {
    printHelp();
    return;
  }

  if (!shouldDelegateHelpAndVersion && (command === "--version" || command === "-v")) {
    printVersion();
    return;
  }

  if (command === "install") {
    installVibe();
    const vibe = findVibeBinary();
    if (vibe) {
      console.log(`Avibe is ready: ${vibe}`);
    }
    return;
  }

  const vibe = ensureVibeInstalled();
  const status = runCommand(vibe, normalizeArgs(args));
  process.exit(status);
}

main();
