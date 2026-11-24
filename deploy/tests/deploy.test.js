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

function buildTarGz(stageDir, topFolder, archivePath) {
  const res = childProcess.spawnSync("tar", ["-czf", path.basename(archivePath), topFolder], {
    cwd: stageDir,
    encoding: "utf8",
  });
  assert.equal(res.status, 0, `tar failed: ${res.stderr}`);
}

const VALID_BUNDLE_FILES = {
  "musebot-server-v1.0.0/install.sh": "#!/usr/bin/env bash\necho ok\n",
  "musebot-server-v1.0.0/uninstall.sh": "#!/usr/bin/env bash\necho ok\n",
  "musebot-server-v1.0.0/Dockerfile": "FROM debian:13-slim\n",
  "musebot-server-v1.0.0/LICENSE": "MIT\n",
  "musebot-server-v1.0.0/NOTICE": "Muse Bot\n",
  "musebot-server-v1.0.0/hermes/pyproject.toml": "[project]\nname='hermes-agent'\n",
  "musebot-server-v1.0.0/hermes/uv.lock": "version = 1\n",
  "musebot-server-v1.0.0/hermes/.env.example": "OPENAI_API_KEY=\n",
  "musebot-server-v1.0.0/hermes/.envrc": "dotenv_if_exists\n",
  "musebot-server-v1.0.0/hermes/tools/data/readme.txt": "nested data folder inside hermes source\n",
  "musebot-server-v1.0.0/companion/plugin.yaml": "name: companion-platform\n",
  "musebot-server-v1.0.0/web/companion.html": "<!doctype html><title>Muse Bot</title>\n",
  "musebot-server-v1.0.0/tools/companion-pair.py": "print('pair')\n",
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

  fs.writeFileSync(path.join(installedApp, "VERSION"), "v1.2.3\n", "utf8");
  const withServer = runCli(["--version"], { MUSEBOT_APP_DIR: installedApp });
  assert.equal(withServer.status, 0);
  assert.equal(withServer.stdout.trim(), `musebot-server ${PKG_VERSION} (server: v1.2.3)`);
});

test("check-bundle accepts clean bundle (including .env.example, .envrc, nested hermes data/) and rejects secrets or missing files", (t) => {
  const tmp = makeTempDir("musebot-bundle-");
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));

  writeFiles(tmp, VALID_BUNDLE_FILES);
  const cleanArchive = path.join(tmp, "clean.tar.gz");
  buildTarGz(tmp, "musebot-server-v1.0.0", cleanArchive);

  const okRes = runCli(["check-bundle", cleanArchive]);
  assert.equal(okRes.status, 0, okRes.stderr);
  assert.match(okRes.stdout, /安装包齐全，没有数据和密钥/);

  // Missing LICENSE must fail
  const missingDir = makeTempDir("musebot-missing-");
  t.after(() => fs.rmSync(missingDir, { recursive: true, force: true }));
  const withoutLicense = { ...VALID_BUNDLE_FILES };
  delete withoutLicense["musebot-server-v1.0.0/LICENSE"];
  writeFiles(missingDir, withoutLicense);
  const missingArchive = path.join(missingDir, "missing.tar.gz");
  buildTarGz(missingDir, "musebot-server-v1.0.0", missingArchive);
  const missingRes = runCli(["check-bundle", missingArchive]);
  assert.equal(missingRes.status, 1);
  assert.match(missingRes.stderr, /缺 LICENSE/);

  // Forbidden secret paths
  const forbiddenCases = [
    "musebot-server-v1.0.0/data/config.yaml",
    "musebot-server-v1.0.0/.env",
    "musebot-server-v1.0.0/companion/.env.local",
    "musebot-server-v1.0.0/vault/items.json",
    "musebot-server-v1.0.0/companion/media.key",
    "musebot-server-v1.0.0/state.db",
    "musebot-server-v1.0.0/projects.db",
    "musebot-server-v1.0.0/signing/companion.jks",
    "musebot-server-v1.0.0/signing/keystore-password.txt",
  ];
  for (const badPath of forbiddenCases) {
    const rel = badPath.replace(/^[^/]+\//, "");
    assert.equal(musebot.isForbiddenBundlePath(rel), true, `Expected forbidden: ${rel}`);
  }
  assert.equal(musebot.isForbiddenBundlePath("hermes/.env.example"), false);
  assert.equal(musebot.isForbiddenBundlePath("hermes/.envrc"), false);
  assert.equal(musebot.isForbiddenBundlePath("hermes/tools/data/readme.txt"), false);

  const leakyDir = makeTempDir("musebot-leaky-");
  t.after(() => fs.rmSync(leakyDir, { recursive: true, force: true }));
  writeFiles(leakyDir, {
    ...VALID_BUNDLE_FILES,
    "musebot-server-v1.0.0/companion/.env.production": "SECRET=1\n",
  });
  const leakyArchive = path.join(leakyDir, "leaky.tar.gz");
  buildTarGz(leakyDir, "musebot-server-v1.0.0", leakyArchive);
  const leakyRes = runCli(["check-bundle", leakyArchive]);
  assert.equal(leakyRes.status, 1);
  assert.match(leakyRes.stderr, /不该公开的文件/);
});

test("Path traversal & SHA-256 checksum helpers enforce strict integrity", (t) => {
  assert.equal(musebot.isUnsafeArchiveEntry("../etc/passwd"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("musebot/../../etc/shadow"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("/etc/passwd"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("C:/Windows/win.ini"), true);
  assert.equal(musebot.isUnsafeArchiveEntry("musebot-server-v1.0.0/install.sh"), false);

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

test("Dockerfile and VPS shell scripts enforce runtime, security, and upgrade invariants", () => {
  const dockerfile = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "Dockerfile"), "utf8");
  const installSh = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "install.sh"), "utf8");
  const uninstallSh = fs.readFileSync(path.join(REPO_ROOT, "deploy", "vps", "uninstall.sh"), "utf8");

  // Dockerfile invariants
  assert.match(dockerfile, /PYTHONUNBUFFERED=1/);
  assert.match(dockerfile, /@playwright\/mcp@0\.0\.83/);
  assert.match(dockerfile, /uv sync --frozen --no-dev --inexact/);
  assert.match(dockerfile, /import py_vapid, http_ece, anthropic, sqlite3/);
  assert.match(dockerfile, /sqlite_version_info >= \(3, 51, 3\)/);
  assert.match(dockerfile, /tokenize=['"]?trigram/);
  assert.match(dockerfile, /install-stamp\.json/);
  assert.match(dockerfile, /\/usr\/local\/bin\/gateway/);
  assert.match(dockerfile, /CMD \["hermes", "gateway", "run"\]/);

  // install.sh invariants
  assert.match(installSh, /command: \["hermes", "gateway", "run"\]/);
  assert.match(installSh, /docker build -t companion-hermes:next/);
  assert.match(installSh, /rollback_upgrade/);
  assert.match(installSh, /boot_code=.*"200".*"401"/s);
  assert.match(installSh, /chmod 700 "\$DATA"/);
  assert.match(installSh, /chmod 600 "\$secret_file"/);
  assert.match(installSh, /Strict-Transport-Security/);
  assert.doesNotMatch(installSh, /^\s*encode gzip\s*$/m);
  assert.match(installSh, /ufw allow 443\/udp/);
  assert.match(installSh, /max_turns: 100/);

  // uninstall.sh preserves caddy certs on non-purge and removes .installed
  assert.match(uninstallSh, /rm -f "\$APP\/\.installed"/);
  assert.match(uninstallSh, /touch "\$APP\/\.uninstalled"/);
  assert.doesNotMatch(uninstallSh, /rm -rf "\$APP\/hermes" "\$APP\/caddy"/);
});

test("Shell syntax, --help flags, and end-to-end pack.sh (clean & --with-data) work in bash", (t) => {
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

  const fakeHermes = path.join(tmp, "hermes");
  const fakeWeb = path.join(tmp, "web-dist");
  const outDir = path.join(tmp, "out");
  writeFiles(fakeHermes, {
    "pyproject.toml": "[project]\nname = 'hermes-agent'\nversion = '0.0.0'\n",
    "uv.lock": "version = 1\n",
    ".env.example": "OPENAI_API_KEY=\n",
    "apps/desktop/ignore.txt": "excluded\n",
  });
  writeFiles(fakeWeb, {
    "companion.html": "<!doctype html><html><body>Muse Bot</body></html>\n",
  });

  // 1. Clean release pack + check-bundle (pass drive-letter slash path on Windows to exercise GNU tar --force-local)
  const cleanPack = childProcess.spawnSync(bash, ["deploy/vps/pack.sh"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      MUSEBOT_VERSION: "v9.9.9-test",
      MUSEBOT_HERMES_SRC: toPosixPath(fakeHermes),
      MUSEBOT_WEB_DIST: toPosixPath(fakeWeb),
      MUSEBOT_OUT_DIR: outDir.replace(/\\/g, "/"),
    },
  });
  assert.equal(cleanPack.status, 0, `clean pack.sh failed: ${cleanPack.stderr}\n${cleanPack.stdout}`);
  const cleanTar = path.join(outDir, "musebot-server-v9.9.9-test.tar.gz");
  assert.equal(fs.existsSync(cleanTar), true);

  const checkClean = runCli(["check-bundle", cleanTar]);
  assert.equal(checkClean.status, 0, `check-bundle failed on clean pack: ${checkClean.stderr}`);

  // 2. --with-data pack: verifies ruamel.yaml/yaml config rewrite, SQLite backup, and stt/pylib exclusion
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

  const fakeHome = path.join(tmp, "custom-hermes-home");
  writeFiles(fakeHome, {
    ".env": "COMPANION_KEY_TEST=sk-test-dummy\n",
    "SOUL.md": "You are Muse.\n",
    "companion/state.json": '{"name":"Muse"}\n',
    "companion/devices.json": '[{"id":"pc"}]\n',
    "companion/stt/pylib/win_only.pyd": "binary",
    "companion/mcp/browser/cookies.sqlite": "secret-cookie",
    "config.yaml": [
      "plugins:",
      "  enabled:",
      "    - companion-platform",
      "security:",
      "  fake_ip_ranges:",
      "    - 198.18.0.0/15",
      "mcp_servers:",
      "  github:",
      "    command: npx",
      "    cwd: 'E:\\\\personal_bot\\\\hermes-home\\\\companion\\\\mcp\\\\github'",
      "  browser:",
      "    command: npx",
      "    args: [-y, '@playwright/mcp@0.0.83', --browser, msedge]",
      "    cwd: 'E:\\\\personal_bot\\\\hermes-home\\\\companion\\\\mcp\\\\browser'",
      "",
    ].join("\n"),
  });

  const dbInit = childProcess.spawnSync(
    py,
    [
      "-I",
      "-c",
      "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE t (v TEXT)'); c.execute(\"INSERT INTO t VALUES ('ok')\"); c.commit(); c.close()",
      path.join(fakeHome, "state.db"),
    ],
    { encoding: "utf8" }
  );
  assert.equal(dbInit.status, 0, dbInit.stderr);

  const dataPack = childProcess.spawnSync(bash, ["deploy/vps/pack.sh", "--with-data"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      HERMES_HOME: toPosixPath(fakeHome),
      MUSEBOT_PYTHON: toPosixPath(py),
      MUSEBOT_VERSION: "priv-test",
      MUSEBOT_HERMES_SRC: toPosixPath(fakeHermes),
      MUSEBOT_WEB_DIST: toPosixPath(fakeWeb),
      MUSEBOT_OUT_DIR: toPosixPath(outDir),
    },
  });
  assert.equal(dataPack.status, 0, `pack.sh --with-data failed: ${dataPack.stderr}\n${dataPack.stdout}`);

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
  assert.equal(fs.existsSync(path.join(dataOut, "state.db")), true);

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

