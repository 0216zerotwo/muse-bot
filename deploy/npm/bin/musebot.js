#!/usr/bin/env node
/*
 * Muse Bot 服务器的一键安装和日常管理（npx musebot-server …）。
 *
 * 这个 npm 包只是安装器：安装包（Hermes + 伙伴插件 + 网页 + install.sh）由 GitHub Actions 在每次
 * 打版本标签时构建，发在仓库的 Releases 里；install / update 下载最新的（或 --version 指定的）那个。
 * install.sh 装 Docker（没有的话）、构建镜像（浏览器、Python、语音组件都在镜像里）、用 Caddy 申请
 * HTTPS 证书，容器设成 restart: unless-stopped，Docker 开机自启，所以伙伴常驻。
 * 所以服务器端改了代码，只要在 GitHub 上发新版本；这个 npm 包只有安装器本身改了才需要重新发布。
 *
 * 只用 Node 自带的模块，Node 12 也能跑（Ubuntu 22.04 apt 里的版本）。
 */
"use strict";

var childProcess = require("child_process");
var fs = require("fs");
var os = require("os");
var path = require("path");

var APP = "/opt/companion";
var PORT = 8766;
// Where the server bundles are published (a fork can point MUSEBOT_REPO at itself).
var REPO = process.env.MUSEBOT_REPO || "0216zerotwo/muse-bot";
var ASSET = "musebot-server.tar.gz";
var VERSION = require("../package.json").version;

// 安装包里不许出现的东西：数据和密钥只能用 pack.sh --with-data 私下带到自己的服务器。
var FORBIDDEN = [".env", "auth.json", "devices.json", "media.key", "state.db", "data"];

function say(text) {
  console.log("\x1b[1;34m==>\x1b[0m " + text);
}

function fail(text) {
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
  return { ok: !result.error && result.status === 0, out: (result.stdout || "").trim() };
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

function entries(archive) {
  // By its name from its own folder: GNU tar reads "E:\…" (packing on Windows) as a remote host.
  var listed = quiet("tar", ["-tzf", path.basename(archive)], { cwd: path.dirname(archive), maxBuffer: 256 * 1024 * 1024 });
  if (!listed.ok) fail("读不了安装包：" + archive);
  return listed.out.split(/\r?\n/).filter(Boolean);
}

/** 发布前（GitHub Actions 的 Release 流程）：安装包齐全，而且没有数据和密钥。 */
function checkBundle(archive) {
  if (!archive || !fs.existsSync(archive)) fail("找不到安装包：" + (archive || "（没写）"));
  var names = entries(archive).map(function (entry) {
    return entry.replace(/^[^/]+\//, "");
  });
  ["install.sh", "uninstall.sh", "Dockerfile", "hermes/pyproject.toml", "hermes/uv.lock", "companion/plugin.yaml",
    "web/companion.html", "tools/companion-pair.py"].forEach(function (file) {
    if (names.indexOf(file) === -1) fail("安装包不完整，缺 " + file);
  });
  var found = names.filter(function (name) {
    return name === "data/" || name.indexOf("data/") === 0 || FORBIDDEN.indexOf(path.posix.basename(name)) !== -1;
  });
  if (found.length) fail("安装包里有不该公开的文件：\n  " + found.join("\n  "));
  say("安装包齐全，没有数据和密钥（" + names.length + " 个文件）");
}

/** 取出 `--name 值` 这样的选项（没有就是 undefined）。 */
function option(args, name) {
  var index = args.indexOf(name);
  if (index === -1) return undefined;
  var value = args[index + 1];
  if (!value || value.charAt(0) === "-") fail(name + " 后面要写值");
  args.splice(index, 2);
  return value;
}

/** 安装包：--bundle 给的本地文件，或从 GitHub Releases 下载（最新的，或 --version 指定的）。
 * 解压后返回 install.sh 所在的目录。 */
function bundleDir(args) {
  var target = fs.mkdtempSync(path.join(os.tmpdir(), "musebot-"));
  var archive = option(args, "--bundle");
  var version = option(args, "--version");
  if (archive) {
    if (!fs.existsSync(archive)) fail("找不到 --bundle 后面的安装包：" + archive);
  } else {
    var url = version
      ? "https://github.com/" + REPO + "/releases/download/" + version + "/" + ASSET
      : "https://github.com/" + REPO + "/releases/latest/download/" + ASSET;
    archive = path.join(target, ASSET);
    say("下载安装包 " + (version || "（最新版）") + "：" + url);
    if (run("curl", ["-fL", "--retry", "3", "-o", archive, url]) !== 0) {
      fail("下载失败。确认服务器能访问 github.com，以及 " + (version ? "版本 " + version + " 存在" : "仓库已经发过版本") + "：https://github.com/" + REPO + "/releases");
    }
  }
  say("解压安装包");
  if (run("tar", ["-xzf", archive, "-C", target]) !== 0) fail("解压失败：" + archive);
  var inside = fs.readdirSync(target).filter(function (name) {
    return fs.existsSync(path.join(target, name, "install.sh"));
  })[0];
  if (!inside) fail("这不是伙伴的安装包（里面没有 install.sh）");
  return path.join(target, inside);
}

function install(args) {
  needServer();
  var env = Object.assign({}, process.env);
  var yes = args.indexOf("--yes");
  if (yes === -1) yes = args.indexOf("-y");
  if (yes !== -1) {
    env.ASSUME_YES = "1";
    args.splice(yes, 1);
  }
  var folder = bundleDir(args);
  var target = args.filter(function (arg) {
    return arg.charAt(0) !== "-";
  })[0];
  var shellArgs = [path.join(folder, "install.sh")];
  if (target) shellArgs.push(target);
  var code = run("bash", shellArgs, { env: env });
  // 程序已经放进 /opt/companion；解压出来的这份没用了（--bundle 带数据时里面还有密钥）。
  run("rm", ["-rf", path.dirname(folder)]);
  process.exit(code);
}

function installedVersion() {
  try {
    return fs.readFileSync(path.join(APP, "VERSION"), "utf8").trim();
  } catch (error) {
    return "";
  }
}

function status() {
  needInstalled();
  var address = domain();
  console.log("版本      " + (installedVersion() || "（未知）"));
  var up = quiet("curl", ["-fsS", "-o", "/dev/null", "--max-time", "5", "http://127.0.0.1:" + PORT + "/webui/bootstrap"]).ok;
  var boot = quiet("systemctl", ["is-enabled", "docker"]).out === "enabled";
  console.log("伙伴      " + (up ? "运行中" : "没在运行（看日志：sudo npx musebot-server logs）"));
  console.log("地址      " + (address ? "https://" + address : "（未知）"));
  console.log("开机自启  " + (boot ? "是（Docker 开机启动，容器 restart: unless-stopped）" : "否：sudo systemctl enable docker"));
  run("docker", ["compose", "ps"], { cwd: APP });
  process.exit(up ? 0 : 1);
}

function compose(words, done) {
  needInstalled();
  var code = run("docker", ["compose"].concat(words), { cwd: APP });
  if (code === 0 && done) say(done);
  process.exit(code);
}

function logs(args) {
  needInstalled();
  var follow = args.indexOf("-f") !== -1 || args.indexOf("--follow") !== -1;
  process.exit(run("docker", ["logs", "--tail", "200"].concat(follow ? ["-f"] : []).concat(["companion"])));
}

function pair() {
  needInstalled();
  var address = domain();
  process.exit(run("python3", [path.join(APP, "tools", "companion-pair.py")].concat(address ? ["https://" + address] : []).concat([String(PORT)])));
}

function uninstall(args) {
  needInstalled();
  process.exit(run("bash", [path.join(APP, "uninstall.sh")].concat(args.indexOf("--purge") !== -1 ? ["--purge"] : [])));
}

function help() {
  console.log([
    "Muse Bot 服务器（musebot）" + VERSION,
    "",
    "在服务器上（Ubuntu 22.04+ / Debian 12+，用 root）：",
    "  sudo npx -y musebot-server install 你的域名   安装（不写域名就用 <IP>.sslip.io）",
    "  sudo npx -y musebot-server update             升级到 GitHub 上的最新版本（数据保留）",
    "  sudo npx musebot-server status                 版本、是否在运行、地址、开机自启",
    "  sudo npx musebot-server logs [-f]              看日志",
    "  sudo npx musebot-server restart                重启（stop / start 停下和启动）",
    "  sudo npx musebot-server pair                   生成手机配对码",
    "  sudo npx musebot-server uninstall [--purge]    卸载（--purge 连数据一起删）",
    "",
    "选项：",
    "  --yes                 不再确认，直接装",
    "  --version v1.2.0      装指定版本（默认最新版，见 https://github.com/" + REPO + "/releases）",
    "  --bundle 文件.tar.gz  用 pack.sh 打的本地安装包（比如带着数据搬家），不从 GitHub 下载",
  ].join("\n"));
}

var args = process.argv.slice(2);
var command = args.shift() || "help";
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
    status();
    break;
  case "logs":
    logs(args);
    break;
  case "restart":
    compose(["restart"], "已重启");
    break;
  case "stop":
    compose(["stop"], "已停下（再开：sudo npx musebot-server start）");
    break;
  case "start":
    compose(["up", "-d"], "已启动");
    break;
  case "pair":
    pair();
    break;
  case "uninstall":
    uninstall(args);
    break;
  case "check-bundle":
    checkBundle(args[0]);
    break;
  case "version":
  case "--version":
  case "-v":
    console.log(VERSION);
    break;
  default:
    help();
}
