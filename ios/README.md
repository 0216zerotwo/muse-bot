# Muse Bot · iPhone 原生版

SwiftUI 原生 iPhone 客户端，最低 iOS 17，直接连接你自己的 Muse Bot 服务器。底部是主要聊天、动态、点子、目标、帮手、资源库六个选项卡；点顶部形象查看状态、审批、服务器浏览器、身份与记忆；左上角打开旁聊抽屉，抽屉左下角是完整设置。

**没有 Mac 也能装**：GitHub Actions 的云端 Mac 负责编译，你在 Windows 上用 Sideloadly 签名安装。

## 用 GitHub Actions 构建

1. 在 GitHub 上 Fork 本仓库（或推送到你自己的仓库）。
2. 推送到 `main`，或在 **Actions → Build iOS → Run workflow** 手动运行。流程依次是：模拟器单元测试 → 未签名安装包（产物 **Companion-unsigned**）→ 在模拟器里走一遍所有页面截图（产物 **Companion-screenshots**，用的是 `preview/mock_server.py` 的示例数据）。
3. 运行变绿后，在运行详情底部 **Artifacts → Companion-unsigned** 下载 ZIP，解压得到 `Companion-unsigned.ipa`。

工作流用 `macos-latest` 并通过 `DEVELOPER_DIR` 固定 **Xcode 26.3** 与 iOS 26.2 模拟器；GitHub 移除这个版本后，按 [macOS 镜像清单](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-arm64-Readme.md) 更新 `.github/workflows/ios.yml` 里的版本。仓库里不需要任何签名证书、Apple ID 或 GitHub Secrets。

## 用 Windows + Sideloadly 安装

1. 从 [Sideloadly 官网](https://sideloadly.io/) 下载 Windows 版，并按官网说明安装 Apple 官网下载的 iTunes 和 iCloud（不要用 Microsoft Store 版）。
2. 准备一个专门用来侧载的**免费 Apple ID**。账号密码只在 Sideloadly / Apple 的登录流程里输入，不写进项目。
3. 数据线连接 iPhone，解锁并点「信任这台电脑」。
4. 打开 Sideloadly，选中 iPhone，拖入 `Companion-unsigned.ipa`，输入 Apple ID，勾选自动刷新，点 **Start**。
5. 若提示未开启开发者模式：iPhone **设置 → 隐私与安全性 → 开发者模式**，按提示重启确认（[Apple 说明](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)）。
6. **设置 → 通用 → VPN 与设备管理** 里信任这个 Apple ID，然后从桌面打开「Muse Bot」。

免费账号的签名 **7 天**有效，同一台手机最多同时装 **3 个**这类侧载 App；Sideloadly 的自动刷新会在到期前重新签名（见 [Sideloadly FAQ](https://sideloadly.io/faq)）。这一版没有系统推送（免费签名不能用 APNs），提醒到点时由手机自己发本地通知。

更新：下载新的产物，用**同一个 Apple ID** 直接覆盖安装，不必先删除 App。

## 第一次打开

在「连接你的伙伴」页填服务器地址：

- 服务器在家里电脑上：填电脑的局域网地址，例如 `http://192.168.1.20:8766`（`127.0.0.1` 在手机上指的是手机自己）。Windows 防火墙要放行这个端口；iPhone 第一次访问局域网会询问权限。
- 服务器在 VPS 上：填 `https://你的域名`。

然后在服务器上生成一次性配对码（或在已连上的设备里 **设置 → 设备 → 生成配对码**）：

```bash
python tools/companion-pair.py https://你的地址
```

在 iPhone 输入配对码即可。设备令牌存在 iOS Keychain，以后自动连接；断网时消息进入待发队列，恢复后按顺序发送。

## 给开发者

- `Companion/`：入口和 SwiftUI 页面；`Core/` 纯数据和事件处理（有 XCTest）；`Services/` HTTP、Keychain、WebSocket 和状态；`Views/` 原生视图，设置页在 `Views/Settings/`。
- 接口以服务器为准：先看 `../web/src/companion/companion-api.ts` 和 `../companion/server.py` 的字段。新接口要有脱敏的真实响应（`CompanionTests/Fixtures`）和解码测试。
- `project.yml` 是 XcodeGen 配置，不提交 `.xcodeproj`。唯一的 SwiftPM 依赖是固定在 2.4.1 的 MarkdownUI；互动卡片内置 Web UI kit、Chart.js、Leaflet（及其许可），在隔离的 WKWebView 里运行。
- Windows 上的静态检查：`python scripts/validate.py`（需要 PyYAML、Pillow）。把兼容的 `tree-sitter` 与 `tree-sitter-swift` wheel 放进被忽略的 `.validation/python` 后，`python scripts/validate.py --require-swift` 还会检查每个 Swift 文件的语法树。它不做类型检查，真正的编译看 CI。
- CI 出错时找失败步骤里**第一条** `error:`；测试失败可下载 `Companion-test-results`（`.xcresult`）。
