"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const musebot = require("../npm/bin/musebot.js");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const CLI_PATH = path.join(REPO_ROOT, "deploy", "npm", "bin", "musebot.js");
const PKG_VERSION = require("../npm/package.json").version;

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
  if (process.env.MUSEBOT_PYTHON) {
    assert.equal(fs.existsSync(process.env.MUSEBOT_PYTHON), true, `MUSEBOT_PYTHON does not exist: ${process.env.MUSEBOT_PYTHON}`);
    return process.env.MUSEBOT_PYTHON;
  }
  const candidates = [
    path.join(REPO_ROOT, ".venv", "bin", "python"),
    path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  for (const cmd of ["python3", "python"]) {
    const res = childProcess.spawnSync(cmd, ["--version"], { encoding: "utf8" });
    if (!res.error && res.status === 0) return cmd;
  }
  return null;
}

function toPosixPath(p) {
  const normalized = p.replace(/\\/g, "/");
  if (process.platform === "win32" && /^[A-Za-z]:\//.test(normalized)) {
    return "/" + normalized[0].toLowerCase() + normalized.slice(2);
  }
  return normalized;
}

function runCli(args, extraEnv = {}) {
  return childProcess.spawnSync(process.execPath, [CLI_PATH, ...args], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, ...extraEnv },
  });
}

function makeTempDir(prefix = "musebot-test-") {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function writeFiles(baseDir, fileMap) {
  for (const [rel, content] of Object.entries(fileMap)) {
    const full = path.join(baseDir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content, "utf8");
  }
}

function sha256File(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function buildTarGz(stageDir, topFolder, archivePath) {
  const res = childProcess.spawnSync("tar", ["-czf", path.basename(archivePath), topFolder], {
    cwd: stageDir,
    encoding: "utf8",
  });
  assert.equal(res.status, 0, `tar failed: ${res.stderr}`);
}

const VALID_BUNDLE_FILES = {
  "musebot-server-v2.0.0/install.sh": "#!/usr/bin/env bash\necho ok\n",
  "musebot-server-v2.0.0/uninstall.sh": "#!/usr/bin/env bash\necho ok\n",
  "musebot-server-v2.0.0/Dockerfile": "FROM debian:13-slim\n",
  "musebot-server-v2.0.0/LICENSE": "MIT\n",
  "musebot-server-v2.0.0/NOTICE": "Muse Bot\n",
  "musebot-server-v2.0.0/pyproject.toml": "[project]\nname='muse-bot'\n",
  "musebot-server-v2.0.0/uv.lock": "version = 1\n",
  "musebot-server-v2.0.0/muse/__init__.py": "__version__ = '2.0.0'\n",
  "musebot-server-v2.0.0/muse/.env.example": "OPENAI_API_KEY=\n",
  "musebot-server-v2.0.0/muse/.envrc": "dotenv_if_exists\n",
  "musebot-server-v2.0.0/muse/tools/data/readme.txt": "nested data folder inside muse source\n",
  "musebot-server-v2.0.0/companion/server.py": "app = None\n",
  "musebot-server-v2.0.0/web/companion.html": "<!doctype html><title>Muse Bot</title>\n",
  "musebot-server-v2.0.0/tools/companion-pair.py": "print('pair')\n",
};

test("CLI --help and -h are non-destructive across every subcommand", (t) => {
  const fakeApp = makeTempDir("musebot-app-");
  t.after(() => fs.rmSync(fakeApp, { recursive: true, force: true }));

  const marker = path.join(fakeApp, "ran-uninstall");
  fs.writeFileSync(path.join(fakeApp, ".installed"), "", "utf8");
  fs.writeFileSync(
    path.join(fakeApp, "uninstall.sh"),
    `#!/usr/bin/env bash\necho boom > "${toPosixPath(marker)}"\nexit 1\n`,
    "utf8"
  );

  const subcommands = [
    ["--help"],
    ["-h"],
    ["help"],
    ["install", "--help"],
    ["install", "-h"],
    ["update", "--help"],
    ["upgrade", "--help"],
    ["uninstall", "--help"],
    ["uninstall", "--purge", "-h"],
    ["status", "--help"],
    ["logs", "--help"],
    ["restart", "--help"],
    ["stop", "--help"],
    ["start", "--help"],
    ["pair", "--help"],
    ["check-bundle", "--help"],
  ];

  for (const args of subcommands) {
    const res = runCli(args, { MUSEBOT_APP_DIR: fakeApp });
    assert.equal(res.status, 0, `Expected exit 0 for ${args.join(" ")}: ${res.stderr}`);
    assert.match(res.stdout, /Muse Bot 服务器/);
    assert.equal(fs.existsSync(marker), false, `Subcommand ${args.join(" ")} must not run side effects`);
  }
});

test("CLI rejects unknown commands and unknown flags cleanly", () => {
  const badCmd = runCli(["nonexistent-subcommand"]);
  assert.equal(badCmd.status, 1);
  assert.match(badCmd.stderr, /未知命令/);

  for (const args of [
    ["install", "--unknown-flag"],
    ["uninstall", "--purg"],
    ["logs", "--unknown"],
    ["status", "--unknown"],
    ["check-bundle", "--unknown"],
  ]) {
    const res = runCli(args);
    assert.equal(res.status, 1, `Expected exit 1 for ${args.join(" ")}`);
    assert.match(res.stderr, /未知选项/);
  }
});

test("CLI version prints installer version and optional installed server VERSION", (t) => {
  const emptyApp = makeTempDir("musebot-ver-empty-");
  const installedApp = makeTempDir("musebot-ver-srv-");
  t.after(() => {
    fs.rmSync(emptyApp, { recursive: true, force: true });
    fs.rmSync(installedApp, { recursive: true, force: true });
  });

  const plain = runCli(["version"], { MUSEBOT_APP_DIR: emptyApp });
  assert.equal(plain.status, 0);
  assert.equal(plain.stdout.trim(), PKG_VERSION);

  fs.writeFileSync(path.join(installedApp, "VERSION"), "v2.0.0\n", "utf8");
  const withServer = runCli(["--version"], { MUSEBOT_APP_DIR: installedApp });
  assert.equal(withServer.status, 0);
  assert.equal(withServer.stdout.trim(), `musebot-server ${PKG_VERSION} (server: v2.0.0)`);
});

test("check-bundle accepts clean v2.0 bundle and rejects secrets, DBs, homes, or missing files", (t) => {
  const tmp = makeTempDir("musebot-bundle-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  writeFiles(tmp, VALID_BUNDLE_FILES);
  const cleanArchive = path.join(tmp, "clean.tar.gz");
  buildTarGz(tmp, "musebot-server-v2.0.0", cleanArchive);

  const okRes = runCli(["check-bundle", cleanArchive]);
  assert.equal(okRes.status, 0, okRes.stderr);
  assert.match(okRes.stdout, /安装包齐全，没有数据和密钥/);

  // Missing required v2.0 files must fail
  for (const requiredRel of ["LICENSE", "pyproject.toml", "uv.lock", "muse/__init__.py", "companion/server.py"]) {
    const missingDir = makeTempDir("musebot-missing-");
    t.after(() => fs.rmSync(missingDir, { recursive: true, force: true }));
    const withoutFile = { ...VALID_BUNDLE_FILES };
    delete withoutFile[`musebot-server-v2.0.0/${requiredRel}`];
    writeFiles(missingDir, withoutFile);
    const missingArchive = path.join(missingDir, "missing.tar.gz");
    buildTarGz(missingDir, "musebot-server-v2.0.0", missingArchive);
    const missingRes = runCli(["check-bundle", missingArchive]);
    assert.equal(missingRes.status, 1, `Expected check-bundle to fail when missing ${requiredRel}`);
    assert.match(missingRes.stderr, new RegExp(`缺 ${requiredRel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
  }

  // Forbidden secret, DB, and home paths
  const forbiddenCases = [
    "musebot-server-v2.0.0/data/config.yaml",
    "musebot-server-v2.0.0/muse-home/config.yaml",
    "musebot-server-v2.0.0/hermes-home/config.yaml",
    "musebot-server-v2.0.0/.env",
    "musebot-server-v2.0.0/companion/.env.local",
    "musebot-server-v2.0.0/muse/.env-prod",
    "musebot-server-v2.0.0/vault/items.json",
    "musebot-server-v2.0.0/companion/media.key",
    "musebot-server-v2.0.0/companion/vapid_private.pem",
    "musebot-server-v2.0.0/muse.db",
    "musebot-server-v2.0.0/muse.db-wal",
    "musebot-server-v2.0.0/muse.db-shm",
    "musebot-server-v2.0.0/state.db",
    "musebot-server-v2.0.0/state.db-wal",
    "musebot-server-v2.0.0/state.db-shm",
    "musebot-server-v2.0.0/projects.db",
    "musebot-server-v2.0.0/signing/companion.jks",
    "musebot-server-v2.0.0/signing/keystore-password.txt",
  ];
  for (const badPath of forbiddenCases) {
    const rel = badPath.replace(/^[^/]+\//, "");
    assert.equal(musebot.isForbiddenBundlePath(rel), true, `Expected forbidden: ${rel}`);
  }
  assert.equal(musebot.isForbiddenBundlePath("muse/.env.example"), false);
  assert.equal(musebot.isForbiddenBundlePath("muse/.envrc"), false);
  assert.equal(musebot.isForbiddenBundlePath("muse/tools/data/readme.txt"), false);
  assert.equal(musebot.isForbiddenBundlePath("muse/db.py"), false);

  for (const leakyEntry of [
    "musebot-server-v2.0.0/companion/.env.production",
    "musebot-server-v2.0.0/muse.db-wal",
    "musebot-server-v2.0.0/muse-home/auth.json",
  ]) {
    const leakyDir = makeTempDir("musebot-leaky-");
    t.after(() => fs.rmSync(leakyDir, { recursive: true, force: true }));
    writeFiles(leakyDir, {
      ...VALID_BUNDLE_FILES,
      [leakyEntry]: "SECRET=1\n",
    });
    const leakyArchive = path.join(leakyDir, "leaky.tar.gz");
    buildTarGz(leakyDir, "musebot-server-v2.0.0", leakyArchive);
    const leakyRes = runCli(["check-bundle", leakyArchive]);
    assert.equal(leakyRes.status, 1, `Expected check-bundle to reject ${leakyEntry}`);
    assert.match(leakyRes.stderr, /不该公开的文件/);
  }
});

test("Path traversal & SHA-256 checksum helpers enforce strict integrity", (t) => {
  assert.equal(musebot.isUnsafeArchiveEntry("../etc/passwd"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("musebot/../../etc/shadow"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("/etc/passwd"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("C:/Windows/win.ini"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("musebot-server-v2.0.0/install.sh"), false);

  const tmp = makeTempDir("musebot-sha-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const sample = path.join(tmp, "musebot-server.tar.gz");
  fs.writeFileSync(sample, "hello musebot release payload", "utf8");
  const expected = crypto.createHash("sha256").update("hello musebot release payload").digest("hex");

  assert.equal(musebot.computeFileSha256(sample), expected);
  assert.equal(
    musebot.parseSha256Text(`${"0".repeat(64)}  other.tar.gz\n${expected}  musebot-server.tar.gz\n`, "musebot-server.tar.gz"),
    expected
  );
  assert.equal(musebot.parseSha256Text(`${expected} *musebot-server.tar.gz\n`, "musebot-server.tar.gz"), expected);
  assert.equal(musebot.parseSha256Text("not-a-valid-sha256", "musebot-server.tar.gz"), null);
  assert.doesNotThrow(() => musebot.verifyArchiveSha256(sample, `${expected}  musebot-server.tar.gz\n`));
});

test("Dockerfile, VPS shell scripts, and host companion-pair.py enforce v2.0 invariants", () => {
  const dockerfile = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "Dockerfile"), "utf8");
  const installSh = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "install.sh"), "utf8");
  const uninstallSh = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "uninstall.sh"), "utf8");
  const pairPy = fs.readFileSync(path.join(REPO_ROOT, "tools", "companion-pair.py"), "utf8");

  // Dockerfile invariants (v2.0 pure native runtime on Python 3.14)
  assert.match(dockerfile, /PYTHONUNBUFFERED=1/);
  assert.match(dockerfile, /@playwright\/mcp@0\.0\.83/);
  assert.match(dockerfile, /uv python install 3\.14/);
  assert.match(dockerfile, /uv sync --frozen --no-dev/);
  assert.doesNotMatch(dockerfile, /--inexact/);
  assert.doesNotMatch(dockerfile, /install-stamp\.json/);
  assert.doesNotMatch(dockerfile, /COPY hermes\//);
  assert.match(dockerfile, /import py_vapid, http_ece, anthropic, sqlite3/);
  assert.match(dockerfile, /sqlite_version_info >= \(3, 51, 3\)/);
  assert.match(dockerfile, /tokenize=['"]?trigram/);
  assert.match(dockerfile, /\/usr\/local\/bin\/gateway/);
  assert.match(dockerfile, /exec \/opt\/venv\/bin\/musebot serve "\$@"/);
  assert.match(dockerfile, /MUSE_HOME=\/opt\/data/);
  assert.match(dockerfile, /HERMES_HOME=\/opt\/data/);
  assert.match(dockerfile, /CMD \["musebot", "serve"\]/);

  // install.sh invariants (v2.0 runtime + dual .prev rollback + muse.db 0600)
  assert.match(installSh, /command: \["musebot", "serve"\]/);
  assert.match(installSh, /docker build -t companion-hermes:next/);
  assert.match(installSh, /rollback_upgrade/);
  assert.match(installSh, /docker-compose\.yml\.prev/);
  assert.match(installSh, /plugins\/companion\.prev/);
  assert.match(installSh, /chmod 600 "\$DATA\/muse\.db"/);
  assert.match(installSh, /boot_code=.*"200".*"401"/s);
  assert.match(installSh, /chmod 700 "\$DATA"/);
  assert.match(installSh, /chmod 600 "\$secret_file"/);
  assert.match(installSh, /Strict-Transport-Security/);
  assert.doesNotMatch(installSh, /^\s*encode gzip\s*$/m);
  assert.match(installSh, /ufw allow 443\/udp/);
  assert.match(installSh, /max_turns: 100/);

  // uninstall.sh preserves caddy certs on non-purge and cleans all v2.0 build context + .prev files
  assert.match(uninstallSh, /\$APP\/\.installed/);
  assert.match(uninstallSh, /touch "\$APP\/\.uninstalled"/);
  for (const cleaned of [
    '"$APP/muse"',
    '"$APP/muse.prev"',
    '"$APP/companion"',
    '"$APP/companion.prev"',
    '"$APP/pyproject.toml"',
    '"$APP/pyproject.toml.prev"',
    '"$APP/uv.lock"',
    '"$APP/uv.lock.prev"',
    '"$APP/docker-compose.yml.prev"',
    '"$APP/LICENSE"',
    '"$APP/NOTICE"',
  ]) {
    assert.equal(uninstallSh.includes(cleaned), true, `uninstall.sh missing non-purge cleanup for ${cleaned}`);
  }
  assert.doesNotMatch(uninstallSh, /rm -rf "\$APP\/hermes" "\$APP\/caddy"/);

  // tools/companion-pair.py MUST stay 100% Python stdlib-only (runs on VPS host outside container)
  assert.doesNotMatch(pairPy, /^\s*(?:from|import)\s+(?:muse|companion|httpx|starlette|uvicorn|yaml|ruamel|requests)\b/m);
});

test("Shell syntax, --help flags, and end-to-end v2.0 pack.sh (clean & --with-data) work in bash", (t) => {
  const bash = findGitBash();
  if (!bash) {
    t.skip("Git Bash not found on this Windows machine");
    return;
  }

  for (const script of ["deploy/vps/install.sh", "deploy/vps/pack.sh", "deploy/vps/uninstall.sh"]) {
    const syn = childProcess.spawnSync(bash, ["-n", script], { cwd: REPO_ROOT, encoding: "utf8" });
    assert.equal(syn.status, 0, `bash -n failed on ${script}: ${syn.stderr}`);

    const help = childProcess.spawnSync(bash, [script, "--help"], { cwd: REPO_ROOT, encoding: "utf8" });
    assert.equal(help.status, 0, `--help failed on ${script}: ${help.stderr}`);
    assert.match(help.stdout, /用法：/);
  }

  const tmp = makeTempDir("musebot-pack-e2e-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  const fakeWeb = path.join(tmp, "web-dist");
  const outDir = path.join(tmp, "out");
  writeFiles(fakeWeb, {
    "companion.html": "<!doctype html><html><body>Muse Bot v2.0</body></html>\n",
  });

  assert.equal(fs.existsSync(path.join(REPO_ROOT, "pyproject.toml")), true, "Root pyproject.toml must exist");
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "uv.lock")), true, "Root uv.lock must exist");

  // 1. Clean v2.0 release pack + check-bundle + zero hermes/ + < 6 MB gate (using real root pyproject.toml + uv.lock)
  const cleanPack = childProcess.spawnSync(bash, ["deploy/vps/pack.sh"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      MUSEBOT_VERSION: "v2.0.0-test",
      MUSEBOT_WEB_DIST: toPosixPath(fakeWeb),
      MUSEBOT_OUT_DIR: outDir.replace(/\\/g, "/"),
    },
  });
  assert.equal(cleanPack.status, 0, `clean pack.sh failed: ${cleanPack.stderr}\n${cleanPack.stdout}`);
  const cleanTar = path.join(outDir, "musebot-server-v2.0.0-test.tar.gz");
  assert.equal(fs.existsSync(cleanTar), true);
  assert.equal(fs.statSync(cleanTar).size < 6 * 1024 * 1024, true, "v2.0 bundle must stay under 6 MB");

  const checkClean = runCli(["check-bundle", cleanTar]);
  assert.equal(checkClean.status, 0, `check-bundle failed on clean pack: ${checkClean.stderr}`);

  const cleanList = childProcess.spawnSync("tar", ["-tzf", path.basename(cleanTar)], {
    cwd: outDir,
    encoding: "utf8",
  });
  assert.equal(cleanList.status, 0, cleanList.stderr);
  const entries = cleanList.stdout.split(/\r?\n/).filter(Boolean);
  assert.equal(entries.some((e) => /^[^/]+\/hermes\//.test(e)), false, "v2.0 bundle must contain zero hermes/ files");
  assert.equal(entries.some((e) => e.endsWith("/muse/__init__.py")), true);
  assert.equal(entries.some((e) => e.endsWith("/companion/server.py")), true);
  assert.equal(entries.some((e) => e.endsWith("/pyproject.toml")), true);
  assert.equal(entries.some((e) => e.endsWith("/uv.lock")), true);

  // 2. --with-data pack: verifies MUSE_HOME, config rewrite, online backup of BOTH muse.db (WAL) & state.db, and exclusions
  const py = findPython();
  if (!py) {
    assert.equal(Boolean(process.env.MUSEBOT_PYTHON), false, "MUSEBOT_PYTHON was requested, so Python cannot be skipped");
    return;
  }
  const yamlProbe = childProcess.spawnSync(
    py,
    ["-I", "-c", "try:\n import ruamel.yaml\nexcept ImportError:\n import yaml"],
    { encoding: "utf8" }
  );
  if (yamlProbe.status !== 0) {
    assert.equal(
      Boolean(process.env.MUSEBOT_PYTHON),
      false,
      `MUSEBOT_PYTHON (${py}) is missing ruamel.yaml / pyyaml: ${yamlProbe.stderr}`
    );
    return;
  }

  const fakeHome = path.join(tmp, "custom-muse-home");
  writeFiles(fakeHome, {
    ".env": "COMPANION_KEY_TEST=sk-test-dummy\n",
    "SOUL.md": "You are Muse.\n",
    "companion/state.json": '{"name":"Muse"}\n',
    "companion/devices.json": '[{"id":"pc"}]\n',
    "companion/stt/pylib/win_only.pyd": "binary",
    "companion/mcp/browser/cookies.sqlite": "secret-cookie",
    "config.yaml": [
      "security:",
      "  fake_ip_ranges:",
      "    - 198.18.0.0/15",
      "mcp_servers:",
      "  github:",
      "    command: npx",
      "    cwd: 'E:\\\\personal_bot\\\\muse-home\\\\companion\\\\mcp\\\\github'",
      "  browser:",
      "    command: npx",
      "    args: [-y, '@playwright/mcp@0.0.83', --browser, msedge]",
      "    cwd: 'E:\\\\personal_bot\\\\muse-home\\\\companion\\\\mcp\\\\browser'",
      "",
    ].join("\n"),
  });

  const dbInit = childProcess.spawnSync(
    py,
    [
      "-I",
      "-c",
      [
        "import sqlite3, sys",
        "for idx, tag in ((1, 'muse-wal'), (2, 'state-ro')):",
        "    c = sqlite3.connect(sys.argv[idx])",
        "    if idx == 1:",
        "        c.execute('PRAGMA journal_mode=WAL')",
        "    c.execute('CREATE TABLE t (v TEXT)')",
        "    c.execute('INSERT INTO t VALUES (?)', (tag,))",
        "    c.commit()",
        "    c.close()",
      ].join("\n"),
      path.join(fakeHome, "muse.db"),
      path.join(fakeHome, "state.db"),
    ],
    { encoding: "utf8" }
  );
  assert.equal(dbInit.status, 0, dbInit.stderr);
  const stateDbHashBefore = sha256File(path.join(fakeHome, "state.db"));

  const dataPack = childProcess.spawnSync(bash, ["deploy/vps/pack.sh", "--with-data"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      MUSE_HOME: toPosixPath(fakeHome),
      MUSEBOT_PYTHON: toPosixPath(py),
      MUSEBOT_VERSION: "priv-test",
      MUSEBOT_WEB_DIST: toPosixPath(fakeWeb),
      MUSEBOT_OUT_DIR: toPosixPath(outDir),
    },
  });
  assert.equal(dataPack.status, 0, `pack.sh --with-data failed: ${dataPack.stderr}\n${dataPack.stdout}`);
  assert.equal(sha256File(path.join(fakeHome, "state.db")), stateDbHashBefore, "pack.sh must never mutate source state.db");

  const privTar = path.join(outDir, "musebot-server-priv-test.tar.gz");
  assert.equal(fs.existsSync(privTar), true);

  // Public release gate MUST reject --with-data bundles
  const checkPriv = runCli(["check-bundle", privTar]);
  assert.equal(checkPriv.status, 1);
  assert.match(checkPriv.stderr, /不该公开的文件/);

  // Inspect unpacked --with-data contents
  const unpackDir = path.join(tmp, "unpacked");
  fs.mkdirSync(unpackDir, { recursive: true });
  const untar = childProcess.spawnSync("tar", ["-xzf", path.basename(privTar), "-C", unpackDir], {
    cwd: outDir,
    encoding: "utf8",
  });
  assert.equal(untar.status, 0, untar.stderr);

  const dataOut = path.join(unpackDir, "musebot-server-priv-test", "data");
  assert.equal(fs.existsSync(path.join(dataOut, "companion", "state.json")), true);
  assert.equal(fs.existsSync(path.join(dataOut, "companion", "devices.json")), false);
  assert.equal(fs.existsSync(path.join(dataOut, "companion", "stt", "pylib")), false);
  assert.equal(fs.existsSync(path.join(dataOut, "companion", "mcp")), false);
  assert.equal(fs.existsSync(path.join(dataOut, "muse.db")), true);
  assert.equal(fs.existsSync(path.join(dataOut, "state.db")), true);

  // Verify BOTH backed-up SQLite databases are readable and intact
  const dbCheck = childProcess.spawnSync(
    py,
    [
      "-I",
      "-c",
      [
        "import sqlite3, sys",
        "for idx, expected in ((1, 'muse-wal'), (2, 'state-ro')):",
        "    c = sqlite3.connect(sys.argv[idx])",
        "    assert c.execute('SELECT v FROM t').fetchone() == (expected,)",
        "    c.close()",
      ].join("\n"),
      path.join(dataOut, "muse.db"),
      path.join(dataOut, "state.db"),
    ],
    { encoding: "utf8" }
  );
  assert.equal(dbCheck.status, 0, `SQLite backup verification failed: ${dbCheck.stderr}`);

  const migratedCfg = fs.readFileSync(path.join(dataOut, "config.yaml"), "utf8");
  assert.doesNotMatch(migratedCfg, /fake_ip_ranges/);
  assert.doesNotMatch(migratedCfg, /msedge/);
  assert.doesNotMatch(migratedCfg, /@playwright\/mcp/);
  assert.doesNotMatch(migratedCfg, /E:\\/);
  assert.match(migratedCfg, /command:\s*playwright-mcp/);
  assert.match(migratedCfg, /\/opt\/data\/companion\/mcp\/github/);
  assert.match(migratedCfg, /\/opt\/data\/companion\/mcp\/browser/);
  assert.match(migratedCfg, /http:\/\/127\.0\.0\.1:9333/);
});

function setupUpgradeTestHarness(tmpDir) {
  const fakeBin = path.join(tmpDir, "fakebin");
  const dockerState = path.join(tmpDir, "docker-state");
  const appDir = path.join(tmpDir, "opt-companion");
  const bundleDir = path.join(tmpDir, "bundle-v2");
  const osRelease = path.join(tmpDir, "os-release");

  fs.mkdirSync(fakeBin, { recursive: true });
  fs.mkdirSync(path.join(dockerState, "images"), { recursive: true });
  fs.writeFileSync(osRelease, 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04 LTS"\n', "utf8");

  const writeExec = (name, body) => {
    const p = path.join(fakeBin, name);
    fs.writeFileSync(p, body.replace(/\r\n/g, "\n"), "utf8");
    fs.chmodSync(p, 0o755);
  };

  writeExec("id", '#!/usr/bin/env bash\nif [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi\nexec /usr/bin/id "$@"\n');
  writeExec("systemctl", '#!/usr/bin/env bash\nexit 0\n');
  writeExec("ss", '#!/usr/bin/env bash\nexit 0\n');
  writeExec("ufw", '#!/usr/bin/env bash\necho "Status: inactive"\nexit 0\n');
  writeExec("chown", '#!/usr/bin/env bash\nexit 0\n');
  writeExec("sleep", '#!/usr/bin/env bash\nexit 0\n');
  writeExec("python3", '#!/usr/bin/env bash\nexit 0\n');
  writeExec(
    "curl",
    `#!/usr/bin/env bash
url=""
for a in "$@"; do url="$a"; done
if [[ "$url" == *"/webui/bootstrap"* ]]; then
  printf '%s' "\${FAKE_CURL_HEALTH_CODE:-200}"
  exit 0
fi
exit 0
`
  );
  writeExec(
    "docker",
    `#!/usr/bin/env bash
set -euo pipefail
STATE="$FAKE_DOCKER_STATE"
IMG_DIR="$STATE/images"
mkdir -p "$IMG_DIR"
tag_file() { printf '%s/%s' "$IMG_DIR" "$(printf '%s' "$1" | tr ':/' '__')"; }

case "\${1:-}" in
  build)
    shift
    tag=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "-t" ]; then tag="$2"; shift 2; else shift; fi
    done
    printf '%s\n' "\${FAKE_BUILD_IMAGE_ID:-v2.0.0-img}" > "$(tag_file "$tag")"
    echo "build:$tag" >> "$STATE/events.log"
    ;;
  image)
    if [ "\${2:-}" = "inspect" ]; then
      [ -f "$(tag_file "\${3:-}")" ]
    fi
    ;;
  tag)
    cp "$(tag_file "$2")" "$(tag_file "$3")"
    echo "tag:$2->$3=$(cat "$(tag_file "$3")")" >> "$STATE/events.log"
    ;;
  rmi)
    rm -f "$(tag_file "\${2:-}")"
    echo "rmi:\${2:-}" >> "$STATE/events.log"
    ;;
  logs)
    echo "fake container logs"
    ;;
  compose)
    shift
    case "\${1:-}" in
      version)
        echo "Docker Compose version v2.29.0"
        ;;
      up)
        img="$(cat "$(tag_file companion-hermes:latest)" 2>/dev/null || echo missing)"
        cmd_line="$(grep -E '^[[:space:]]*command:' "$PWD/docker-compose.yml" 2>/dev/null | tr -d '\r' || echo none)"
        has_plugin="0"
        [ -f "$PWD/data/plugins/companion/plugin.yaml" ] && has_plugin="1"
        echo "compose-up|img=$img|plugin=$has_plugin|$cmd_line" >> "$STATE/events.log"
        # Simulate v2.0 container creating muse.db with 0644 on healthy startup
        if [[ "$cmd_line" == *"musebot"* ]] && [ "\${FAKE_CURL_HEALTH_CODE:-200}" = "200" ]; then
          printf 'SQLite v2 placeholder\n' > "$PWD/data/muse.db"
          chmod 644 "$PWD/data/muse.db" || true
        fi
        ;;
      down)
        echo "compose-down" >> "$STATE/events.log"
        ;;
    esac
    ;;
esac
`
  );

  // Pre-populate realistic v1.1.0 installation in appDir
  writeFiles(appDir, {
    ".installed": "",
    "VERSION": "v1.1.0\n",
    "domain": "bot.example.com\n",
    "Dockerfile": "FROM debian:13-slim\nCMD [\"hermes\", \"gateway\", \"run\"]\n",
    "docker-compose.yml": [
      "services:",
      "  companion:",
      "    image: companion-hermes:latest",
      "    container_name: companion",
      '    command: ["hermes", "gateway", "run"]',
      "",
    ].join("\n"),
    "hermes/pyproject.toml": "[project]\nname = 'hermes-agent'\n",
    "data/state.db": "SQLite format 3 v1.1.0 immutable payload\n",
    "data/config.yaml": "agent:\n  max_turns: 100\n",
    "data/.env": "OPENAI_API_KEY=sk-existing\n",
    "data/plugins/companion/plugin.yaml": "name: companion-platform\nversion: 1.1.0\n",
    "data/companion-lib/muse/__init__.py": "# obsolete v1 bridge dir\n",
    "data/companion-web/companion.html": "<!doctype html><title>v1.1.0</title>\n",
    "data/companion/devices.json": '[{"id":"iphone-1"}]\n',
    "data/companion/media.key": "secret-media-key\n",
    "caddy/acme.json": '{"cert":"preserved"}\n',
  });
  fs.writeFileSync(path.join(dockerState, "images", "companion-hermes_latest"), "v1.1.0-img\n", "utf8");

  // Populate v2.0 server bundle using the real deploy/vps scripts
  writeFiles(bundleDir, {
    "install.sh": fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "install.sh"), "utf8"),
    "uninstall.sh": fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "uninstall.sh"), "utf8"),
    "README.md": fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "README.md"), "utf8"),
    "Dockerfile": fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "Dockerfile"), "utf8"),
    "LICENSE": "MIT\n",
    "NOTICE": "Muse Bot v2.0\n",
    "VERSION": "v2.0.0\n",
    "pyproject.toml": "[project]\nname = 'muse-bot'\nversion = '2.0.0'\n",
    "uv.lock": "version = 1\n",
    "muse/__init__.py": "__version__ = '2.0.0'\n",
    "companion/server.py": "# v2.0 companion server\n",
    "web/companion.html": "<!doctype html><title>v2.0.0</title>\n",
    "tools/companion-pair.py": fs.readFileSync(path.join(REPO_ROOT, "tools", "companion-pair.py"), "utf8"),
  });

  const makeEnv = (healthCode) => ({
    ...process.env,
    MUSEBOT_APP_DIR: toPosixPath(appDir),
    MUSEBOT_OS_RELEASE: toPosixPath(osRelease),
    MUSEBOT_HEALTH_ATTEMPTS: "2",
    MUSEBOT_HTTPS_ATTEMPTS: "1",
    FAKE_DOCKER_STATE: toPosixPath(dockerState),
    FAKE_BUILD_IMAGE_ID: "v2.0.0-img",
    FAKE_CURL_HEALTH_CODE: healthCode,
  });

  const runWithFakeBin = (bash, scriptPath, scriptArgs, cwd, healthCode) =>
    childProcess.spawnSync(
      bash,
      ["-c", 'export PATH="$1:$PATH"; shift; exec bash "$@"', "_", toPosixPath(fakeBin), toPosixPath(scriptPath), ...scriptArgs],
      {
        cwd,
        encoding: "utf8",
        env: makeEnv(healthCode),
      }
    );

  return { fakeBin, dockerState, appDir, bundleDir, makeEnv, runWithFakeBin };
}

test("install.sh upgrades v1.x -> v2.0 cleanly, preserves state.db/devices, and uninstall.sh cleans v2.0 build context", (t) => {
  const bash = findGitBash();
  if (!bash) {
    t.skip("Git Bash not found on this Windows machine");
    return;
  }

  const tmp = makeTempDir("musebot-upgrade-ok-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const { dockerState, appDir, bundleDir, runWithFakeBin } = setupUpgradeTestHarness(tmp);
  const stateDbPath = path.join(appDir, "data", "state.db");
  const stateDbShaBefore = sha256File(stateDbPath);

  const res = runWithFakeBin(bash, path.join(bundleDir, "install.sh"), ["--yes"], bundleDir, "200");
  assert.equal(res.status, 0, `v1.x -> v2.0 upgrade failed:\nSTDOUT:\n${res.stdout}\nSTDERR:\n${res.stderr}`);
  assert.match(res.stdout, /升级好了：https:\/\/bot\.example\.com（版本：v2\.0\.0/);

  // Full v2.0 build context present in /opt/companion
  for (const rel of [
    "muse/__init__.py",
    "companion/server.py",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "LICENSE",
    "NOTICE",
    "tools/companion-pair.py",
    "VERSION",
    "domain",
    ".installed",
  ]) {
    assert.equal(fs.existsSync(path.join(appDir, rel)), true, `Expected ${rel} in upgraded APP dir`);
  }

  const composeYml = fs.readFileSync(path.join(appDir, "docker-compose.yml"), "utf8");
  assert.match(composeYml, /command: \["musebot", "serve"\]/);
  assert.match(composeYml, /image: companion-hermes:latest/);
  assert.match(composeYml, /COMPANION_WEB_DIST=\/opt\/data\/companion-web/);

  // Obsolete v1.x files and all .prev backups cleaned up after healthy boot
  for (const gone of [
    "hermes",
    "hermes.prev",
    "muse.prev",
    "companion.prev",
    "pyproject.toml.prev",
    "uv.lock.prev",
    "Dockerfile.prev",
    "docker-compose.yml.prev",
    "VERSION.prev",
    "data/plugins/companion",
    "data/plugins/companion.prev",
    "data/companion-lib",
    "data/companion-lib.prev",
    "data/companion-web.prev",
  ]) {
    assert.equal(fs.existsSync(path.join(appDir, gone)), false, `Expected stale path removed: ${gone}`);
  }

  // Data invariants preserved verbatim
  assert.equal(sha256File(stateDbPath), stateDbShaBefore, "state.db must remain byte-for-byte untouched");
  assert.equal(fs.readFileSync(path.join(appDir, "data", "companion", "devices.json"), "utf8"), '[{"id":"iphone-1"}]\n');
  assert.equal(fs.readFileSync(path.join(appDir, "data", "companion", "media.key"), "utf8"), "secret-media-key\n");
  assert.match(fs.readFileSync(path.join(appDir, "data", "companion-web", "companion.html"), "utf8"), /v2\.0\.0/);
  assert.equal(fs.readFileSync(path.join(dockerState, "images", "companion-hermes_latest"), "utf8").trim(), "v2.0.0-img");

  if (process.platform !== "win32") {
    const museDbMode = fs.statSync(path.join(appDir, "data", "muse.db")).mode & 0o777;
    assert.equal(museDbMode, 0o600, `Expected muse.db mode 0600, got ${museDbMode.toString(8)}`);
  }

  // Non-purge uninstall.sh removes all v2.0 build context while keeping data, caddy certs, and domain
  const unRes = runWithFakeBin(bash, path.join(appDir, "uninstall.sh"), [], appDir, "200");
  assert.equal(unRes.status, 0, `uninstall.sh failed: ${unRes.stderr}`);
  for (const removed of [
    "muse",
    "companion",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
    "LICENSE",
    "NOTICE",
    ".installed",
  ]) {
    assert.equal(fs.existsSync(path.join(appDir, removed)), false, `uninstall.sh should remove ${removed}`);
  }
  assert.equal(fs.existsSync(path.join(appDir, ".uninstalled")), true);
  assert.equal(sha256File(stateDbPath), stateDbShaBefore);
  assert.equal(fs.existsSync(path.join(appDir, "caddy", "acme.json")), true);
  assert.equal(fs.readFileSync(path.join(appDir, "domain"), "utf8").trim(), "bot.example.com");
});

test("install.sh automatically rolls back v1.x -> v2.0 upgrade on /webui/bootstrap health failure", (t) => {
  const bash = findGitBash();
  if (!bash) {
    t.skip("Git Bash not found on this Windows machine");
    return;
  }

  const tmp = makeTempDir("musebot-upgrade-rollback-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const { dockerState, appDir, bundleDir, runWithFakeBin } = setupUpgradeTestHarness(tmp);
  const stateDbPath = path.join(appDir, "data", "state.db");
  const stateDbShaBefore = sha256File(stateDbPath);

  const res = runWithFakeBin(bash, path.join(bundleDir, "install.sh"), ["--yes"], bundleDir, "000");
  assert.equal(res.status, 1, "Expected install.sh to exit non-zero after health-check failure");
  assert.match(res.stderr, /已自动回滚到上一版本/);

  // Dual .prev rollback restored docker-compose.yml, plugins/companion, companion-lib, hermes, Dockerfile, VERSION, web, and image tag
  const restoredCompose = fs.readFileSync(path.join(appDir, "docker-compose.yml"), "utf8");
  assert.match(restoredCompose, /command: \["hermes", "gateway", "run"\]/);
  assert.equal(fs.existsSync(path.join(appDir, "docker-compose.yml.prev")), false);

  assert.equal(
    fs.readFileSync(path.join(appDir, "data", "plugins", "companion", "plugin.yaml"), "utf8"),
    "name: companion-platform\nversion: 1.1.0\n"
  );
  assert.equal(fs.existsSync(path.join(appDir, "data", "plugins", "companion.prev")), false);

  assert.equal(fs.existsSync(path.join(appDir, "data", "companion-lib", "muse", "__init__.py")), true);
  assert.equal(fs.existsSync(path.join(appDir, "data", "companion-lib.prev")), false);

  assert.equal(fs.existsSync(path.join(appDir, "hermes", "pyproject.toml")), true);
  assert.equal(fs.existsSync(path.join(appDir, "hermes.prev")), false);
  assert.equal(fs.readFileSync(path.join(appDir, "VERSION"), "utf8").trim(), "v1.1.0");
  assert.match(fs.readFileSync(path.join(appDir, "Dockerfile"), "utf8"), /CMD \["hermes", "gateway", "run"\]/);
  assert.match(fs.readFileSync(path.join(appDir, "data", "companion-web", "companion.html"), "utf8"), /v1\.1\.0/);

  // Newly added v2.0 build-context artifacts cleaned up during rollback to v1.x
  for (const v2Only of ["muse", "companion", "pyproject.toml", "uv.lock"]) {
    assert.equal(fs.existsSync(path.join(appDir, v2Only)), false, `Rollback to v1.x should remove v2-only ${v2Only}`);
  }

  // Image tag restored to v1.1.0-img and state.db untouched
  assert.equal(fs.readFileSync(path.join(dockerState, "images", "companion-hermes_latest"), "utf8").trim(), "v1.1.0-img");
  assert.equal(sha256File(stateDbPath), stateDbShaBefore, "state.db must remain untouched across rollback");

  // Verify ordering: rollback restored docker-compose.yml, plugins/companion, AND companion-hermes:prev BEFORE docker compose up
  const events = fs.readFileSync(path.join(dockerState, "events.log"), "utf8").trim().split(/\r?\n/);
  const composeUps = events.filter((line) => line.startsWith("compose-up|"));
  assert.equal(composeUps.length, 2, `Expected 2 compose-up calls (upgrade + rollback), got: ${events.join("; ")}`);
  assert.match(composeUps[0], /img=v2\.0\.0-img\|plugin=0\|.*command: \["musebot", "serve"\]/);
  assert.match(composeUps[1], /img=v1\.1\.0-img\|plugin=1\|.*command: \["hermes", "gateway", "run"\]/);
});

test("GitHub Actions workflows pin action SHAs, drop checkout credentials, and enforce CI/release gates", () => {
  const wfDir = path.join(REPO_ROOT, ".github", "workflows");
  const wfFiles = fs.readdirSync(wfDir).filter((f) => f.endsWith(".yml")).sort();
  assert.deepEqual(wfFiles, ["android.yml", "ios.yml", "release.yml", "server.yml", "web.yml"]);

  for (const file of wfFiles) {
    const text = fs.readFileSync(path.join(wfDir, file), "utf8");
    const usesMatches = [...text.matchAll(/^\s*-?\s*uses:\s*([^\s#]+)/gm)];
    assert.equal(usesMatches.length > 0, true, `${file} should have at least one uses: step`);
    for (const m of usesMatches) {
      assert.match(m[1], /^[\w./-]+@[0-9a-f]{40}$/, `Unpinned action in ${file}: ${m[1]}`);
    }
    const checkoutCount = (text.match(/uses:\s*actions\/checkout@/g) || []).length;
    const persistFalseCount = (text.match(/persist-credentials:\s*false/g) || []).length;
    assert.equal(persistFalseCount, checkoutCount, `${file} must set persist-credentials: false on every checkout`);
  }

  const releaseYml = fs.readFileSync(path.join(wfDir, "release.yml"), "utf8");
  assert.match(releaseYml, /musebot-server\.tar\.gz\.sha256/);
  assert.match(releaseYml, /--prerelease/);
  assert.match(releaseYml, /npm run lint && npm test && npm run build/);
  assert.match(releaseYml, /unittest discover/);

  const androidYml = fs.readFileSync(path.join(wfDir, "android.yml"), "utf8");
  assert.match(androidYml, /python3 android\/scripts\/validate\.py/);
  assert.match(androidYml, /ANDROID_KEYSTORE_PASSWORD is empty/);
  assert.match(androidYml, /rm -rf android\/signing/);

  const serverYml = fs.readFileSync(path.join(wfDir, "server.yml"), "utf8");
  assert.match(serverYml, /python3 android\/scripts\/validate\.py/);

  const iosYml = fs.readFileSync(path.join(wfDir, "ios.yml"), "utf8");
  assert.doesNotMatch(iosYml, /continue-on-error:\s*true/);
});
