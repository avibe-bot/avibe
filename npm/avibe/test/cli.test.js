"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const cliPath = path.join(__dirname, "..", "bin", "avibe.js");

function makeTempEnv() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "avibe-cli-test-"));
  const bin = path.join(root, "bin");
  const home = path.join(root, "home");
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(home, { recursive: true });

  return { root, bin, home };
}

function writeFakeVibe(bin, label) {
  const vibePath = path.join(bin, process.platform === "win32" ? "vibe.cmd" : "vibe");
  if (process.platform === "win32") {
    fs.writeFileSync(vibePath, `@echo off\necho ${label} %*\n`);
  } else {
    fs.writeFileSync(vibePath, `#!/bin/sh\necho ${label} "$@"\n`);
    fs.chmodSync(vibePath, 0o755);
  }
  return vibePath;
}

function writeSelfNpmVibeShim(bin) {
  const shimPath = path.join(bin, process.platform === "win32" ? "vibe.cmd" : "vibe");
  if (process.platform === "win32") {
    fs.writeFileSync(shimPath, `@echo off\r\nset AVIBE_ENTRYPOINT=vibe\r\nnode "${cliPath}" %*\r\n`);
  } else {
    fs.symlinkSync(cliPath, shimPath);
  }
  return shimPath;
}

function runCli(args, envOverrides = {}, binPath = cliPath) {
  const env = {
    ...process.env,
    ...envOverrides,
  };

  return childProcess.spawnSync(process.execPath, [binPath, ...args], {
    encoding: "utf8",
    env,
    timeout: 5000,
  });
}

function loadCliForUnitTest(overrides = {}) {
  const source = fs.readFileSync(cliPath, "utf8").replace(/\nmain\(\);\s*$/, "\n");
  const sandbox = {
    console,
    process: {
      ...process,
      argv: [process.execPath, cliPath],
      env: { ...process.env, ...overrides.env },
      exit(code) {
        throw new Error(`process.exit(${code})`);
      },
    },
    require: Module.createRequire(cliPath),
    __dirname: path.dirname(cliPath),
    __filename: cliPath,
    module: { exports: {} },
    exports: {},
  };
  vm.runInNewContext(`${source}\nmodule.exports = { quoteWindowsCmdArgument, shouldRunThroughShell };`, sandbox, {
    filename: cliPath,
  });
  return sandbox.module.exports;
}

test("uses the public installer URLs for user-facing bootstrap", () => {
  const source = fs.readFileSync(cliPath, "utf8");

  assert.match(source, /"https:\/\/avibe\.bot\/install\.sh"/);
  assert.match(source, /"https:\/\/avibe\.bot\/install\.ps1"/);
  assert.doesNotMatch(source, /raw\.githubusercontent\.com\/avibe-bot\/avibe\/master\/install\.(?:sh|ps1)/);
});

test("prints wrapper help without installing", () => {
  const temp = makeTempEnv();
  const result = runCli(["--help"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
    AVIBE_INSTALL_COMMAND: "exit 42",
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /npx @avibe\/cli/);
  assert.match(result.stdout, /npm install -g @avibe\/cli/);
});

test("delegates help and version when invoked through vibe", () => {
  const temp = makeTempEnv();
  const runtimeBin = path.join(temp.root, "runtime-bin");
  const npmBin = path.join(temp.root, "npm-bin");
  fs.mkdirSync(runtimeBin, { recursive: true });
  fs.mkdirSync(npmBin, { recursive: true });
  writeFakeVibe(runtimeBin, "runtime-vibe");
  const npmVibeShim = writeSelfNpmVibeShim(npmBin);

  const help = runCli(["--help"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: `${npmBin}${path.delimiter}${runtimeBin}`,
  }, npmVibeShim);
  const version = runCli(["--version"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: `${npmBin}${path.delimiter}${runtimeBin}`,
  }, npmVibeShim);
  const commandHelp = runCli(["help", "remote"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: `${npmBin}${path.delimiter}${runtimeBin}`,
  }, npmVibeShim);

  assert.equal(help.status, 0);
  assert.equal(version.status, 0);
  assert.equal(commandHelp.status, 0);
  assert.match(help.stdout, /runtime-vibe --help/);
  assert.match(version.stdout, /runtime-vibe --version/);
  assert.match(commandHelp.stdout, /runtime-vibe help remote/);
});

test("delegates commands to an existing vibe binary", () => {
  const temp = makeTempEnv();
  writeFakeVibe(temp.bin, "existing-vibe");

  const result = runCli(["status", "--json"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /existing-vibe status --json/);
});

test("ignores non-executable vibe files on PATH", { skip: process.platform === "win32" }, () => {
  const temp = makeTempEnv();
  const staleBin = path.join(temp.root, "stale-bin");
  const workingBin = path.join(temp.root, "working-bin");
  fs.mkdirSync(staleBin, { recursive: true });
  fs.mkdirSync(workingBin, { recursive: true });
  fs.writeFileSync(path.join(staleBin, "vibe"), "not executable\n", { mode: 0o644 });
  writeFakeVibe(workingBin, "working-vibe");

  const result = runCli(["status"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: `${staleBin}${path.delimiter}${workingBin}`,
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /working-vibe status/);
});

test("skips its own npm vibe shim before delegating", () => {
  const temp = makeTempEnv();
  const npmBin = path.join(temp.root, "npm-bin");
  const runtimeBin = path.join(temp.root, "runtime-bin");
  fs.mkdirSync(npmBin, { recursive: true });
  fs.mkdirSync(runtimeBin, { recursive: true });
  writeSelfNpmVibeShim(npmBin);
  writeFakeVibe(runtimeBin, "runtime-vibe");

  const result = runCli(["status"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: `${npmBin}${path.delimiter}${runtimeBin}`,
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /runtime-vibe status/);
});

test("skips its own npm vibe shim in fallback bin dirs before installing", () => {
  const temp = makeTempEnv();
  const fallbackBin = path.join(temp.home, ".local", "bin");
  fs.mkdirSync(fallbackBin, { recursive: true });
  writeSelfNpmVibeShim(fallbackBin);

  const installCommand =
    process.platform === "win32"
      ? `echo @echo off> "${path.join(temp.bin, "vibe.cmd")}" && echo echo installed-vibe %*>> "${path.join(temp.bin, "vibe.cmd")}"`
      : `/bin/mkdir -p "${temp.bin}" && /usr/bin/printf '#!/bin/sh\\necho installed-vibe "$@"\\n' > "${path.join(
          temp.bin,
          "vibe"
        )}" && /bin/chmod +x "${path.join(temp.bin, "vibe")}"`;

  const result = runCli(["doctor"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
    AVIBE_INSTALL_COMMAND: installCommand,
  });

  assert.equal(result.error, undefined);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /Installing Avibe/);
  assert.match(result.stdout, /installed-vibe doctor/);
});

test("installs vibe when missing, then delegates", () => {
  const temp = makeTempEnv();
  const installCommand =
    process.platform === "win32"
      ? `echo @echo off> "${path.join(temp.bin, "vibe.cmd")}" && echo echo installed-vibe %*>> "${path.join(temp.bin, "vibe.cmd")}"`
      : `/bin/mkdir -p "${temp.bin}" && /usr/bin/printf '#!/bin/sh\\necho installed-vibe "$@"\\n' > "${path.join(
          temp.bin,
          "vibe"
        )}" && /bin/chmod +x "${path.join(temp.bin, "vibe")}"`;

  const result = runCli(["doctor"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
    AVIBE_INSTALL_COMMAND: installCommand,
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /Installing Avibe/);
  assert.match(result.stdout, /installed-vibe doctor/);
});

test("maps init and start to the default vibe command", () => {
  const temp = makeTempEnv();
  writeFakeVibe(temp.bin, "default-vibe");

  const init = runCli(["init"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
  });
  const start = runCli(["start"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: temp.bin,
  });

  assert.equal(init.status, 0);
  assert.equal(start.status, 0);
  assert.match(init.stdout, /default-vibe\s*$/m);
  assert.match(start.stdout, /default-vibe\s*$/m);
});

test("runs Windows batch wrappers through a shell", { skip: process.platform !== "win32" }, () => {
  const { shouldRunThroughShell } = loadCliForUnitTest();

  assert.equal(shouldRunThroughShell("C:\\Users\\alex\\.local\\bin\\vibe.cmd"), true);
  assert.equal(shouldRunThroughShell("C:\\Users\\alex\\.local\\bin\\vibe.bat"), true);
  assert.equal(shouldRunThroughShell("C:\\Users\\alex\\.local\\bin\\vibe.exe"), false);
});

test("quotes Windows batch wrapper paths with spaces", { skip: process.platform !== "win32" }, () => {
  const { quoteWindowsCmdArgument } = loadCliForUnitTest();

  assert.equal(
    quoteWindowsCmdArgument("C:\\Users\\Jane Doe\\.local\\bin\\vibe.cmd"),
    '"C:\\Users\\Jane Doe\\.local\\bin\\vibe.cmd"'
  );
  assert.equal(quoteWindowsCmdArgument('C:\\bin\\vibe "dev".cmd'), '"C:\\bin\\vibe \\"dev\\".cmd"');
});

test("delegates to Windows batch wrappers in paths with spaces", { skip: process.platform !== "win32" }, () => {
  const temp = makeTempEnv();
  const spacedBin = path.join(temp.root, "space dir");
  fs.mkdirSync(spacedBin, { recursive: true });
  writeFakeVibe(spacedBin, "spaced-vibe");

  const result = runCli(["status", "--json"], {
    HOME: temp.home,
    USERPROFILE: temp.home,
    PATH: spacedBin,
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /spaced-vibe status --json/);
});
