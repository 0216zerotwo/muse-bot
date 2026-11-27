import SwiftUI

@MainActor
struct SideMenu: View {
    @EnvironmentObject private var store: CompanionStore
    @Binding var selectedTab: CompanionTab
    var order: [CompanionTab]
    var onReorder: ([CompanionTab]) -> Void
    var onClose: () -> Void
    var onSettings: () -> Void
    @State private var query = ""
    @State private var renaming: SideChat?
    @State private var title = ""
    @State private var showArchived = false
    @State private var confirmPrune = false
    @State private var pruning = false
    @State private var menuNotice: String?
    /// 聊天记录: messages that say what is typed in 搜索 (any chat), and the words they were found for.
    @State private var hits: [JSONValue] = []
    @State private var searched = ""
    @State private var searching = false

    private var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var matchingChats: [SideChat] {
        let search = trimmedQuery
        return search.isEmpty
            ? store.sideChats
            : store.sideChats.filter { ($0.title + " " + $0.preview).localizedCaseInsensitiveContains(search) }
    }

    private var activeChats: [SideChat] { matchingChats.filter { !$0.isArchived } }
    private var pinnedChats: [SideChat] { activeChats.filter(\.isPinned) }
    private var regularChats: [SideChat] { activeChats.filter { !$0.isPinned } }
    private var archivedChats: [SideChat] { matchingChats.filter(\.isArchived) }
    private var emptyPrunableCount: Int {
        store.sideChats.filter { $0.isEmptyChat && !$0.isPinned }.count
    }

    var body: some View {
        VStack(spacing: 0) {
            menuHeader
            if let menuNotice {
                noticeBar(menuNotice)
            }
            menuList
            bottomBar
        }
        .task(id: query) { await searchMessages() }
        .alert("旁聊名称", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("名称", text: $title)
            Button("取消", role: .cancel) { renaming = nil }
            Button("保存") {
                if let chat = renaming { Task { await store.renameChat(id: chat.id, title: title) } }
                renaming = nil
            }
        }
        .confirmationDialog(
            "清理空旁聊",
            isPresented: $confirmPrune,
            titleVisibility: .visible
        ) {
            Button("清理 \(emptyPrunableCount) 个空旁聊", role: .destructive) {
                Task { await runPruneEmpty() }
            }
            Button("取消", role: .cancel) { }
        } message: {
            Text("仅清理没有消息且未置顶的空旁聊，主对话与已置顶旁聊会被保留。")
        }
    }

    private var menuHeader: some View {
        HStack(alignment: .top) {
            Text(store.companionName)
                .font(.system(size: 32, weight: .bold))
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onClose) {
                Image(systemName: "xmark").frame(width: 44, height: 44)
            }
            .accessibilityLabel("关闭菜单")
        }
        .padding(.horizontal, 24).padding(.top, 32).padding(.bottom, 20)
        .gesture(DragGesture(minimumDistance: 25).onEnded { if $0.translation.width < -65 { onClose() } })
    }

    private func noticeBar(_ text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(CompanionPalette.primary)
            Text(text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 8)
    }

    private var menuList: some View {
        List {
            // When searching, surface cross-session message hits at the very top.
            if !trimmedQuery.isEmpty {
                searchHitsSection
            }
            if trimmedQuery.isEmpty {
                tabsSection
            }
            if !pinnedChats.isEmpty {
                pinnedChatsSection
            }
            sideChatsSection
            if !archivedChats.isEmpty {
                archivedChatsSection
            }
            if !trimmedQuery.isEmpty {
                tabsSection
            }
        }
        .listStyle(.plain).scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 44)
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
    }

    private var bottomBar: some View {
        HStack(spacing: 8) {
            Button(action: onSettings) {
                Image(systemName: "gearshape").font(.system(size: 23)).frame(width: 48, height: 48).modifier(FloatingPanel(radius: 24))
            }
            .accessibilityLabel("设置")
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary).accessibilityHidden(true)
                TextField("搜索", text: $query).accessibilityLabel("搜索旁聊和聊天记录").submitLabel(.search)
                if !query.isEmpty {
                    Button {
                        query = ""
                        hits = []
                        searched = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 16))
                            .foregroundStyle(.tertiary)
                            .frame(width: 28, height: 28)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("清除搜索词")
                }
            }
            .padding(.horizontal, 12).frame(height: 48).modifier(FloatingPanel(radius: 24))
            Button {
                selectedTab = .chat; Task { await store.createChat() }; onClose(); Haptics.selection()
            } label: {
                Image(systemName: "square.and.pencil").font(.system(size: 22)).frame(width: 48, height: 48).modifier(FloatingPanel(radius: 24))
            }
            .accessibilityLabel("新开旁聊")
        }
        .padding(12)
    }

    private var searchHitsSection: some View {
        Section {
            if searching && searched != trimmedQuery {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在搜索聊天记录…").font(.subheadline).foregroundStyle(.secondary)
                }
                .padding(.vertical, 6)
            } else if !searched.isEmpty && hits.isEmpty {
                Text("聊天记录里没有「\(searched)」").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 8)
            } else {
                ForEach(Array(hits.enumerated()), id: \.offset) { _, hit in
                    Button { openHit(hit) } label: { hitRow(hit) }.buttonStyle(.plain)
                }
            }
        } header: {
            HStack {
                Text("聊天记录")
                if !hits.isEmpty && !searching {
                    Text("\(hits.count)")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .textCase(nil)
            .padding(.top, 4)
        }
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
    }

    private var tabsSection: some View {
        Section {
            ForEach(order) { tab in
                Button {
                    selectedTab = tab; Haptics.selection()
                    if tab == .chat && !store.mainChatID.isEmpty { Task { await store.changeChat(store.mainChatID) } }
                    onClose()
                } label: {
                    Label { Text(tab.rawValue).font(.system(size: 18)) } icon: {
                        Image(systemName: tab.icon).font(.system(size: 24)).frame(width: 32)
                    }.foregroundStyle(.primary)
                        .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
                        .padding(.horizontal, 16)
                        .background(selectedTab == tab && (tab != .chat || store.currentChatID == store.mainChatID) ? CompanionPalette.elevated : .clear, in: Capsule())
                }.buttonStyle(.plain)
                    .contextMenu {
                        Button("上移", systemImage: "arrow.up") { move(tab, by: -1) }.disabled(order.first == tab)
                        Button("下移", systemImage: "arrow.down") { move(tab, by: 1) }.disabled(order.last == tab)
                    }
            }.onMove { from, to in var updated = order; updated.move(fromOffsets: from, toOffset: to); onReorder(updated) }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 0, leading: 12, bottom: 0, trailing: 12))
        } header: {
            VStack(alignment: .leading, spacing: 8) {
                Text("选项卡").font(.caption)
                Text("你可以长按来重新排序").font(.subheadline)
            }.foregroundStyle(.secondary).textCase(nil).padding(.bottom, 8)
        }
    }

    private var pinnedChatsSection: some View {
        Section {
            ForEach(pinnedChats) { chat in
                sideChatRow(chat, pinnedBadge: true)
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } header: {
            Text("📌 置顶旁聊").textCase(nil).padding(.top, 12)
        }
    }

    private var sideChatsSection: some View {
        Section {
            if regularChats.isEmpty && pinnedChats.isEmpty {
                Text(trimmedQuery.isEmpty ? "还没有旁聊" : "没有找到")
                    .font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 8)
            } else if regularChats.isEmpty && !pinnedChats.isEmpty {
                Text("其余旁聊都在上方置顶区")
                    .font(.caption).foregroundStyle(.tertiary).padding(.vertical, 4)
            }
            ForEach(regularChats) { chat in
                sideChatRow(chat, pinnedBadge: false)
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } header: {
            HStack(spacing: 8) {
                Text("旁聊")
                Spacer()
                if trimmedQuery.isEmpty && store.connected && emptyPrunableCount > 0 {
                    Button {
                        confirmPrune = true
                        Haptics.selection()
                    } label: {
                        HStack(spacing: 4) {
                            if pruning { ProgressView().controlSize(.mini) }
                            else { Image(systemName: "sparkles").font(.caption2) }
                            Text(pruning ? "清理中…" : "清理空旁聊")
                                .font(.caption.weight(.medium))
                        }
                        .foregroundStyle(CompanionPalette.primary)
                    }
                    .buttonStyle(.plain)
                    .disabled(pruning)
                    .accessibilityLabel("清理空旁聊")
                }
            }
            .textCase(nil)
            .padding(.top, 12)
        }
    }

    private var archivedChatsSection: some View {
        let expanded = showArchived || !trimmedQuery.isEmpty
        return Section {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { showArchived.toggle() }
                Haptics.selection()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "archivebox").font(.subheadline).foregroundStyle(.secondary)
                    Text("已归档旁聊（\(archivedChats.count)）")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .frame(minHeight: 40)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("展开或收起已归档旁聊")

            if expanded {
                ForEach(archivedChats) { chat in
                    sideChatRow(chat, pinnedBadge: chat.isPinned)
                        .opacity(0.82)
                }
            }
        }
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
    }

    private func sideChatRow(_ chat: SideChat, pinnedBadge: Bool) -> some View {
        let active = selectedTab == .chat && store.currentChatID == chat.id
        return Button {
            selectedTab = .chat
            Task { await store.changeChat(chat.id) }
            onClose()
            Haptics.selection()
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if pinnedBadge {
                        Image(systemName: "pin.fill")
                            .font(.caption)
                            .foregroundStyle(CompanionPalette.primary)
                            .accessibilityHidden(true)
                    }
                    Text(chat.title.isEmpty ? "新的旁聊" : chat.title)
                        .font(.system(size: 17.5, weight: active ? .semibold : .regular))
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let preset = chat.modelPreset, !preset.isEmpty {
                    Text(preset)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, minHeight: 46, alignment: .leading)
            .background(active ? CompanionPalette.elevated : .clear, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            Button {
                Haptics.selection()
                Task { await store.togglePinChat(chatId: chat.id, pinned: !chat.isPinned) }
            } label: {
                Label(chat.isPinned ? "取消置顶" : "置顶", systemImage: chat.isPinned ? "pin.slash" : "pin.fill")
            }
            .tint(CompanionPalette.primary)
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button("删除", role: .destructive) { Task { await store.deleteChat(id: chat.id) } }
            Button(chat.isArchived ? "取消归档" : "归档") {
                Haptics.selection()
                Task { await store.toggleArchiveChat(chatId: chat.id, archived: !chat.isArchived) }
            }
            .tint(.indigo)
            Button("改名") { title = chat.title; renaming = chat }.tint(.orange)
        }
        .contextMenu {
            Button(chat.isPinned ? "取消置顶" : "置顶旁聊", systemImage: chat.isPinned ? "pin.slash" : "pin") {
                Haptics.selection()
                Task { await store.togglePinChat(chatId: chat.id, pinned: !chat.isPinned) }
            }
            Button(chat.isArchived ? "移出归档" : "归档旁聊", systemImage: chat.isArchived ? "tray.and.arrow.up" : "archivebox") {
                Haptics.selection()
                Task { await store.toggleArchiveChat(chatId: chat.id, archived: !chat.isArchived) }
            }
            Button("改名", systemImage: "pencil") { title = chat.title; renaming = chat }
            Button("删除", systemImage: "trash", role: .destructive) { Task { await store.deleteChat(id: chat.id) } }
        }
    }

    private func runPruneEmpty() async {
        guard !pruning else { return }
        pruning = true
        defer { pruning = false }
        let removed = await store.pruneEmptyChats()
        Haptics.selection()
        let msg = removed > 0 ? "已清理 \(removed) 个空旁聊" : "没有可清理的空旁聊"
        menuNotice = msg
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(3.0))
            if menuNotice == msg { menuNotice = nil }
        }
    }

    private func searchMessages() async {
        let words = trimmedQuery
        guard !words.isEmpty else { hits = []; searched = ""; searching = false; return }
        searching = true
        do { try await Task.sleep(for: .milliseconds(300)) } catch { searching = false; return }
        let found = try? await store.searchMessages(words)
        guard !Task.isCancelled else { return }
        hits = found ?? []
        searched = words
        searching = false
    }

    private func hitRow(_ hit: JSONValue) -> some View {
        let snippet = hit["snippet"].nonemptyString ?? hit["text"].stringValue
        let matches = extractMatches(hit["matches"])
        let timestamp = hit["created_at_ms"].doubleValue ?? hit["at_ms"].doubleValue ?? 0
        let chatTitle = hit["chat_title"].nonemptyString ?? hit["title"].nonemptyString ?? "旁聊"
        return VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text((hit["role"].stringValue == "user" ? "我" : store.companionName) + " · " + chatTitle)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Text(when(timestamp)).font(.caption2).foregroundStyle(.tertiary)
            }
            Text(marked(snippet, matches: matches)).font(.system(size: 15)).lineLimit(3)
        }
        .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private func extractMatches(_ raw: JSONValue) -> [[Int]]? {
        let pairs: [[Int]] = raw.array.compactMap { entry in
            let pair = entry.array
            guard pair.count >= 2, let start = pair[0].intValue, let end = pair[1].intValue, end > start else { return nil }
            return [start, end]
        }
        return pairs.isEmpty ? nil : pairs
    }

    /// Highlights every matched span from backend `matches` (or case-insensitive query fallback).
    private func marked(_ text: String, matches: [[Int]]?) -> AttributedString {
        SearchHighlight.styled(text, matches: matches, fallbackQuery: searched)
    }

    private func when(_ ms: Double) -> String {
        let date = Date(timeIntervalSince1970: ms / 1000)
        return Calendar.current.isDateInToday(date) ? date.formatted(date: .omitted, time: .shortened) : date.formatted(.dateTime.month().day())
    }

    private func openHit(_ hit: JSONValue) {
        let chat = hit["chat_id"].stringValue
        guard !chat.isEmpty else { return }
        store.revealTurn = hit["turn_id"].nonemptyString
        selectedTab = .chat; Task { await store.changeChat(chat) }; onClose(); Haptics.selection()
    }

    private func move(_ tab: CompanionTab, by direction: Int) {
        guard let from = order.firstIndex(of: tab), order.indices.contains(from + direction) else { return }
        var updated = order; updated.swapAt(from, from + direction); onReorder(updated); Haptics.selection()
    }
}
