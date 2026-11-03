import XCTest

/// Walks every screen against `preview/mock_server.py` and keeps a screenshot of each,
/// so the look can be checked before installing. Run by the workflow's screenshot job only.
@MainActor
final class ScreenshotTests: XCTestCase {
    private var app: XCUIApplication!
    private let server = "http://127.0.0.1:8799/#pair=ABCD-2345"

    func testEveryScreen() throws {
        continueAfterFailure = true
        app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(zh-Hans)", "-AppleLocale", "zh_CN"]
        app.launch()
        snap("00-launch")
        connect()
        XCTAssertTrue(app.buttons["动态"].waitForExistence(timeout: 40), "the tab bar never appeared")
        pause(4)
        snap("01-chat")
        // A file it sent opens whole (Quick Look), from its card's first page.
        if tap(app.buttons["打开PDF「Weekend Hike」"]) {
            pause(2.5)
            snap("01-chat-file-open")
            tap(app.buttons["关闭"])
            pause(1)
        }

        app.swipeDown(velocity: .fast)
        app.swipeDown(velocity: .fast)
        pause(1)
        snap("02-chat-scrolled-up")

        if tap(app.buttons["添加附件"]) {
            pause(1.5)
            allowPhotos()
            pause(2)
            snap("03-composer-plus")
            // The simulator has no camera: 拍照或录像 says so (on an iPhone it opens the camera).
            if !tap(app.buttons["拍照或录像"]) { dismissSheet() }
            pause(1)
            snap("03-camera")
        }

        if tap(button(containing: "打开状态与身份")) {
            pause(2.5)
            snap("04-avatar-tap")
            walkCompanionPage()
            walkBrowser()
            if !tap(app.buttons["关闭状态"]) { dismissSheet() }
        }

        if tap(app.buttons["打开菜单"]) {
            pause(1.5)
            snap("05-menu")
            // 搜索 finds side chats and, below them, messages in any chat.
            let search = app.textFields["搜索旁聊和聊天记录"]
            if tap(search) {
                search.typeText("西湖")
                pause(2)
                snap("05-menu-search")
            }
            closeMenu()
        }

        // A link in a post opens the app's own web page (× closes it), not Safari.
        if tap(app.buttons["动态"]) {
            pause(3)
            if tap(app.links["阅读原文"].firstMatch) || tap(app.buttons["阅读原文"].firstMatch) {
                pause(4)
                snap("06-feed-link")
                tap(app.buttons["关闭"].firstMatch)
                pause(1)
            }
        }

        for (name, tab) in [("06-feed", "动态"), ("07-ideas", "点子"), ("08-goals", "目标"),
                            ("09-helpers", "帮手"), ("10-library", "资源库")] {
            if tap(app.buttons[tab]) {
                pause(3)
                snap(name)
                if tab == "帮手" { walkHelpers() }
            }
        }
        tap(app.buttons["主要聊天"])

        // 外观 closes settings and opens its dock over the chat.
        if openSettings() {
            snap("11-settings")
            if tap(button(beginningWith: "外观")) {
                pause(2)
                snap("12-appearance")
                if tap(app.buttons["中"]) { pause(1.5); snap("12-appearance-medium") }
                tap(app.buttons["大"])
                pause(1)
                tap(app.buttons["完成"])
                pause(1)
            }
        }

        if openSettings() {
            for (name, row) in [("13-models", "模型"), ("14-usage", "用量"),
                                ("15-skills", "技能"), ("16-services", "已连接的服务"), ("17-permissions", "权限"),
                                ("18-vault", "安全凭证"), ("19-devices", "设备")] {
                let link = button(beginningWith: row)
                if !link.exists { app.swipeUp() }
                if tap(link) {
                    pause(2.5)
                    snap(name)
                    if row == "模型" { walkModels() }
                    app.swipeUp()  // the rest of a long page
                    pause(1)
                    snap(name + "-more")
                    back()
                }
            }
            if !tap(app.buttons["关闭设置"].firstMatch) { dismissSheet() }
        }

        if tap(app.buttons["打开菜单"]), tap(app.buttons["新开旁聊"]) {
            pause(3)
            snap("20-side-chat-new")
        }

        if tap(app.buttons["主要聊天"]) {
            let field = app.textViews["消息内容"].exists ? app.textViews["消息内容"] : app.textFields["消息内容"]
            if tap(field) {
                field.typeText("你好")
                pause(1)
                snap("21-composer-typing")
                // A tap on the chat above puts the keyboard away.
                app.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.35)).tap()
                pause(1.5)
                snap("22-keyboard-dismissed")
                XCTAssertFalse(app.keyboards.firstMatch.exists, "the keyboard stayed up after a tap outside")
            }
        }
    }

    // MARK: - Steps

    /// 浏览器: the preview card, full screen watching, 接管控制 (tools appear), 复制, 交还控制.
    /// The companion's page: each pane, and a sheet from each (a task, an approval, a schedule, SOUL.md).
    private func walkCompanionPage() {
        let panes = [("04-page-activity", "做过的事", ["下午", "上午"]), ("04-page-approvals", "审核记录", ["已被拒", "已允许"]),
                     ("04-page-schedules", "定时任务", ["下次", "已暂停"]), ("04-page-identity", "身份与记忆", ["打开 SOUL.md"])]
        for (name, pane, rows) in panes {
            guard tap(app.buttons[pane]) else { continue }
            pause(2)
            snap(name)
            for row in rows where button(containing: row).exists {
                if tap(button(containing: row)) {
                    pause(2.5)
                    snap(name + "-sheet")
                    if !tap(app.buttons["关闭"].firstMatch) { dismissSheet() }
                    pause(1.2)
                }
                break
            }
        }
        if tap(app.buttons["更换形象或名称"]) {
            pause(1)
            snap("04-page-avatar-menu")
            if tap(app.buttons["更换虚拟形象"]) { pause(2); snap("04-page-looks"); tap(app.buttons["完成"].firstMatch); pause(1) }
        }
    }

    private func walkBrowser() {
        guard tap(app.buttons["浏览器"]) else { return }
        pause(3)
        snap("04-browser-card")
        guard tap(app.buttons["打开浏览器"]) else { return }
        pause(3)
        snap("04-browser-watching")
        if tap(app.buttons["接管控制"]) {
            pause(2)
            snap("04-browser-controlling")
            if tap(app.buttons["复制选中文字"]) { pause(1.2); snap("04-browser-copied") }
            if tap(app.buttons["交还控制"]) { pause(2); snap("04-browser-handed-back") }
        }
        tap(app.buttons["完成"])
        pause(1.5)
    }

    /// 帮手: a helper's conversation (chat-app style), its profile, and 新建帮手.
    private func walkHelpers() {
        let row = app.collectionViews.cells.element(boundBy: 1)  // the first conversation (row 0 is the heading)
        if tap(row) {
            pause(3)
            snap("09-helper-chat")
            if tap(app.buttons["资料与设置"]) {
                pause(2)
                snap("09-helper-profile")
                tap(app.buttons["关闭"].firstMatch)
                pause(1)
            }
            tap(app.buttons["返回帮手列表"])
            pause(1.5)
        }
        if tap(app.buttons["新建"]) {
            pause(1)
            snap("09-helpers-menu")
            if tap(app.buttons["新建帮手"]) {
                pause(2)
                snap("09-new-helper")
                tap(app.buttons["关闭"].firstMatch)
                pause(1)
            }
        }
    }

    /// Inside 模型: one model's page, 添加模型 (services, then one service's models), the 图像 kind.
    private func walkModels() {
        if tap(button(beginningWith: "DeepSeek")) {
            pause(2)
            snap("13-model-detail")
            back()
        }
        if tap(button(beginningWith: "添加对话模型")) {
            pause(2.5)
            snap("13-add-model")
            if tap(button(beginningWith: "DeepSeek")) {
                pause(3)
                snap("13-add-model-vendor")
            }
            tap(app.buttons["关闭"].firstMatch)
            pause(1.5)
        }
        if tap(button(beginningWith: "图像")) {
            pause(1.5)
            snap("13-models-image")
            tap(button(beginningWith: "对话"))
            pause(1)
        }
    }

    private func connect() {
        let field = app.textFields["服务器地址或连接链接"]
        guard field.waitForExistence(timeout: 20) else { return }
        field.tap()
        field.typeText(server)
        snap("00-connect")
        tap(app.buttons["连接"])
    }

    private func openSettings() -> Bool {
        guard tap(app.buttons["打开菜单"]) else { return false }
        pause(1)
        guard tap(app.buttons["设置"].firstMatch) else { return false }
        pause(2.5)
        return true
    }

    /// The system's photo-access question (shown by SpringBoard, in its own language): allow all.
    private func allowPhotos() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for label in ["允许完全访问", "允许访问所有照片", "Allow Full Access", "Allow Access to All Photos"] {
            let button = springboard.buttons[label]
            if button.waitForExistence(timeout: 2) {
                button.tap()
                return
            }
        }
    }

    /// The dimmed strip to the right of the drawer closes it.
    private func closeMenu() {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5)).tap()
        pause(1)
    }

    private func back() {
        let button = app.navigationBars.buttons.element(boundBy: 0)
        if button.exists { button.tap() } else { app.swipeRight() }
        pause(1)
    }

    private func dismissSheet() {
        // A sheet's own close button first; otherwise drag it down by its top edge.
        for label in ["关闭", "完成", "取消"] {
            let button = app.buttons[label].firstMatch
            if button.exists, !button.frame.isEmpty {
                button.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
                pause(1)
                return
            }
        }
        let top = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45))
        top.press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.98)))
        pause(1)
    }

    /// Taps the middle of the element. (Asking `isHittable` fails the test outright for some
    /// SwiftUI buttons — "activation point invalid" — so it is not asked.)
    @discardableResult
    private func tap(_ element: XCUIElement) -> Bool {
        guard element.waitForExistence(timeout: 8), !element.frame.isEmpty else {
            XCTContext.runActivity(named: "missing: \(element)") { _ in }
            return false
        }
        element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        return true
    }

    private func button(beginningWith label: String) -> XCUIElement {
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", label)).firstMatch
    }

    private func button(containing label: String) -> XCUIElement {
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", label)).firstMatch
    }

    private func pause(_ seconds: TimeInterval) {
        RunLoop.current.run(until: Date().addingTimeInterval(seconds))
    }

    private func snap(_ name: String) {
        let shot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let folder = ProcessInfo.processInfo.environment["SCREENSHOT_DIR"], !folder.isEmpty {
            try? shot.pngRepresentation.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name + ".png"))
        }
    }
}
