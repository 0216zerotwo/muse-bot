import SwiftUI
import UIKit
import PhotosUI

/// 帮手, as the web's muse/Helpers: one list of conversations like a chat app's — helpers and groups
/// together, the latest on top, a red dot on a face with something new. ＋ makes a helper (say what it
/// is for, or start from a template), a group, or imports shared settings. Swipe a row for 编辑 / 删除.
@MainActor
struct HelpersScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = TabResource()
    @State private var sheet: HelpersSheet?
    @State private var chatting: HelperChatTarget?
    @State private var deleting: TabRecord?
    @State private var seen: [String: Double] = [:]
    @State private var previousChat = ""
    @State private var openChat = ""
    var onChat: () -> Void = {}

    private var helpers: [JSONValue] { resource.payload["helpers"].array }
    private var full: Bool { resource.hasLoaded && helpers.count >= (resource.payload["max"].intValue ?? 99) }
    private var seenKey: String { "companion.helpers.seen." + store.serverAddress }

    var body: some View {
        let helperList = helpers
        let rows = conversations(helperList)
        let working = helperList.contains { $0["working"].boolValue }
        List {
            heading.listRowSeparator(.hidden).listRowInsets(EdgeInsets()).listRowBackground(Color.clear)
            if let error = resource.error {
                Text(error).font(.subheadline).foregroundStyle(.red).listRowBackground(Color.clear)
            }
            if resource.hasLoaded && rows.isEmpty {
                Text("点右上角的 + 建一个帮手：跟它聊几句，它就知道自己该干什么。")
                    .font(.system(size: 15)).foregroundStyle(.secondary).listRowSeparator(.hidden).listRowBackground(Color.clear)
            }
            ForEach(rows) { row in
                conversationRow(row, helpers: helperList)
                    .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 0, trailing: 16))
                    .listRowBackground(Color.clear)
                    .listRowSeparatorTint(CompanionPalette.border)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button("删除", role: .destructive) { deleting = row }
                        Button("编辑") { edit(row) }.tint(.gray)
                    }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .contentMargins(.top, 160, for: .scrollContent)
        .contentMargins(.bottom, 24, for: .scrollContent)
        .refreshable { await load() }
        .task { await load() }
        // While one is working, look again now and then: it finishes on its own.
        .task(id: working) {
            guard working else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if Task.isCancelled { break }
                await load()
            }
        }
        .sheet(item: $sheet) { which in sheetContent(which).tint(CompanionPalette.primary) }
        .fullScreenCover(item: $chatting, onDismiss: chatClosed) { target in
            HelperChatScreen(chatID: target.id, resource: resource)
        }
        .alert(deleting.map { "删除「\($0["name"].stringValue)」？" } ?? "", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            Button("取消", role: .cancel) { deleting = nil }
            Button("删除", role: .destructive) { if let row = deleting { Task { await remove(row) } }; deleting = nil }
        } message: {
            Text(deleting?["members"].array.isEmpty == false ? "群里的聊天记录会一起删掉，帮手们还在。"
                 : "它的聊天记录、工作笔记、摸清的习惯和它定的例行会一起删掉，删了找不回来。")
        }
    }

    private var heading: some View {
        HStack {
            Text("帮手").font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            Spacer()
            Menu {
                if !full { Button { sheet = .newHelper } label: { Label("新建帮手", systemImage: "person.badge.plus") } }
                if helpers.count >= 2 { Button { sheet = .group(nil) } label: { Label("发起群聊", systemImage: "person.2") } }
                if !full { Button { sheet = .importing } label: { Label("导入设定", systemImage: "square.and.arrow.down") } }
            } label: {
                Image(systemName: "plus").font(.title2).frame(width: 44, height: 44).background(CompanionPalette.elevated, in: Circle())
            }.accessibilityLabel("新建")
        }.padding(.horizontal, 20).padding(.vertical, 12)
    }

    // MARK: rows

    private func conversations(_ helpers: [JSONValue]) -> [TabRecord] {
        let all = TabRecords.items(.object(["rows": .array(helpers + resource.payload["groups"].array)]), key: "rows")
        func at(_ row: TabRecord) -> Double { row["last"]["at"].doubleValue ?? row["created_at"].doubleValue ?? 0 }
        return all.sorted { at($0) > at($1) }
    }

    private func conversationRow(_ row: TabRecord, helpers: [JSONValue]) -> some View {
        let isGroup = !row["members"].array.isEmpty
        let last = row["last"]
        let lastAt = last["at"].doubleValue ?? 0
        let chat = row["chat_id"].stringValue
        let unread = last != .null && last["who"].stringValue != "我" && lastAt > max(seen["*"] ?? 0, seen[chat] ?? 0)
        let working = !isGroup && row["working"].boolValue
        let preview: String
        if working { preview = "正在做事…" }
        else if !isGroup && row["setup"].boolValue { preview = "还没设定好，点进去跟它聊" }
        else if let text = last["text"].nonemptyString {
            let who = last["who"].stringValue
            preview = who.isEmpty ? text : "\(who)：\(text)"
        } else {
            preview = isGroup ? "\(row["members"].array.count) 位帮手，还没聊过，说一句大家都能看到" : (row["title"].nonemptyString ?? "还没聊过")
        }
        return Button { open(chat) } label: {
            HStack(spacing: 12) {
                ZStack(alignment: .topTrailing) {
                    if isGroup { GroupFace(members: members(of: row, in: helpers), size: 48, baseURL: store.baseURL) }
                    else { HelperAvatar(emoji: row["emoji"].stringValue, avatar: row["avatar"].stringValue, size: 48, baseURL: store.baseURL) }
                    if unread {
                        Circle().fill(Color.red).frame(width: 12, height: 12)
                            .overlay(Circle().strokeBorder(CompanionPalette.background, lineWidth: 2)).offset(x: 2, y: -2)
                            .accessibilityLabel("有新消息")
                    }
                }
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        (Text(row["name"].stringValue).font(.system(size: 16.5, weight: .semibold))
                         + Text(isGroup ? "（\(row["members"].array.count + 1)）" : "").font(.system(size: 16.5)).foregroundColor(.secondary))
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        if lastAt > 0 { Text(chatTime(lastAt)).font(.system(size: 12)).foregroundStyle(.secondary) }
                    }
                    Text(preview).font(.system(size: 14)).lineLimit(1)
                        .foregroundStyle(working ? CompanionPalette.primary : Color.secondary)
                }
            }.padding(.vertical, 12).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func members(of group: TabRecord, in helpers: [JSONValue]) -> [JSONValue] {
        group["members"].array.compactMap { id in helpers.first { $0["id"] == id } }
    }

    // MARK: sheets

    @ViewBuilder private func sheetContent(_ which: HelpersSheet) -> some View {
        switch which {
        case .newHelper:
            NewHelperSheet(resource: resource) { opened in sheet = nil; if let opened { openSoon(opened) } }
        case .importing:
            ImportHelperSheet { fields in sheet = .imported(fields) }
        case .imported(let fields):
            NavigationStack {
                HelperEditorView(start: fields, helperID: nil, resource: resource, canReset: false, title: "导入的帮手",
                                 onSave: { fields in
                                     var payload = fields
                                     payload["action"] = .string("create")
                                     return await resource.change("companion.helpers.change", payload: payload, store: store)
                                 }, onDone: { sheet = nil })
            }
        case .profile(let chatID):
            HelperProfileView(chatID: chatID, resource: resource, onClose: { sheet = nil })
        case .group(let group):
            GroupSheet(group: group, resource: resource) { opened in sheet = nil; if let opened { openSoon(opened) } }
        }
    }

    private func edit(_ row: TabRecord) {
        sheet = row["members"].array.isEmpty ? .profile(row["chat_id"].stringValue) : .group(row)
    }

    // MARK: actions

    private func load() async {
        seen = UserDefaults.standard.dictionary(forKey: seenKey) as? [String: Double] ?? [:]
        // The first look at the list: what was there before counts as read.
        if seen["*"] == nil { seen["*"] = Date().timeIntervalSince1970 * 1_000; UserDefaults.standard.set(seen, forKey: seenKey) }
        await resource.load("/api/webui/companion/helpers", store: store)
        if resource.hasLoaded, store.helpers != helpers { store.helpers = helpers }
    }

    private func markSeen(_ chat: String) {
        seen[chat] = Date().timeIntervalSince1970 * 1_000
        UserDefaults.standard.set(seen, forKey: seenKey)
    }

    /// The conversation opens at once, full screen like a chat app's; its history loads as it slides in.
    private func open(_ chat: String) {
        guard !chat.isEmpty else { resource.error = "这个帮手还没有聊天，刷新后重试。"; return }
        if previousChat.isEmpty { previousChat = store.currentChatID }
        markSeen(chat)
        openChat = chat
        chatting = HelperChatTarget(id: chat)
        Task { await store.changeChat(chat) }
    }

    /// After a sheet closes (a new helper or group): its chat.
    private func openSoon(_ chat: String) {
        Task { try? await Task.sleep(for: .milliseconds(450)); open(chat) }
    }

    private func chatClosed() {
        // What came in while it was open counts as read (the item is already cleared here).
        if !openChat.isEmpty { markSeen(openChat); openChat = "" }
        let old = previousChat
        previousChat = ""
        Task {
            if !old.isEmpty { await store.changeChat(old) }
            await load()
        }
    }

    private func remove(_ row: TabRecord) async {
        let isGroup = !row["members"].array.isEmpty
        await resource.change(isGroup ? "companion.groups.change" : "companion.helpers.change",
                              payload: ["action": .string("delete"), "id": .string(row.id)], store: store)
    }
}

private enum HelpersSheet: Identifiable {
    case newHelper, importing, imported([String: JSONValue]), profile(String), group(TabRecord?)
    var id: String {
        switch self {
        case .newHelper: return "new"
        case .importing: return "import"
        case .imported: return "imported"
        case .profile(let chat): return "profile:" + chat
        case .group(let group): return "group:" + (group?.id ?? "new")
        }
    }
}

struct HelperChatTarget: Identifiable { let id: String }

// MARK: - 新建帮手 / 导入

/// New: say what it is for (it works out the rest and proposes it in its chat), or start from a template.
@MainActor
private struct NewHelperSheet: View {
    @EnvironmentObject private var store: CompanionStore
    @ObservedObject var resource: TabResource
    var onMade: (String?) -> Void
    @State private var idea = ""

    private var unused: [JSONValue] {
        let names = Set(resource.payload["helpers"].array.map { $0["name"].stringValue })
        return resource.payload["templates"].array.filter { !names.contains($0["name"].stringValue) }
    }

    var body: some View {
        SheetPage(title: "新建帮手") {
            VStack(alignment: .leading, spacing: 10) {
                TextField("给它起个名字，或说说它负责什么", text: $idea).submitLabel(.go)
                    .onSubmit { Task { await make(template: nil) } }.modifier(HelperField())
                Text("比如「邮件分拣」「盯着我的快递」「健身教练」，它会自己想好怎么做，给你过目。")
                    .font(.system(size: 12.5)).foregroundStyle(.secondary).padding(.horizontal, 4)
                PrimaryButton(title: idea.trimmingCharacters(in: .whitespaces).isEmpty ? "聊着设定一个新帮手" : "建好，让它出个方案",
                              icon: "message", busy: resource.busy) { Task { await make(template: nil) } }
                if let error = resource.error { Text(error).font(.subheadline).foregroundStyle(.red) }
                if !unused.isEmpty {
                    Text("或者从这些开始").font(.system(size: 13)).foregroundStyle(.secondary).padding(.horizontal, 4).padding(.top, 8)
                    ForEach(unused, id: \.selfID) { template in
                        Button { Task { await make(template: template) } } label: {
                            HStack(spacing: 12) {
                                HelperAvatar(emoji: template["emoji"].stringValue, size: 40, baseURL: store.baseURL)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(template["name"].stringValue).font(.system(size: 15, weight: .semibold))
                                    Text(template["title"].stringValue).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                            }.padding(12).background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18)).contentShape(Rectangle())
                        }.buttonStyle(.plain).disabled(resource.busy)
                    }
                }
            }
        }
    }

    private func make(template: JSONValue?) async {
        var payload: [String: JSONValue]
        if let template {
            payload = template.object.filter { ["name", "emoji", "title", "instructions"].contains($0.key) }
            payload["action"] = .string("create")
        } else {
            payload = ["action": .string("blank"), "idea": .string(idea.trimmingCharacters(in: .whitespaces))]
        }
        if await resource.change("companion.helpers.change", payload: payload, store: store) {
            onMade(resource.payload["opened"].nonemptyString)
        }
    }
}

/// 导入设定: settings someone shared, checked, then shown in the editor to look over before saving.
@MainActor
private struct ImportHelperSheet: View {
    var onNext: ([String: JSONValue]) -> Void
    @State private var pasted = ""
    var body: some View {
        let parsed = HelperShare.parse(pasted)
        SheetPage(title: "导入设定") {
            VStack(alignment: .leading, spacing: 12) {
                TextField("把别人「分享设定」给你的内容粘贴在这里", text: $pasted, axis: .vertical).lineLimit(7...10)
                    .font(.system(size: 14)).modifier(HelperField(vertical: true))
                if !pasted.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && parsed == nil {
                    Text("这不像是一份帮手设定。").font(.system(size: 13.5)).foregroundStyle(.red)
                }
                PrimaryButton(title: "下一步：看一眼再保存", enabled: parsed != nil) { if let parsed { onNext(parsed) } }
            }
        }
    }
}

/// A helper's settings as text to send someone, and back (as the web's helperShareText / parseHelperShare).
enum HelperShare {
    static func text(_ helper: JSONValue) -> String {
        let json: JSONValue = .object(["helper": .number(1), "name": helper["name"], "emoji": helper["emoji"],
                                       "title": helper["title"], "instructions": helper["instructions"]])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .withoutEscapingSlashes]
        return (try? encoder.encode(json)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }

    static func parse(_ text: String) -> [String: JSONValue]? {
        guard let start = text.firstIndex(of: "{"), let end = text.lastIndex(of: "}"), start < end,
              let value = try? JSONValue.parse(Data(String(text[start...end]).utf8)),
              let name = value["name"].nonemptyString else { return nil }
        func field(_ key: String, _ limit: Int) -> String { String(value[key].stringValue.trimmingCharacters(in: .whitespacesAndNewlines).prefix(limit)) }
        let emoji = field("emoji", 4)
        return ["name": .string(String(name.prefix(12))), "emoji": .string(emoji.isEmpty ? "🤖" : emoji),
                "title": .string(field("title", 16)), "instructions": .string(field("instructions", 2000))]
    }
}

// MARK: - pieces shared by the helper sheets

/// A sheet's page: title in the bar, × to close, content scrolling below.
struct SheetPage<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            ScrollView { content.padding(20) }
                .scrollDismissesKeyboard(.interactively)
                .background(CompanionPalette.surface)
                .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭")
                } }
        }
    }
}

/// A labelled field with a line under it saying what it is for (as the web's Field).
struct HelperFieldBlock<Content: View>: View {
    let label: String
    let hint: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 14, weight: .semibold))
            content
            Text(hint).font(.system(size: 12.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct HelperField: ViewModifier {
    var vertical = false
    func body(content: Content) -> some View {
        content.padding(.horizontal, 14).padding(.vertical, vertical ? 12 : 0).frame(minHeight: 46)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 16))
    }
}

struct PrimaryButton: View {
    let title: String
    var icon: String?
    var enabled = true
    var busy = false
    var danger = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy { ProgressView().controlSize(.small) } else if let icon { Image(systemName: icon) }
                Text(title)
            }
            .font(.system(size: 15, weight: .semibold)).frame(maxWidth: .infinity).frame(minHeight: 46)
            .background(danger ? Color.red : CompanionPalette.primary, in: Capsule())
            .foregroundStyle(danger ? Color.white : CompanionPalette.primaryText)
            .opacity(enabled && !busy ? 1 : 0.4)
        }.buttonStyle(.plain).disabled(!enabled || busy)
    }
}

struct SecondaryButton: View {
    let title: String
    var icon: String?
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) { if let icon { Image(systemName: icon) }; Text(title) }
                .font(.system(size: 15)).frame(maxWidth: .infinity).frame(minHeight: 44)
                .background(Color.primary.opacity(0.07), in: Capsule()).foregroundStyle(.primary)
        }.buttonStyle(.plain)
    }
}
