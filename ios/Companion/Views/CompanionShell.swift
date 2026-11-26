import SwiftUI
import UIKit

enum CompanionTab: String, CaseIterable, Identifiable {
    case chat = "主要聊天", feed = "动态", ideas = "点子", goals = "目标", helpers = "帮手", library = "资源库"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .chat: return "bubble.left"
        case .feed: return "newspaper"
        case .ideas: return "lightbulb"
        case .goals: return "checkmark.square"
        case .helpers: return "person.2"
        case .library: return "square.grid.2x2"
        }
    }
}

@MainActor
struct CompanionRootView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var menu = false
    @State private var settings = false
    @State private var identity = false
    @State private var appearance = false
    @State private var keyboardUp = false
    @State private var editSide = false
    @State private var sideName = ""
    @State private var exportedChatFile: SharedFile?
    @State private var exportCleanupFolder: URL?
    @State private var exportingChat = false
    @State private var selectedTab = CompanionTab.chat
    @ObservedObject private var inbox = IntentInbox.shared
    @State private var accentRevision = 0
    @State private var feedPosts: [JSONValue] = []
    @AppStorage("companion.tab-order") private var savedOrder = ""

    private var order: [CompanionTab] {
        var seen = Set<CompanionTab>()
        let kept = savedOrder.split(separator: "|").compactMap { CompanionTab(rawValue: String($0)) }.filter { seen.insert($0).inserted }
        return kept + CompanionTab.allCases.filter { !kept.contains($0) }
    }
    private var colorScheme: ColorScheme? {
        switch store.companionState["theme_mode"].stringValue {
        case "light": return .light
        case "dark": return .dark
        default: return nil
        }
    }
    private var unread: Int {
        let seen = store.companionState["feed_seen_at"].doubleValue ?? 0
        return feedPosts.filter { ($0["ts"].doubleValue ?? 0) > seen }.count
    }
    var body: some View {
        Group {
            if store.needsServer || store.needsPairing { ConnectionView() }
            else { appShell.inAppLinks() }  // links in 动态, replies and cards open inside the app
        }
        .tint(CompanionPalette.primary)
        .preferredColorScheme(colorScheme)
        .sheet(isPresented: $settings) {
            SettingsView().presentationDetents([.large]).presentationDragIndicator(.visible).tint(CompanionPalette.primary)
                .environment(\.openAppearance, {
                    settings = false
                    Task { try? await Task.sleep(for: .milliseconds(450)); appearance = true }
                })
        }
        // 外观 sits low over the main screen, so the chat above it previews each choice.
        .sheet(isPresented: $appearance) {
            AppearanceSettingsPage()
                .presentationDetents([.height(300)]).presentationDragIndicator(.visible)
                .presentationBackgroundInteraction(.enabled).presentationBackground(.regularMaterial)
                .tint(CompanionPalette.primary)
        }
        .sheet(isPresented: $identity) {
            IdentityView().presentationDetents([.large]).presentationDragIndicator(.hidden).tint(CompanionPalette.primary)
        }
        .sheet(item: $exportedChatFile, onDismiss: { exportedChatFile = nil; cleanupExportedChat() }) { file in
            ShareSheet(items: [file.url])
        }
        .alert("旁聊设置", isPresented: $editSide) {
            TextField("旁聊名称", text: $sideName)
            Button("保存") { Task { await store.renameChat(id: store.currentChatID, title: sideName) } }
            Button("删除旁聊", role: .destructive) { Task { await store.deleteChat(id: store.currentChatID) } }
            Button("取消", role: .cancel) { }
        }
        .alert("要切换到新的服务器吗？", isPresented: Binding(
            get: { store.pendingDeepLink != nil },
            set: { if !$0 { store.cancelPendingDeepLink() } }
        )) {
            Button("取消", role: .cancel) { store.cancelPendingDeepLink() }
            Button("切换并连接", role: .destructive) {
                settings = false; identity = false; appearance = false; menu = false
                Task { await store.confirmPendingDeepLink() }
            }
        } message: {
            Text("当前已配置服务器 \(store.serverAddress)。切换后将连接到 \(store.pendingDeepLink?.server ?? "")。")
        }
        .task(id: store.connected) {
            guard store.connected else { return }
            while !Task.isCancelled {
                await loadBadge()
                do { try await Task.sleep(for: .seconds(60)) } catch { break }
            }
        }
        .onChange(of: selectedTab) { old, new in if old == .feed || new == .feed { Task { await loadBadge() } } }
        .onChange(of: store.feedRevision) { _, _ in Task { await loadBadge() } }
        // 问伙伴 from Siri or Shortcuts: sent in the chat once there is one to send it in.
        .onChange(of: inbox.pending) { _, _ in deliverAsked() }
        .onChange(of: store.currentChatID) { _, _ in deliverAsked() }
        // A reply may have set a reminder (or changed one): the phone's notifications follow.
        .onChange(of: store.running) { was, now in if was && !now { Task { await store.planReminders() } } }
        .onChange(of: store.needsPairing) { _, needed in
            if needed { settings = false; identity = false; appearance = false; menu = false }
        }
        .onChange(of: store.needsServer) { _, needed in
            if needed { settings = false; identity = false; appearance = false; menu = false }
        }
        .onAppear { deliverAsked() }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            if !menu { keyboardUp = true }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardUp = false }
        .onChange(of: store.suggestedDraft) { _, value in
            if value != nil { selectedTab = .chat; settings = false; identity = false }
        }
    }

    private func deliverAsked() {
        guard let text = inbox.pending, !store.currentChatID.isEmpty, store.send(text: text) else { return }
        inbox.pending = nil
        selectedTab = .chat; settings = false; identity = false
        Haptics.send()
    }

    private var appShell: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                ZStack(alignment: .top) {
                    CompanionPalette.background.ignoresSafeArea()
                    // (A new accent redraws this root view; screens pick the colours up as they redraw.)
                    screen
                        // While typing, the keyboard takes the tab bar's place (it would only crowd the chat).
                        // Keep the obscured main screen's inset stable while the side drawer is open.
                        .safeAreaInset(edge: .bottom, spacing: 0) { if !keyboardUp || menu { tabBar } }
                        .accessibilityHidden(menu)
                    floatingHeader
                    statusBanner
                }
                .overlay(alignment: .leading) {
                    Color.clear.frame(width: 18).contentShape(Rectangle())
                        .gesture(DragGesture(minimumDistance: 24).onEnded { value in
                            if value.translation.width > 55 && abs(value.translation.height) < 90 { toggleMenu(true) }
                        }).accessibilityHidden(true)
                }
                if menu {
                    Color.black.opacity(0.55).ignoresSafeArea()
                        .onTapGesture { toggleMenu(false) }
                        .accessibilityLabel("关闭菜单").accessibilityAddTraits(.isButton)
                    SideMenu(selectedTab: $selectedTab, order: order, onReorder: saveOrder,
                             onClose: { toggleMenu(false) }, onSettings: { toggleMenu(false); settings = true })
                        .frame(width: min(360, geometry.size.width * 0.82))
                        .frame(maxHeight: .infinity)
                        .background(CompanionPalette.background.ignoresSafeArea())
                        .transition(.move(edge: .leading))
                }
            }
        }
    }

    /// The chat stays alive under the other tabs (hidden), so coming back finds it where it was —
    /// rebuilt each time, it started at the top and scrolled all the way down again.
    @ViewBuilder private var screen: some View {
        ZStack {
            ChatView()
                .opacity(selectedTab == .chat ? 1 : 0)
                .allowsHitTesting(selectedTab == .chat)
                .accessibilityHidden(selectedTab != .chat)
            switch selectedTab {
            case .chat: EmptyView()
            case .feed: FeedScreen(onChat: { selectedTab = .chat })
            case .ideas: IdeasScreen(onChat: { selectedTab = .chat })
            case .goals: GoalsScreen(onChat: { selectedTab = .chat }, onLibrary: { selectedTab = .library })
            case .helpers: HelpersScreen(onChat: { selectedTab = .chat })
            case .library: LibraryScreen()
            }
        }
    }

    private var floatingHeader: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .top) {
                VStack(spacing: -2) {
                    Button { identity = true; Haptics.selection() } label: {
                        AvatarView(value: store.companionState["avatar"].string, library: store.avatars,
                                   animation: store.animation, size: avatarSize, baseURL: store.baseURL,
                                   onAccent: rememberAccent)
                    }.buttonStyle(.plain).accessibilityLabel("\(store.companionName)，打开状态与身份")
                    Button { identity = true; Haptics.selection() } label: {
                        VStack(spacing: 2) {
                            Text(store.companionName).font(.system(size: 14, weight: .medium)).lineLimit(1)
                            HStack(spacing: 4) {
                                if store.running { ProgressView().controlSize(.mini) }
                                Text(statusLine).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }.padding(.horizontal, 12).padding(.vertical, 5)
                            .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(CompanionPalette.border))
                    }.buttonStyle(.plain).accessibilityHidden(true)
                }.frame(maxWidth: .infinity)
                HStack(alignment: .top) {
                    roundButton("line.3.horizontal", "打开菜单") { toggleMenu(true) }
                    Spacer()
                    if selectedTab == .chat && !store.currentChatID.isEmpty {
                        Menu {
                            Button {
                                Task { await exportCurrentChat() }
                            } label: {
                                Label(exportingChat ? "正在导出…" : "导出分享对话", systemImage: "square.and.arrow.up")
                            }
                            .disabled(exportingChat || !store.connected)

                            Button {
                                Haptics.selection()
                                Task { await store.forkChat(chatId: store.currentChatID) }
                            } label: {
                                Label("分叉为新旁聊", systemImage: "arrow.triangle.branch")
                            }
                            .disabled(!store.connected)

                            if store.currentChatID != store.mainChatID {
                                Divider()
                                Button {
                                    sideName = store.sideChats.first { $0.id == store.currentChatID }?.title ?? "新的旁聊"
                                    editSide = true
                                } label: {
                                    Label("旁聊改名 / 删除", systemImage: "slider.horizontal.3")
                                }
                            }
                        } label: {
                            Image(systemName: store.currentChatID != store.mainChatID ? "slider.horizontal.3" : "ellipsis")
                                .font(.system(size: 20))
                                .frame(width: 44, height: 44)
                                .modifier(FloatingPanel(radius: 22))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(store.currentChatID != store.mainChatID ? "旁聊操作与导出" : "对话操作与导出")
                    }
                }.padding(.horizontal, 16)
            }.padding(.top, 6)
            Spacer(minLength: 0)
        }
        .frame(height: 174)
        .background {
            LinearGradient(stops: [.init(color: CompanionPalette.background, location: 0),
                                   .init(color: CompanionPalette.background.opacity(0.97), location: 0.74),
                                   .init(color: CompanionPalette.background.opacity(0), location: 1)],
                           startPoint: .top, endPoint: .bottom).ignoresSafeArea(edges: .top)
                .allowsHitTesting(false)
        }
    }
    private var avatarSize: CGFloat {
        switch store.companionState["pet_size"].stringValue {
        case "small": return 72
        case "large": return 112
        default: return 90
        }
    }
    private var statusLine: String {
        if selectedTab == .chat && store.currentChatID != store.mainChatID {
            return "旁聊 · " + (store.sideChats.first { $0.id == store.currentChatID }?.title ?? "新的旁聊")
        }
        return store.running || !store.pendingApprovals.isEmpty ? store.activityLabel : (store.connected ? "在线" : "等待连接")
    }
    private var statusBanner: some View {
        VStack(spacing: 4) {
            if !store.connected {
                HStack {
                    Label(store.connecting ? "正在连接…" : "离线 · 消息等待重新连接后发送", systemImage: "wifi.slash")
                    Spacer(minLength: 0)
                    if !store.connecting { Button("重试") { Task { await store.foreground() } } }
                }.font(.caption).padding(10).background(CompanionPalette.elevated, in: Capsule())
            }
            if let error = store.errorMessage, !error.isEmpty {
                HStack {
                    Text(error).font(.caption).foregroundStyle(.red)
                    Spacer(minLength: 0)
                    if selectedTab == .chat {
                        Button("重试") { Task { await store.changeChat(store.currentChatID, forceHistory: true) } }.font(.caption).frame(minHeight: 44)
                    }
                    Button { store.errorMessage = nil } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭提示")
                }.padding(.leading, 12).background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
            }
        }.padding(.horizontal, 16).padding(.top, 174)
    }
    private var tabBar: some View {
        HStack(spacing: 0) {
            ForEach(order) { tab in
                Button { select(tab) } label: {
                    ZStack(alignment: .topTrailing) {
                        Image(systemName: tab.icon).font(.system(size: 24, weight: .regular))
                            .frame(maxWidth: .infinity).frame(height: 46)
                            .foregroundStyle(selectedTab == tab ? CompanionPalette.primaryText : Color.secondary)
                            .background(selectedTab == tab ? CompanionPalette.primary : .clear, in: Capsule())
                        if tab == .feed && unread > 0 {
                            Text(unread > 99 ? "99+" : "\(unread)").font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
                                .padding(4).background(.red, in: Capsule()).offset(x: -2, y: -2)
                        }
                    }
                }.buttonStyle(.plain).accessibilityLabel(tab.rawValue)
                    .accessibilityValue(tab == .feed && unread > 0 ? "\(unread) 条未读" : "")
                    .accessibilityAddTraits(selectedTab == tab ? .isSelected : [])
            }
        }.padding(.horizontal, 12).padding(.vertical, 6)
            .modifier(FloatingPanel(radius: 32))
            .padding(.horizontal, 12).padding(.top, 4).padding(.bottom, 4)
            .background {
                // Content fades out behind the bar instead of showing below it (as the web app).
                LinearGradient(stops: [.init(color: CompanionPalette.background.opacity(0), location: 0),
                                       .init(color: CompanionPalette.background, location: 0.45)],
                               startPoint: .top, endPoint: .bottom)
                    .ignoresSafeArea(edges: .bottom).allowsHitTesting(false)
            }
    }
    private func roundButton(_ icon: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon).font(.system(size: 22)).frame(width: 44, height: 44).modifier(FloatingPanel(radius: 22)) }
            .buttonStyle(.plain).accessibilityLabel(label)
    }
    private func select(_ tab: CompanionTab) {
        Haptics.selection(); selectedTab = tab
        // Back to the main chat only from a side chat: it is already there otherwise (no reload).
        if tab == .chat, !store.mainChatID.isEmpty, store.currentChatID != store.mainChatID {
            Task { await store.changeChat(store.mainChatID) }
        }
    }
    private func toggleMenu(_ show: Bool) {
        Haptics.selection()
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.25)) { menu = show }
    }
    private func saveOrder(_ value: [CompanionTab]) { savedOrder = value.map(\.rawValue).joined(separator: "|") }
    private func rememberAccent(_ color: Color) {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        guard UIColor(color).getRed(&r, green: &g, blue: &b, alpha: &a) else { return }
        let next = [Double(r), Double(g), Double(b)]
        let old = UserDefaults.standard.array(forKey: "companion.palette.accent") as? [Double] ?? []
        // Colours round-trip with tiny float noise: only a visibly different accent repaints.
        guard old.count != 3 || zip(old, next).contains(where: { abs($0 - $1) > 0.01 }) else { return }
        UserDefaults.standard.set(next, forKey: "companion.palette.accent"); accentRevision += 1
    }
    private func loadBadge() async {
        do {
            let posts = try await store.getResource("/api/webui/companion/feed")["posts"].array
            if posts != feedPosts { feedPosts = posts }  // an unchanged list redraws nothing
        }
        catch { /* The feed itself presents errors and a retry button. */ }
    }

    private func exportCurrentChat() async {
        let chatID = store.currentChatID
        guard !chatID.isEmpty, !exportingChat else { return }
        exportingChat = true
        defer { exportingChat = false }
        do {
            let markdown = try await store.exportChatMarkdown(chatId: chatID)
            cleanupExportedChat()
            let folder = FileManager.default.temporaryDirectory
                .appendingPathComponent("CompanionExport", isDirectory: true)
                .appendingPathComponent(UUID().uuidString, isDirectory: true)
            let rawTitle = chatID == store.mainChatID
                ? "\(store.companionName)-对话记录"
                : (store.sideChats.first { $0.id == chatID }?.title ?? "旁聊记录")
            let filename = FileKinds.safeName(rawTitle) + ".md"
            let fileURL = folder.appendingPathComponent(filename)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try Data(markdown.utf8).write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            exportCleanupFolder = folder
            exportedChatFile = SharedFile(url: fileURL)
            Haptics.selection()
        } catch {
            store.show(error)
        }
    }

    private func cleanupExportedChat() {
        guard let folder = exportCleanupFolder else { return }
        exportCleanupFolder = nil
        try? FileManager.default.removeItem(at: folder)
    }
}
