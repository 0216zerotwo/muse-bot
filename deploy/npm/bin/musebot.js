#!/usr/bin/env node
/*
 * Muse Bot 服务器的一键安装和日常管理（npx musebot-server …）。
 *
 * 这个 npm 包只是安装器：安装包（Muse 原生运行时 + 伙伴服务 + 网页 + install.sh）由 GitHub Actions 在每次
 * 打版本标签时构建，发在仓库的 Releases 里；install / update 下载最新的（或 --version 指定的）那个，
 * 并在存在 .sha256 文件时校验完整性。
 * install.sh 装 Docker（没有的话）、构建镜像（浏览器、Python、语音组件都在镜像里）、用 Caddy 申请
 * HTTPS 证书，容器设成 restart: unless-stopped，Docker 开机自启，所以伙伴常驻。
 * 所以服务器端改了代码，只要在 GitHub 上发新版本；这个 npm 包只有安装器本身改了才需要重新发布。
 *
 * 只用 Node 自带的模块，Node 12 也能跑（Ubuntu 22.04 apt 里的版本）。
 */
"use strict";

var childProcess = require("child_process");
var crypto = require("crypto");
var fs = require("fs");
var os = require("os");
var path = require("path");

var APP = process.env.MUSEBOT_APP_DIR || "/opt/companion";
var PORT = Number(process.env.COMPANION_PORT) || 8766;
// Where the server bundles are published (a fork can point MUSEBOT_REPO at itself).
var REPO = process.env.MUSEBOT_REPO || "0216zerotwo/muse-bot";
var RELEASE_BASE_URL = (process.env.MUSEBOT_RELEASE_BASE_URL || ("https://github.com/" + REPO + "/releases")).replace(/\/+$/, "");
var ASSET = "musebot-server.tar.gz";
var VERSION = require("../package.json").version;

// 公开的安装包里不许出现的东西：数据和密钥只能用 pack.sh --with-data 私下带到自己的服务器
// （那时它们在安装包最外层的 data/ 里）。
var FORBIDDEN_BASENAMES = [
  ".env",
  "auth.json",
  "devices.json",
  "media.key",
  "vapid_private.pem",
  "muse.db",
  "state.db",
  "projects.db",
  "keystore-password.txt"
];

var REQUIRED_BUNDLE_FILES = [
  "install.sh",
  "uninstall.sh",
  "Dockerfile",
  "LICENSE",
  "NOTICE",
  "pyproject.toml",
  "uv.lock",
  "muse/__init__.py",
  "companion/server.py",
  "web/companion.html",
  "tools/companion-pair.py"
];

var activeTempDir = null;
var cleanupHooksInstalled = false;

function say(text) {
  console.log("\x1b[1;34m==>\x1b[0m " + text);
}

function warn(text) {
  console.log("\x1b[1;33m[注意]\x1b[0m " + text);
}

function removeDirSync(target) {
  if (!target || !fs.existsSync(target)) return;
  if (typeof fs.rmSync === "function") {
    try {
      fs.rmSync(target, { recursive: true, force: true });
      return;
    } catch (err) {
      // Fall through to manual walk on older Node or locked handles.
    }
  }
  var stat;
  try {
    stat = fs.lstatSync(target);
  } catch (err) {
    return;
  }
  if (!stat.isDirectory()) {
    try {
      fs.unlinkSync(target);
    } catch (err) {}
    return;
  }
  var items = [];
  try {
    items = fs.readdirSync(target);
  } catch (err) {}
  for (var i = 0; i < items.length; i++) {
    removeDirSync(path.join(target, items[i]));
  }
  try {
    fs.rmdirSync(target);
  } catch (err) {}
}

function cleanupActiveTempDir() {
  if (!activeTempDir) return;
  var dir = activeTempDir;
  activeTempDir = null;
  removeDirSync(dir);
}

function installCleanupHooks() {
  if (cleanupHooksInstalled) return;
  cleanupHooksInstalled = true;
  process.on("SIGINT", function () {
    cleanupActiveTempDir();
    process.exit(130);
  });
  process.on("SIGTERM", function () {
    cleanupActiveTempDir();
    process.exit(143);
  });
}

function fail(text) {
  cleanupActiveTempDir();
  console.error("\x1b[1;31m[停止]\x1b[0m " + text);
  process.exit(1);
}

function run(command, args, options) {
  var result = childProcess.spawnSync(command, args, Object.assign({ stdio: "inherit" }, options || {}));
  if (result.error) fail(command + " 运行不了：" + result.error.message);
  return result.status === null ? 1 : result.status;
}

function quiet(command, args, options) {
  var result = childProcess.spawnSync(command, args, Object.assign({ encoding: "utf8" }, options || {}));
  return {
    ok: !result.error && result.status === 0,
    status: result.status,
    out: (result.stdout || "").trim(),
    err: (result.stderr || "").trim()
  };
}

function needServer() {
  if (process.platform !== "linux") {
    fail("这个命令要在服务器（Ubuntu / Debian）上运行。在电脑上请先 ssh 登录服务器。");
  }
  if (typeof process.getuid === "function" && process.getuid() !== 0) {
    fail("需要 root 权限：sudo npx musebot-server " + process.argv.slice(2).join(" "));
  }
}

function needInstalled() {
  needServer();
  if (!fs.existsSync(path.join(APP, ".installed"))) {
    fail("这台服务器上还没装伙伴：sudo npx musebot-server install 你的域名");
  }
}

function domain() {
  try {
    return fs.readFileSync(path.join(APP, "domain"), "utf8").trim();
  } catch (error) {
    return "";
  }
}

function installedVersion() {
  try {
    return fs.readFileSync(path.join(APP, "VERSION"), "utf8").trim();
  } catch (error) {
    return "";
  }
}

function isUnsafeArchiveEntry(rawEntry) {
  if (!rawEntry) return false;
  var normalized = rawEntry.replace(/\\/g, "/");
  if (normalized.charAt(0) === "/" || /^[A-Za-z]:/.test(normalized)) return true;
  var parts = normalized.split("/");
  for (var i = 0; i < parts.length; i++) {
    if (parts[i] === "..") return true;
  }
  return false;
}

function isForbiddenBundlePath(relPath) {
  if (!relPath) return false;
  if (relPath === "data/" || relPath.indexOf("data/") === 0) return true;
  if (/(?:^|\/)(?:muse-home|hermes-home)(?:\/|$)/i.test(relPath)) return true;
  if (relPath === "vault/" || relPath.indexOf("vault/") === 0) return true;
  if (relPath.slice(-1) === "/") return false;
  var base = path.posix.basename(relPath).toLowerCase();
  if (FORBIDDEN_BASENAMES.indexOf(base) !== -1) return true;
  if (/^(?:muse|state|projects)\.db(?:[-.].*)?$/i.test(base)) return true;
  if (/^\.env(?:\.(?!example$)|[-_]).*$/i.test(base)) return true;
  if (/\.(?:jks|keystore|p12|pfx)$/i.test(base)) return true;
  return false;
}

function entries(archive) {
  // By its name from its own folder: GNU tar reads "E:\…" (packing on Windows) as a remote host.
  var resolved = path.resolve(archive);
  var listed = quiet("tar", ["-tzf", path.basename(resolved)], {
    cwd: path.dirname(resolved),
    maxBuffer: 256 * 1024 * 1024
  });
  if (!listed.ok) fail("读不了安装包：" + archive);
  var raw = listed.out.split(/\r?\n/).filter(Boolean);
  var unsafe = raw.filter(isUnsafeArchiveEntry);
  if (unsafe.length) {
    fail("安装包里含有越界路径：\n  " + unsafe.slice(0, 10).join("\n  "));
  }
  return raw;
}

/** 发布前（GitHub Actions 的 Release 流程）：安装包齐全，而且没有数据和密钥。 */
function checkBundle(archive) {
  if (!archive || !fs.existsSync(archive)) fail("找不到安装包：" + (archive || "（没写）"));
  var names = entries(archive).map(function (entry) {
    return entry.replace(/^[^/]+\//, "");
  });
  REQUIRED_BUNDLE_FILES.forEach(function (file) {
    if (names.indexOf(file) === -1) fail("安装包不完整，缺 " + file);
  });
  var found = names.filter(isForbiddenBundlePath);
  if (found.length) fail("安装包里有不该公开的文件：\n  " + found.join("\n  "));
  say("安装包齐全，没有数据和密钥（" + names.length + " 个文件）");
}

function parseSha256Text(text, expectedName) {
  var lines = String(text || "").split(/\r?\n/).map(function (line) {
    return line.trim();
  }).filter(function (line) {
    return line && line.charAt(0) !== "#";
  });
  if (!lines.length) return null;
  var firstHash = null;
  for (var i = 0; i < lines.length; i++) {
    var match = lines[i].match(/^([0-9a-fA-F]{64})(?:\s+\*?(.+))?$/);
    if (!match) continue;
    var digest = match[1].toLowerCase();
    var fileLabel = match[2] ? path.posix.basename(match[2].trim().replace(/\\/g, "/")) : "";
    if (!firstHash) firstHash = digest;
    if (expectedName && fileLabel === expectedName) return digest;
  }
  return firstHash;
}

function computeFileSha256(filePath) {
  var hash = crypto.createHash("sha256");
  var fd = fs.openSync(filePath, "r");
  try {
    var buf = Buffer.alloc(64 * 1024);
    var bytesRead = 0;
    while ((bytesRead = fs.readSync(fd, buf, 0, buf.length, null)) > 0) {
      hash.update(buf.slice(0, bytesRead));
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest("hex").toLowerCase();
}

function verifyArchiveSha256(archive, shaText, assetName) {
  var expected = parseSha256Text(shaText, assetName || path.basename(archive));
  if (!expected) fail("校验文件格式无效（未找到 SHA-256 摘要）");
  var actual = computeFileSha256(archive);
  if (actual !== expected) {
    fail("安装包 SHA-256 校验失败（期望 " + expected + "，实际 " + actual + "）");
  }
  say("安装包 SHA-256 校验通过");
}

/** 取出 `--name 值` 或 `--name=值` 这样的选项（没有就是 undefined）。 */
function option(args, name) {
  for (var i = 0; i < args.length; i++) {
    if (args[i] === name) {
      var value = args[i + 1];
      if (!value || value.charAt(0) === "-") fail(name + " 后面要写值");
      args.splice(i, 2);
      return value;
    }
    if (args[i].indexOf(name + "=") === 0) {
      var inline = args[i].slice(name.length + 1);
      if (!inline) fail(name + " 后面要写值");
      args.splice(i, 1);
      return inline;
    }
  }
  return undefined;
}

function consumeFlag(args, names) {
  var matched = false;
  for (var i = args.length - 1; i >= 0; i--) {
    if (names.indexOf(args[i]) !== -1) {
      args.splice(i, 1);
      matched = true;
    }
  }
  return matched;
}

function rejectUnknownFlags(args) {
  var bad = args.filter(function (arg) {
    return arg.charAt(0) === "-";
  });
  if (bad.length) {
    fail("未知选项：" + bad[0] + "（查看帮助：npx musebot-server --help）");
  }
}

/** 安装包：--bundle 给的本地文件，或从 GitHub Releases 下载（最新的，或 --version 指定的）。
 * 解压后返回 install.sh 所在的目录。 */
function bundleDir(archiveOpt, versionOpt) {
  installCleanupHooks();
  var target = fs.mkdtempSync(path.join(os.tmpdir(), "musebot-"));
  activeTempDir = target;
  var archive = archiveOpt;
  if (archive) {
    if (!fs.existsSync(archive)) fail("找不到 --bundle 后面的安装包：" + archive);
    if (fs.existsSync(archive + ".sha256")) {
      verifyArchiveSha256(archive, fs.readFileSync(archive + ".sha256", "utf8"), path.basename(archive));
    }
  } else {
    if (versionOpt && !/^[A-Za-z0-9._-]+$/.test(versionOpt)) {
      fail("版本号格式不正确：" + versionOpt);
    }
    var url = versionOpt
      ? RELEASE_BASE_URL + "/download/" + versionOpt + "/" + ASSET
      : RELEASE_BASE_URL + "/latest/download/" + ASSET;
    archive = path.join(target, ASSET);
    say("下载安装包 " + (versionOpt || "（最新版）") + "：" + url);
    if (run("curl", ["-fL", "--retry", "3", "-o", archive, url]) !== 0) {
      fail("下载失败。确认服务器能访问 github.com，以及 " + (versionOpt ? "版本 " + versionOpt + " 存在" : "仓库已经发过版本") + "：" + RELEASE_BASE_URL);
    }
    var shaFile = archive + ".sha256";
    var shaUrl = url + ".sha256";
    var shaFetch = quiet("curl", ["-sSL", "--retry", "3", "-w", "%{http_code}", "-o", shaFile, shaUrl]);
    var httpCode = shaFetch.out.slice(-3);
    if (shaFetch.ok && httpCode === "200" && fs.existsSync(shaFile)) {
      verifyArchiveSha256(archive, fs.readFileSync(shaFile, "utf8"), ASSET);
    } else if (httpCode === "404") {
      if (process.env.MUSEBOT_REQUIRE_SHA256 === "1") {
        fail("该版本缺少 .sha256 校验文件，已按 MUSEBOT_REQUIRE_SHA256=1 中止安装");
      }
      warn("该版本未发布 .sha256 校验文件，跳过哈希校验");
    } else {
      fail("获取安装包校验文件失败（HTTP " + (httpCode || "错误") + "）：" + shaUrl);
    }
  }
  // 解压前先做路径安全检查，避免越界覆盖
  entries(archive);
  say("解压安装包");
  var resolvedArchive = path.resolve(archive);
  if (run("tar", ["-xzf", path.basename(resolvedArchive), "--no-same-owner", "-C", target], { cwd: path.dirname(resolvedArchive) }) !== 0) {
    fail("解压失败：" + archive);
  }
  var inside = fs.readdirSync(target).filter(function (name) {
    var candidate = path.join(target, name, "install.sh");
    return fs.existsSync(candidate) && fs.statSync(path.join(target, name)).isDirectory();
  })[0];
  if (!inside) fail("这不是伙伴的安装包（里面没有 install.sh）");
  return path.join(target, inside);
}

function install(args) {
  var assumeYes = consumeFlag(args, ["--yes", "-y"]);
  var archiveOpt = option(args, "--bundle");
  var versionOpt = option(args, "--version");
  rejectUnknownFlags(args);
  if (args.length > 1) {
    fail("多余的参数：" + args.slice(1).join(" ") + "（查看帮助：npx musebot-server --help）");
  }
  needServer();
  var env = Object.assign({}, process.env);
  if (assumeYes) env.ASSUME_YES = "1";
  var code = 1;
  try {
    var folder = bundleDir(archiveOpt, versionOpt);
    var shellArgs = [path.join(folder, "install.sh")];
    if (args[0]) shellArgs.push(args[0]);
    code = run("bash", shellArgs, { env: env });
  } finally {
    // 程序已经放进 /opt/companion；解压出来的临时目录必须清掉（--bundle 带数据时里面还有密钥）。
    cleanupActiveTempDir();
  }
  process.exit(code);
}

function status(args) {
  rejectUnknownFlags(args);
  needInstalled();
  var address = domain();
  console.log("版本      " + (installedVersion() || "（未知）"));
  var probe = quiet("curl", ["-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "http://127.0.0.1:" + PORT + "/webui/bootstrap"]);
  var probeCode = probe.out.slice(-3);
  var up = probe.ok && (probeCode === "200" || probeCode === "401");
  var boot = quiet("systemctl", ["is-enabled", "docker"]).out === "enabled";
  console.log("伙伴      " + (up ? "运行中" : "没在运行（看日志：sudo npx musebot-server logs）"));
  console.log("地址      " + (address ? "https://" + address : "（未知）"));
  console.log("开机自启  " + (boot ? "是（Docker 开机启动，容器 restart: unless-stopped）" : "否：sudo systemctl enable docker"));
  run("docker", ["compose", "ps"], { cwd: APP });
  process.exit(up ? 0 : 1);
}

function compose(args, words, done) {
  rejectUnknownFlags(args);
  needInstalled();
  var code = run("docker", ["compose"].concat(words), { cwd: APP });
  if (code === 0 && done) say(done);
  process.exit(code);
}

function logs(args) {
  var follow = consumeFlag(args, ["-f", "--follow"]);
  var caddy = consumeFlag(args, ["--caddy"]);
  rejectUnknownFlags(args);
  needInstalled();
  var container = caddy ? "companion-caddy" : "companion";
  process.exit(run("docker", ["logs", "--tail", "200"].concat(follow ? ["-f"] : []).concat([container])));
}

function pair(args) {
  rejectUnknownFlags(args);
  needInstalled();
  var address = domain();
  process.exit(run("python3", [path.join(APP, "tools", "companion-pair.py")].concat(address ? ["https://" + address] : []).concat([String(PORT)])));
}

function uninstall(args) {
  var purge = consumeFlag(args, ["--purge"]);
  var assumeYes = consumeFlag(args, ["--yes", "-y"]);
  rejectUnknownFlags(args);
  needInstalled();
  var scriptArgs = [path.join(APP, "uninstall.sh")];
  if (purge) scriptArgs.push("--purge");
  if (assumeYes) scriptArgs.push("--yes");
  process.exit(run("bash", scriptArgs));
}

function printVersion() {
  var srv = installedVersion();
  if (srv) {
    console.log("musebot-server " + VERSION + " (server: " + srv + ")");
  } else {
    console.log(VERSION);
  }
}

function help() {
  console.log([
    "Muse Bot 服务器（musebot-server） " + VERSION,
    "",
    "在服务器上（Ubuntu 22.04+ / Debian 12+，用 root）：",
    "  sudo npx -y musebot-server install [你的域名]  安装（不写域名就用 <IP-横杠>.sslip.io）",
    "  sudo npx -y musebot-server update              升级到 GitHub 上的最新版本（数据保留）",
    "  sudo npx musebot-server status                 版本、是否在运行、地址、开机自启",
    "  sudo npx musebot-server logs [-f] [--caddy]    看日志（--caddy 看 HTTPS 反向代理日志）",
    "  sudo npx musebot-server restart                重启（stop / start 停下和启动）",
    "  sudo npx musebot-server pair                   生成手机配对码",
    "  sudo npx musebot-server uninstall [--purge]    卸载（--purge 连数据一起删）",
    "  npx musebot-server version                     查看安装器与服务器版本",
    "",
    "选项：",
    "  -y, --yes             不再确认，直接执行",
    "  --version v1.2.0      装指定版本（默认最新版，见 https://github.com/" + REPO + "/releases）",
    "  --bundle 文件.tar.gz  用 pack.sh 打的本地安装包（比如带着数据搬家），不从 GitHub 下载",
    "  -h, --help            显示本帮助并退出"
  ].join("\n"));
}

function main(rawArgs) {
  var args = rawArgs.slice();
  var command = args.shift() || "help";
  if (command === "help" || command === "--help" || command === "-h" || args.indexOf("--help") !== -1 || args.indexOf("-h") !== -1) {
    help();
    return;
  }
  switch (command) {
    case "install":
      install(args);
      break;
    case "update":
    case "upgrade":
      // install.sh keeps the data and the domain it was installed with.
      needInstalled();
      install(args);
      break;
    case "status":
      status(args);
      break;
    case "logs":
      logs(args);
      break;
    case "restart":
      compose(args, ["restart"], "已重启");
      break;
    case "stop":
      compose(args, ["stop"], "已停下（再开：sudo npx musebot-server start）");
      break;
    case "start":
      compose(args, ["up", "-d"], "已启动");
      break;
    case "pair":
      pair(args);
      break;
    case "uninstall":
      uninstall(args);
      break;
    case "check-bundle":
      rejectUnknownFlags(args);
      checkBundle(args[0]);
      break;
    case "version":
    case "--version":
    case "-v":
      printVersion();
      break;
    default:
      console.error("\x1b[1;31m[停止]\x1b[0m 未知命令：" + command + "（查看帮助：npx musebot-server --help）");
      help();
      process.exit(1);
  }
}

if (require.main === module) {
  main(process.argv.slice(2));
}

module.exports = {
  checkBundle: checkBundle,
  computeFileSha256: computeFileSha256,
  isForbiddenBundlePath: isForbiddenBundlePath,
  isUnsafeArchiveEntry: isUnsafeArchiveEntry,
  main: main,
  parseSha256Text: parseSha256Text,
  verifyArchiveSha256: verifyArchiveSha256
};
