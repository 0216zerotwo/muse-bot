"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const REPO_ROOT = path.resolve(__dirname, "..", "..");

function spawnAsync(cmd, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(cmd, args, options);
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

function findGitBash() {
  if (process.platform !== "win32") return "bash";
  const candidates = [
    process.env.GIT_BASH,
    "F:\\Git\\bin\\bash.exe",
    "C:\\Program Files\\Git\\bin\\bash.exe",
    "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
    path.join(process.env.LOCALAPPDATA || "", "Programs", "Git", "bin", "bash.exe"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function findPython() {
  if (process.env.MUSEBOT_PYTHON && fs.existsSync(process.env.MUSEBOT_PYTHON)) {
    return process.env.MUSEBOT_PYTHON;
  }
  const candidates = [
    path.join(REPO_ROOT, ".venv", "bin", "python"),
    path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
    path.join(REPO_ROOT, "hermes-env", "bin", "python"),
    path.join(REPO_ROOT, "hermes-env", "Scripts", "python.exe"),
    "E:\\personal_bot\\hermes-env\\Scripts\\python.exe",
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  for (const cmd of ["python3", "python"]) {
    const res = childProcess.spawnSync(cmd, ["--version"], { encoding: "utf8" });
    if (!res.error && res.status === 0) return cmd;
  }
  throw new Error("No Python interpreter found for tools tests");
}

function toPosixPath(p) {
  const normalized = p.replace(/\\/g, "/");
  if (process.platform === "win32" && /^[A-Za-z]:\//.test(normalized)) {
    return "/" + normalized[0].toLowerCase() + normalized.slice(2);
  }
  return normalized;
}

test("All Python tools support --help cleanly without tracebacks", () => {
  const py = findPython();
  const scripts = [
    "tools/setup.py",
    "tools/companion-pair.py",
    "tools/companion_api_probe.py",
    "tools/companion_ws_probe.py",
    "tools/draw_icons.py",
    "tools/make_app_icons.py",
    "tools/icon-runs/contact_sheet.py",
  ];

  for (const rel of scripts) {
    const res = childProcess.spawnSync(py, [path.join(REPO_ROOT, rel), "--help"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    });
    assert.equal(res.status, 0, `${rel} --help failed: ${res.stderr}`);
    assert.equal(res.stdout.trim().length > 0, true, `${rel} --help produced empty stdout`);
  }

  // companion_api_probe.py with no args exits 1 with usage instead of IndexError
  const noArgs = childProcess.spawnSync(py, [path.join(REPO_ROOT, "tools/companion_api_probe.py")], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  assert.equal(noArgs.status, 1);
  assert.doesNotMatch(noArgs.stderr, /Traceback|IndexError/);
});

test("companion-pair.py normalizes bare public domains to https:// and LAN/loopback hosts to http://", () => {
  const py = findPython();
  const code = [
    "import importlib.util, pathlib",
    "spec = importlib.util.spec_from_file_location('pair_mod', pathlib.Path('tools/companion-pair.py'))",
    "mod = importlib.util.module_from_spec(spec)",
    "spec.loader.exec_module(mod)",
    "assert mod.normalize_public_url('bot.example.com') == 'https://bot.example.com'",
    "assert mod.normalize_public_url('https://bot.example.com/companion.html') == 'https://bot.example.com'",
    "assert mod.normalize_public_url('192.168.1.25:8766') == 'http://192.168.1.25:8766'",
    "assert mod.normalize_public_url('127.0.0.1:8766') == 'http://127.0.0.1:8766'",
    "assert mod.normalize_public_url('localhost:8766') == 'http://localhost:8766'",
    "assert mod.normalize_public_url('100.88.1.2:8766') == 'http://100.88.1.2:8766'",
    "print('PAIR_URL_OK')",
  ].join("; ");

  const res = childProcess.spawnSync(py, ["-c", code], { cwd: REPO_ROOT, encoding: "utf8" });
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stdout, /PAIR_URL_OK/);
});

test("companion-pair.py distinguishes HTTP 401 auth rejection from offline service and sends normalized base query", async () => {
  const py = findPython();
  let mode = "ok";
  let lastUrl = "";

  const server = http.createServer((req, res) => {
    lastUrl = req.url || "";
    if (mode === "401") {
      res.writeHead(401, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ code: "ABCD-2345", link: "https://bot.example.com/companion.html#pair=ABCD-2345" }));
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  try {
    const okRun = await spawnAsync(
      py,
      [path.join(REPO_ROOT, "tools/companion-pair.py"), "bot.example.com", String(port)],
      { cwd: REPO_ROOT }
    );
    assert.equal(okRun.status, 0, okRun.stderr);
    assert.equal(lastUrl, "/api/pair/code?base=https%3A%2F%2Fbot.example.com");
    assert.match(okRun.stdout, /ABCD-2345/);

    mode = "401";
    const authFail = await spawnAsync(
      py,
      [path.join(REPO_ROOT, "tools/companion-pair.py"), "bot.example.com", String(port)],
      { cwd: REPO_ROOT }
    );
    assert.equal(authFail.status, 1);
    assert.match(authFail.stderr, /HTTP 401/);
    assert.doesNotMatch(authFail.stderr, /先确认网关在运行/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("tools/*.sh pass bash -n, --help, setup.sh extras parity, and fetch-hermes.sh partial-tree recovery", (t) => {
  const bash = findGitBash();
  if (!bash) {
    t.skip("Git Bash not found on this Windows machine");
    return;
  }

  for (const script of ["tools/fetch-hermes.sh", "tools/setup.sh", "tools/run-hermes-gateway.sh"]) {
    const syn = childProcess.spawnSync(bash, ["-n", script], { cwd: REPO_ROOT, encoding: "utf8" });
    assert.equal(syn.status, 0, `bash -n failed on ${script}: ${syn.stderr}`);

    const help = childProcess.spawnSync(bash, [script, "--help"], { cwd: REPO_ROOT, encoding: "utf8" });
    assert.equal(help.status, 0, `--help failed on ${script}: ${help.stderr}`);
    assert.match(help.stdout, /Usage:/);
  }

  // Verify setup.sh delegates cleanly to cross-platform tools/setup.py + root pyproject.toml
  const setupSh = fs.readFileSync(path.join(REPO_ROOT, "tools", "setup.sh"), "utf8");
  assert.match(setupSh, /tools\/setup\.py/);
  assert.match(setupSh, /pyproject\.toml/);
  const pyproject = fs.readFileSync(path.join(REPO_ROOT, "pyproject.toml"), "utf8");
  assert.match(pyproject, /musebot\s*=\s*"muse\.cli:main"/);

  // Verify fetch-hermes.sh repairs an interrupted partial directory (only pyproject.toml present)
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "musebot-fetch-test-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const commit = "8f5693af418256b89da17ddda18a9242f113dcf6";
  const upstreamTree = path.join(tmp, `hermes-agent-${commit}`);
  fs.mkdirSync(path.join(upstreamTree, "apps"), { recursive: true });
  fs.writeFileSync(path.join(upstreamTree, "pyproject.toml"), "[project]\nname='hermes-agent'\n", "utf8");
  fs.writeFileSync(path.join(upstreamTree, "uv.lock"), "version = 1\n", "utf8");
  fs.writeFileSync(path.join(upstreamTree, "hermes"), "#!/usr/bin/env python3\n", "utf8");
  fs.writeFileSync(path.join(upstreamTree, "apps", "skip.txt"), "skip\n", "utf8");

  const fakeArchive = path.join(tmp, "fake-hermes.tar.gz");
  const tarBuild = childProcess.spawnSync("tar", ["-czf", path.basename(fakeArchive), path.basename(upstreamTree)], {
    cwd: tmp,
    encoding: "utf8",
  });
  assert.equal(tarBuild.status, 0, tarBuild.stderr);

  const partialTarget = path.join(tmp, "hermes-target");
  fs.mkdirSync(partialTarget, { recursive: true });
  // Simulate an interrupted download that wrote pyproject.toml and died before uv.lock / hermes
  fs.writeFileSync(path.join(partialTarget, "pyproject.toml"), "partial\n", "utf8");

  const fetchRun = childProcess.spawnSync(bash, ["tools/fetch-hermes.sh"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      MUSEBOT_HERMES_TARGET: toPosixPath(partialTarget),
      MUSEBOT_HERMES_URL: pathToFileURL(fakeArchive).href,
    },
  });
  assert.equal(fetchRun.status, 0, `fetch-hermes.sh failed: ${fetchRun.stderr}\n${fetchRun.stdout}`);
  assert.equal(fs.existsSync(path.join(partialTarget, "uv.lock")), true);
  assert.equal(fs.existsSync(path.join(partialTarget, "hermes")), true);
  assert.equal(fs.existsSync(path.join(partialTarget, "apps")), false);
  assert.equal(fs.readFileSync(path.join(partialTarget, ".musebot-hermes-commit"), "utf8").trim(), commit);
});
