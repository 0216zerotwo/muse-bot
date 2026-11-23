import Combine
import Foundation
import SwiftUI
import UIKit

/// The server browser's live view over the computer socket: JPEG frames and page info in,
/// normalized taps, scrolls, typing and the hand-over out (as BrowserPane.tsx).
@MainActor
private final class ComputerStream: ObservableObject {
    struct Copied: Equatable { let id = UUID(); let text: String }

    @Published var image: UIImage?
    @Published var aspectRatio: CGFloat = 1.6
    @Published var title = ""
    @Published var pageURL = ""
    @Published var hold = ""
    @Published var handoff = ""
    @Published var idle = false
    @Published var error: String?
    @Published var editable = false
    @Published var teachCount: Int?
    @Published var taughtSteps: [JSONValue] = []
    @Published var copied: Copied?
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var generation = UUID()
    private var keepTeaching = false

    var controlling: Bool { hold == "user" }
    var connected: Bool { socket != nil }

    func connect(_ makeSocket: @escaping @MainActor () throws -> URLSessionWebSocketTask) {
        guard receiveTask == nil else { return }
        let attempt = UUID()
        generation = attempt
        receiveTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled && self.generation == attempt {
                let task: URLSessionWebSocketTask
                do {
                    task = try makeSocket()
                } catch {
                    self.error = error.localizedDescription
                    self.receiveTask = nil
                    return
                }
                self.socket = task
                task.resume()
                do {
                    while !Task.isCancelled && self.generation == attempt {
                        let response = try await task.receive()
                        let data: Data
                        switch response {
                        case .string(let value): data = Data(value.utf8)
                        case .data(let value): data = value
                        @unknown default: continue
                        }
                        guard self.generation == attempt, let message = try? JSONValue.parse(data) else { continue }
                        self.apply(message)
                    }
                } catch {
                    guard !Task.isCancelled, self.generation == attempt else { return }
                    self.error = "浏览器连接中断，正在重连…"
                    self.hold = ""
                    self.teachCount = nil
                    self.editable = false
                }
                task.cancel(with: .goingAway, reason: nil)
                do { try await Task.sleep(nanoseconds: 1_500_000_000) } catch { return }
            }
        }
    }

    func disconnect() async {
        if teachCount != nil { await send("teach_stop") }
        if controlling { await send("done") }
        generation = UUID()
        receiveTask?.cancel()
        receiveTask = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        hold = ""
        editable = false
        teachCount = nil
    }

    func send(_ type: String, fields: [String: JSONValue] = [:]) async {
        guard let socket else { error = "浏览器连接还没准备好"; return }
        var body = fields
        body["type"] = .string(type)
        do {
            let data = try JSONEncoder().encode(JSONValue.object(body))
            guard let text = String(data: data, encoding: .utf8) else { return }
            try await socket.send(.string(text))
        } catch { self.error = "这次操作未送达，请重试" }
    }

    func stopTeaching(keep: Bool) async {
        keepTeaching = keep
        await send("teach_stop")
    }

    private func apply(_ message: JSONValue) {
        error = nil
        switch message["type"].stringValue {
        case "frame":
            guard let data = Data(base64Encoded: message["data"].stringValue), let frame = UIImage(data: data) else { return }
            image = frame
            let width = message["w"].doubleValue ?? Double(frame.size.width)
            let height = message["h"].doubleValue ?? Double(frame.size.height)
            aspectRatio = CGFloat(width / max(1, height))
            idle = false
        case "idle":
            idle = true; image = nil; title = ""; pageURL = ""; hold = ""; editable = false
        case "page":
            title = message["title"].stringValue
            pageURL = message["url"].stringValue
            if message.object.keys.contains("hold") { hold = message["hold"].stringValue }
            handoff = message["handoff"]["reason"].stringValue
        case "hold": hold = message["hold"].stringValue
        case "focus": editable = message["editable"].boolValue
        case "copied": copied = Copied(text: message["text"].stringValue)
        case "error": error = message["message"].nonemptyString ?? "浏览器暂时不可用"
        case "teach": teachCount = message["on"].boolValue ? (message["count"].intValue ?? 0) : nil
        case "teach_steps":
            if keepTeaching { taughtSteps = message["steps"].records }
            keepTeaching = false
        default: break
        }
    }

    /// The page's name for the bar: its title, else its address's host.
    var pageName: String {
        if !title.isEmpty { return title }
        if let host = URL(string: pageURL)?.host { return host }
        return pageURL.isEmpty ? "浏览器" : pageURL
    }
}

/// 浏览器 (in the companion's status sheet, and wherever it is shown): a live preview of the server's
/// browser with 打开浏览器 ↗. Full screen it is watched first; 接管控制 hands it to the user, whose
/// tools then appear (typing, taps, copy, paste); 交还控制 gives it back.
@MainActor
struct ComputerView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var stream = ComputerStream()
    /// Opens full screen at once (教它做 from 技能).
    var openAtOnce = false
    /// As a task in a list (the companion's page, as Muse shows it) instead of the big live card.
    var asTask = false
    @State private var fullScreen = false
    @State private var draftGoal = ""
    @State private var draftName = ""
    @State private var draftContent = ""
    @State private var reviewing = false
    @State private var pendingReview = false
    @State private var saving = false
    @State private var localError: String?
    @State private var wasBackgrounded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !stream.handoff.isEmpty {
                Label("\(store.companionName)需要你来一下：\(stream.handoff)", systemImage: "hand.raised")
                    .font(.subheadline).padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(CompanionPalette.primary.opacity(0.15), in: RoundedRectangle(cornerRadius: 16))
            }
            if asTask { taskList } else { preview }
            if let error = localError ?? stream.error {
                HStack {
                    Text(error).font(.footnote).foregroundStyle(.red)
                    Spacer()
                    Button("重试") { Task { await reconnect() } }.frame(minHeight: 44)
                }
            }
        }
        .task { connect(); if openAtOnce { fullScreen = true } }
        .onChange(of: store.connected) { _, connected in
            Task { if connected { await reconnect() } else { await stream.disconnect() } }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .background:
                wasBackgrounded = true
                Task { await stream.disconnect() }
            case .active:
                guard wasBackgrounded else { return }
                wasBackgrounded = false
                Task { await reconnect() }
            case .inactive:
                break
            @unknown default:
                break
            }
        }
        .onDisappear { if !fullScreen && !reviewing { Task { await stream.disconnect() } } }
        .onChange(of: stream.taughtSteps) { _, steps in
            guard !steps.isEmpty else { return }
            if fullScreen { pendingReview = true; fullScreen = false } else { reviewing = true }
        }
        .fullScreenCover(isPresented: $fullScreen, onDismiss: {
            if pendingReview { pendingReview = false; reviewing = true }
        }) {
            BrowserScreen(stream: stream) { fullScreen = false }
        }
        .sheet(isPresented: $reviewing) { teachingReview }
    }

    /// "1 个浏览器任务" with ＋, and the task: its page small, what state it is in, the site.
    private var taskList: some View {
        let open = !stream.idle || stream.image != nil
        return VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(open ? "1 个浏览器任务" : "没有浏览器任务").font(.system(size: 17, weight: .semibold))
                Spacer()
                Button { fullScreen = true; Haptics.selection() } label: {
                    Image(systemName: "plus").font(.system(size: 18, weight: .medium)).frame(width: 40, height: 40).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("打开浏览器")
            }
            if open {
                Button { fullScreen = true } label: {
                    HStack(spacing: 14) {
                        ZStack {
                            Color.black
                            if let image = stream.image { Image(uiImage: image).resizable().scaledToFill() }
                            else { ProgressView().tint(.white).scaleEffect(0.7) }
                        }
                        .frame(width: 66, height: 44).clipShape(RoundedRectangle(cornerRadius: 7))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(taskState).font(.system(size: 16, weight: .semibold))
                            Text(URL(string: stream.pageURL)?.host ?? stream.pageName).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("打开浏览器任务：\(taskState)")
            } else {
                Text("\(store.companionName)要用浏览器时会自动打开，你也可以点 ＋ 自己打开。").font(.system(size: 14)).foregroundStyle(.secondary)
            }
        }
    }

    private var taskState: String {
        if !stream.handoff.isEmpty { return "需要你来一下" }
        if stream.controlling { return "你在操作" }
        return store.running ? "进行中" : "任务已完成"
    }

    /// The live picture in a card, with 打开浏览器 ↗ (as Muse shows it).
    private var preview: some View {
        Button { fullScreen = true } label: {
            ZStack(alignment: .bottomTrailing) {
                LinearGradient(colors: [Color(red: 0.62, green: 0.82, blue: 0.98), Color(red: 0.55, green: 0.76, blue: 0.96)],
                               startPoint: .top, endPoint: .bottom)
                Group {
                    if let image = stream.image {
                        Image(uiImage: image).resizable().scaledToFit()
                            .clipShape(RoundedRectangle(cornerRadius: 6)).shadow(color: .black.opacity(0.15), radius: 6, y: 2)
                    } else {
                        VStack(spacing: 8) {
                            if stream.idle {
                                Image(systemName: "macwindow").font(.system(size: 34)).foregroundStyle(.white.opacity(0.9))
                                Text("浏览器已关闭，伙伴要用时会自动打开").font(.footnote).foregroundStyle(.white)
                            } else {
                                ProgressView().tint(.white)
                                Text("正在连接浏览器…").font(.footnote).foregroundStyle(.white)
                            }
                        }
                    }
                }.padding(.horizontal, 28).padding(.vertical, 22).frame(maxWidth: .infinity, maxHeight: .infinity)
                Label("打开浏览器", systemImage: "arrow.up.left.and.arrow.down.right")
                    .labelStyle(TitleThenIcon()).font(.system(size: 13, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(.white.opacity(0.92), in: Capsule()).foregroundStyle(.black)
                    .padding(12)
            }
            .aspectRatio(1.6, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 18))
        }.buttonStyle(.plain).accessibilityLabel("打开浏览器")
    }

    private var teachingReview: some View {
        NavigationStack {
            Form {
                Section("你教它做什么") {
                    TextField("例如：查询快递", text: $draftGoal)
                    Text("已记录 \(stream.taughtSteps.count) 步，服务器会跳过密码内容。先整理成草稿，检查后再保存。")
                        .font(.footnote).foregroundStyle(.secondary)
                    Button(saving ? "正在整理…" : "整理技能草稿") { Task { await draftSkill() } }.disabled(saving || draftGoal.isEmpty)
                }
                if !draftContent.isEmpty {
                    Section("检查草稿") {
                        TextField("技能名称", text: $draftName)
                        TextEditor(text: $draftContent).frame(minHeight: 240).font(.body.monospaced())
                        Button("保存技能") { Task { await saveSkill() } }.disabled(saving || draftName.isEmpty)
                    }
                }
                if let localError { Text(localError).foregroundStyle(.red) }
            }
            .navigationTitle("教它做").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { reviewing = false } } }
        }.presentationDetents([.large]).tint(CompanionPalette.primary)
    }

    private func connect() {
        guard store.connected else { localError = "连接伙伴后再打开浏览器"; return }
        localError = nil
        stream.connect { [weak store] in
            guard let store else { throw ServiceError.message("连接后再试一次") }
            return try store.makeComputerSocketTask()
        }
    }

    private func reconnect() async { await stream.disconnect(); connect() }

    private func draftSkill() async {
        saving = true; localError = nil
        defer { saving = false }
        do {
            let result = try await store.changeResource("companion.skills.change", payload: ["action": .string("draft"), "goal": .string(draftGoal), "steps": .array(stream.taughtSteps)])
            draftName = result["name"].stringValue; draftContent = result["content"].stringValue
        } catch { localError = error.localizedDescription }
    }

    private func saveSkill() async {
        saving = true; localError = nil
        defer { saving = false }
        do {
            _ = try await store.changeResource("companion.skills.change", payload: ["action": .string("save"), "name": .string(draftName), "content": .string(draftContent), "new": .bool(true)])
            await stream.send("done"); reviewing = false
        } catch { localError = error.localizedDescription }
    }
}

/// The browser full screen. Watching first (the companion works; 停止任务 / 接管控制); once taken
/// over: the tools (keyboard, tap, copy, paste, ···) and 交还控制. ✓ closes it (handing back too).
@MainActor
private struct BrowserScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @ObservedObject var stream: ComputerStream
    var onClose: () -> Void
    @State private var touch = true
    @State private var typing = ""
    @State private var resettingTyping = false
    /// Keystrokes go out one after another, in order (a backspace never overtakes the letter before it).
    @State private var typingQueue: Task<Void, Never>?
    @FocusState private var keyboardUp: Bool
    @State private var toast: String?
    @State private var toastID = UUID()
    @State private var confirmStop = false
    @State private var address = ""
    @FocusState private var editingAddress: Bool

    var body: some View {
        VStack(spacing: 0) {
            bar
            addressBar
            if stream.teachCount != nil { teachingBanner }
            page.frame(maxWidth: .infinity, maxHeight: .infinity)
            ZStack(alignment: .top) {
                toolbar.padding(.vertical, 12)
                if let toast {
                    Text(toast).font(.system(size: 13)).foregroundStyle(.white).padding(.horizontal, 12).padding(.vertical, 7)
                        .background(Color(white: 0.25), in: Capsule()).offset(y: -40).transition(.opacity)
                }
            }
            // The keyboard types straight into the page (a hidden field passes each change on).
            TextField("", text: $typing).focused($keyboardUp).frame(width: 1, height: 1).opacity(0.02)
                .textInputAutocapitalization(.never).autocorrectionDisabled().submitLabel(.return)
                .onChange(of: typing) { old, new in typed(from: old, to: new) }
                .onSubmit { Task { await stream.send("key", fields: ["key": .string("Enter")]) }; keyboardUp = true }
                .accessibilityHidden(true)
        }
        .background(Color(white: 0.07).ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onChange(of: stream.controlling) { _, controlling in if !controlling { keyboardUp = false } }
        .onChange(of: keyboardUp) { _, up in
            // The hidden field starts empty each time (quietly: emptying it is not a deletion).
            if !up && !typing.isEmpty { resettingTyping = true; typing = "" }
        }
        .onChange(of: stream.copied) { _, copied in
            guard let copied else { return }
            if copied.text.isEmpty { show("没有选中的文字，先在网页里选中一段再复制") }
            else { UIPasteboard.general.string = copied.text; show("已复制到手机"); Haptics.selection() }
        }
        .alert("停止这项任务？", isPresented: $confirmStop) {
            Button("取消", role: .cancel) {}
            Button("停止任务", role: .destructive) {
                Task {
                    await store.stopRunning()
                    await stream.send("close")  // the browser closes with the task
                    onClose()
                }
            }
        } message: { Text("\(store.companionName)会停在原地，浏览器也会关掉，其他不会有任何更改。") }
    }

    // MARK: bar

    /// The browser's own bar, always shown: back, forward, reload and the address. Watching, it only
    /// shows where the page is; taken over, the address can be changed (or words searched on Google).
    private var addressBar: some View {
        HStack(spacing: 2) {
            navButton("chevron.left", "后退") { send("back") }
            navButton("chevron.right", "前进") { send("forward") }
            navButton("arrow.clockwise", "刷新") { send("reload") }
            HStack(spacing: 6) {
                Image(systemName: stream.pageURL.hasPrefix("https://") ? "lock.fill" : "globe")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                if stream.controlling {
                    TextField("搜索或输入网址", text: $address)
                        .focused($editingAddress).keyboardType(.webSearch).submitLabel(.go)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit(openAddress)
                        .accessibilityLabel("网址")
                } else {
                    Text(stream.pageURL.isEmpty ? "about:blank" : stream.pageURL)
                        .lineLimit(1).truncationMode(.middle).frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityLabel("网址：\(stream.pageURL)")
                }
            }
            .font(.system(size: 14)).padding(.horizontal, 12).frame(height: 36)
            .background(Color(white: 0.18), in: RoundedRectangle(cornerRadius: 10))
        }
        .foregroundStyle(.white).padding(.horizontal, 8).padding(.bottom, 8)
        .onChange(of: stream.pageURL) { _, url in if !editingAddress { address = url } }
        .onChange(of: editingAddress) { _, editing in if !editing { address = stream.pageURL } }
        .onAppear { address = stream.pageURL }
    }

    private func navButton(_ icon: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon).font(.system(size: 16, weight: .medium)).frame(width: 36, height: 36) }
            .buttonStyle(.plain).disabled(!stream.controlling).opacity(stream.controlling ? 1 : 0.35).accessibilityLabel(label)
    }

    /// The address bar's go: an address opens; anything else is searched on Google.
    private func openAddress() {
        let text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let looksLikeAddress = !text.contains(" ") && (text.contains("://") || text.contains("."))
        let url = looksLikeAddress ? text
            : "https://www.google.com/search?q=" + (text.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? text)
        editingAddress = false
        Task { await stream.send("navigate", fields: ["url": .string(url)]) }
    }

    private var bar: some View {
        ZStack {
            VStack(spacing: 2) {
                Text(stream.controlling ? "全由你掌控" : (stream.handoff.isEmpty ? "\(store.companionName)在操作" : "\(store.companionName)需要你来一下"))
                    .font(.system(size: 16, weight: .semibold))
                Text(stream.handoff.isEmpty || stream.controlling ? stream.pageName : stream.handoff)
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }.frame(maxWidth: 220)
            HStack {
                if stream.controlling {
                    Button { handBack() } label: {
                        Label("交还控制", systemImage: "arrow.uturn.backward").font(.system(size: 13, weight: .semibold))
                            .padding(.horizontal, 12).frame(height: 36).background(Color(white: 0.2), in: Capsule())
                    }.buttonStyle(.plain).accessibilityLabel("交还控制")
                }
                Spacer()
                Button { finish() } label: {
                    Image(systemName: "checkmark").font(.system(size: 17, weight: .semibold)).frame(width: 40, height: 40)
                        .background(Color(white: 0.2), in: Circle())
                }.buttonStyle(.plain).accessibilityLabel("完成")
            }
        }
        .foregroundStyle(.white).padding(.horizontal, 14).frame(height: 60)
    }

    private var teachingBanner: some View {
        HStack(spacing: 10) {
            Circle().fill(Color.red).frame(width: 8, height: 8)
            Text("正在记下你的操作 · \(stream.teachCount ?? 0) 步").font(.system(size: 13))
            Spacer()
            Button("不记了") { Task { await stream.stopTeaching(keep: false) } }.font(.system(size: 13))
            Button("做完了") { Task { await stream.stopTeaching(keep: true) } }.font(.system(size: 13, weight: .semibold))
        }
        .foregroundStyle(.white).padding(.horizontal, 16).padding(.vertical, 8).background(Color(white: 0.16))
    }

    // MARK: page

    @ViewBuilder private var page: some View {
        if let image = stream.image {
            GeometryReader { geometry in
                let fitted = fit(image.size, in: geometry.size, ratio: stream.aspectRatio)
                Image(uiImage: image).resizable().scaledToFit()
                    .frame(width: fitted.width, height: fitted.height)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 0).onEnded { value in pageGesture(value, size: fitted) },
                             including: stream.controlling && touch ? .all : .subviews)
                    .frame(width: geometry.size.width, height: geometry.size.height)
                    .accessibilityLabel(stream.controlling ? "网页画面：点按点击，拖动翻页" : "网页画面")
            }
        } else {
            VStack(spacing: 14) {
                if stream.idle {
                    Text("浏览器已关闭，\(store.companionName)要用时会自动打开").font(.subheadline).foregroundStyle(.secondary)
                    Button("现在打开") { Task { await stream.send("start") } }
                        .font(.system(size: 15, weight: .semibold)).padding(.horizontal, 20).frame(minHeight: 40)
                        .background(Color(white: 0.2), in: Capsule()).foregroundStyle(.white).buttonStyle(.plain)
                } else {
                    ProgressView().tint(.white)
                    Text("正在启动浏览器…").font(.subheadline).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func fit(_ size: CGSize, in box: CGSize, ratio: CGFloat) -> CGSize {
        let r = ratio > 0 ? ratio : max(1, size.width) / max(1, size.height)
        let width = min(box.width, box.height * r)
        return CGSize(width: width, height: width / r)
    }

    private func pageGesture(_ value: DragGesture.Value, size: CGSize) {
        guard stream.controlling, touch else { return }
        let w = max(1, size.width), h = max(1, size.height)
        Task {
            if hypot(value.translation.width, value.translation.height) < 8 {
                await stream.send("click", fields: ["x": .number(Double(min(1, max(0, value.location.x / w)))),
                                                    "y": .number(Double(min(1, max(0, value.location.y / h))))])
            } else {
                await stream.send("scroll", fields: ["dy": .number(Double(-value.translation.height / h))])
            }
        }
    }

    // MARK: toolbar

    @ViewBuilder private var toolbar: some View {
        HStack(spacing: 10) {
            Button { confirmStop = true } label: {
                Text("停止任务").font(.system(size: 15, weight: .medium)).padding(.horizontal, 18).frame(height: 44)
                    .background(Color(white: 0.2), in: Capsule())
            }.buttonStyle(.plain)
            if stream.controlling {
                HStack(spacing: 0) {
                    tool("keyboard", "键盘", on: keyboardUp) { keyboardUp.toggle() }
                    tool(touch ? "hand.tap.fill" : "hand.raised.slash", touch ? "点按操作（已开）" : "点按操作（已关）", on: touch) { touch.toggle() }
                    tool("doc.on.doc", "复制选中文字", on: false) { copy() }
                    tool("doc.on.clipboard", "粘贴手机上的文字", on: false) { paste() }
                    Menu {
                        Menu {
                            ForEach(Self.keys, id: \.self) { key in
                                Button(Self.keyNames[key] ?? key) { Task { await stream.send("key", fields: ["key": .string(key)]) } }
                            }
                        } label: { Label("按键", systemImage: "command") }
                        if stream.teachCount == nil {
                            Button { send("teach_start") } label: { Label("把接下来的操作教给\(store.companionName)", systemImage: "graduationcap") }
                        }
                        Button(role: .destructive) { send("close"); onClose() } label: { Label("关闭浏览器", systemImage: "xmark.circle") }
                    } label: { Image(systemName: "ellipsis").font(.system(size: 18)).frame(width: 44, height: 44) }
                        .accessibilityLabel("更多操作")
                }
                .padding(.horizontal, 4).frame(height: 44).background(Color(white: 0.2), in: Capsule())
            } else {
                Button { take() } label: {
                    Label("接管控制", systemImage: "hand.tap").font(.system(size: 15, weight: .semibold)).padding(.horizontal, 18).frame(height: 44)
                        .background(Color.white, in: Capsule()).foregroundStyle(.black)
                }.buttonStyle(.plain)
            }
        }.foregroundStyle(.white)
    }

    private static let keys = ["Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace"]
    private static let keyNames = ["Tab": "下一项（Tab）", "Escape": "退出（Esc）", "ArrowUp": "↑", "ArrowDown": "↓",
                                   "ArrowLeft": "←", "ArrowRight": "→", "Backspace": "退格"]

    private func tool(_ icon: String, _ label: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 17)).frame(width: 42, height: 40)
                .background(on ? Color.white.opacity(0.18) : .clear, in: Capsule())
        }.buttonStyle(.plain).accessibilityLabel(label)
    }

    // MARK: actions

    private func send(_ type: String) { Task { await stream.send(type) } }

    private func take() {
        Haptics.selection()
        Task { await stream.send("take") }
    }

    /// 交还控制: the companion goes on from here; the view stays open to watch.
    private func handBack() {
        keyboardUp = false
        Haptics.selection()
        Task {
            if stream.teachCount != nil { await stream.stopTeaching(keep: false) }
            await stream.send("done")
        }
    }

    /// ✓: closes the view; control, if taken, goes back with it.
    private func finish() {
        keyboardUp = false
        if stream.controlling { Task { await stream.send("done") } }
        onClose()
    }

    private func copy() {
        show("正在复制…")
        Task { await stream.send("copy") }
    }

    private func paste() {
        guard let text = UIPasteboard.general.string, !text.isEmpty else { show("剪贴板是空的，先在手机上复制文字"); return }
        Task { await stream.send("text", fields: ["text": .string(String(text.prefix(2000)))]) }
        show("已粘贴")
    }

    /// What was typed on the phone's keyboard goes into the page as it changes.
    private func typed(from old: String, to new: String) {
        if resettingTyping { resettingTyping = false; return }
        let previous = typingQueue
        typingQueue = Task {
            await previous?.value
            let oldChars = Array(old)
            let newChars = Array(new)
            var common = 0
            let limit = min(oldChars.count, newChars.count)
            while common < limit && oldChars[common] == newChars[common] {
                common += 1
            }
            let backspaces = max(0, oldChars.count - common)
            for _ in 0..<backspaces {
                await stream.send("key", fields: ["key": .string("Backspace")])
            }
            if common < newChars.count {
                let added = String(newChars[common...])
                if !added.isEmpty { await stream.send("text", fields: ["text": .string(added)]) }
            }
        }
        // Keep the hidden field short (its text is only a go-between).
        if new.count > 200 { resettingTyping = true; typing = "" }
    }

    private func show(_ text: String) {
        let id = UUID()
        toastID = id
        withAnimation(.easeOut(duration: 0.15)) { toast = text }
        Task {
            try? await Task.sleep(for: .seconds(2.2))
            if toastID == id { withAnimation(.easeOut(duration: 0.2)) { toast = nil } }
        }
    }
}

/// "打开浏览器 ↗": the words, then the icon.
private struct TitleThenIcon: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) { configuration.title; configuration.icon.imageScale(.small) }
    }
}
