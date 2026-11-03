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
    /// 聊天记录: messages that say what is typed in 搜索 (any chat), and the words they were found for.
    @State private var hits: [JSONValue] = []
    @State private var searched = ""

    private var chats: [SideChat] {
        let search = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return search.isEmpty ? store.sideChats : store.sideChats.filter { ($0.title + " " + $0.preview).localizedCaseInsensitiveContains(search) }
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                Text(store.companionName).font(.system(size: 32, weight: .bold)).frame(maxWidth: .infinity, alignment: .leading)
                Button(action: onClose) { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭菜单")
            }.padding(.horizontal, 24).padding(.top, 32).padding(.bottom, 20)
                .gesture(DragGesture(minimumDistance: 25).onEnded { if $0.translation.width < -65 { onClose() } })
            List {
                Section {
                    ForEach(order) { tab in
                        Button {
                            selectedTab = tab; Haptics.selection()
                            if tab == .chat && !store.mainChatID.isEmpty { Task { await store.changeChat(store.mainChatID) } }
                            onClose()
                        } label: {
                            Label { Text(tab.rawValue).font(.system(size: 18)) } icon: {
                                Image(systemName: tab.icon).font(.system(size: 24)).frame(width: 32)
                            }.foregroundStyle(.primary)  // plain like the web's menu, not the list's tint
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
                Section {
                    if chats.isEmpty {
                        Text(query.isEmpty ? "还没有旁聊" : "没有找到").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 8)
                    }
                    ForEach(chats) { chat in
                        Button {
                            selectedTab = .chat; Task { await store.changeChat(chat.id) }; onClose(); Haptics.selection()
                        } label: {
                            Text(chat.title.isEmpty ? "新的旁聊" : chat.title).font(.system(size: 18))
                                .lineLimit(2).frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                        }.buttonStyle(.plain)
                            .swipeActions(edge: .trailing) {
                                Button("删除", role: .destructive) { Task { await store.deleteChat(id: chat.id) } }
                                Button("改名") { title = chat.title; renaming = chat }.tint(.orange)
                            }
                            .contextMenu {
                                Button("改名", systemImage: "pencil") { title = chat.title; renaming = chat }
                                Button("删除", systemImage: "trash", role: .destructive) { Task { await store.deleteChat(id: chat.id) } }
                            }
                    }.listRowBackground(Color.clear).listRowSeparator(.hidden)
                } header: { Text("旁聊").textCase(nil).padding(.top, 12) }
                if !searched.isEmpty {
                    Section {
                        if hits.isEmpty {
                            Text("聊天记录里没有「\(searched)」").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 8)
                        }
                        ForEach(Array(hits.enumerated()), id: \.offset) { _, hit in
                            Button { openHit(hit) } label: { hitRow(hit) }.buttonStyle(.plain)
                        }
                    } header: { Text("聊天记录").textCase(nil).padding(.top, 12) }
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
            }
            .listStyle(.plain).scrollContentBackground(.hidden)
            .environment(\.defaultMinListRowHeight, 44)
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            HStack(spacing: 8) {
                Button(action: onSettings) { Image(systemName: "gearshape").font(.system(size: 23)).frame(width: 48, height: 48).modifier(FloatingPanel(radius: 24)) }
                    .accessibilityLabel("设置")
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary).accessibilityHidden(true)
                    TextField("搜索", text: $query).accessibilityLabel("搜索旁聊和聊天记录").submitLabel(.search)
                }.padding(.horizontal, 12).frame(height: 48).modifier(FloatingPanel(radius: 24))
                Button {
                    selectedTab = .chat; Task { await store.createChat() }; onClose(); Haptics.selection()
                } label: { Image(systemName: "square.and.pencil").font(.system(size: 22)).frame(width: 48, height: 48).modifier(FloatingPanel(radius: 24)) }
                    .accessibilityLabel("新开旁聊")
            }.padding(12)
        }
        .task(id: query) { await searchMessages() }
        .alert("旁聊名称", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("名称", text: $title)
            Button("取消", role: .cancel) { renaming = nil }
            Button("保存") {
                if let chat = renaming { Task { await store.renameChat(id: chat.id, title: title) } }; renaming = nil
            }
        }
    }
    private func searchMessages() async {
        let words = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { hits = []; searched = ""; return }
        do { try await Task.sleep(for: .milliseconds(350)) } catch { return }  // typing: once it pauses
        let found = try? await store.searchMessages(words)
        guard !Task.isCancelled else { return }
        hits = found ?? []
        searched = words
    }

    private func hitRow(_ hit: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text((hit["role"].stringValue == "user" ? "我" : store.companionName) + " · " + hit["chat_title"].stringValue)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Text(when(hit["at_ms"].doubleValue ?? 0)).font(.caption2).foregroundStyle(.tertiary)
            }
            Text(marked(hit["text"].stringValue)).font(.system(size: 15)).lineLimit(3)
        }
        .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// The words searched for, in the accent colour and bold.
    private func marked(_ text: String) -> AttributedString {
        var result = AttributedString(text)
        if let range = result.range(of: searched, options: .caseInsensitive) {
            result[range].foregroundColor = CompanionPalette.primary
            result[range].inlinePresentationIntent = .stronglyEmphasized
        }
        return result
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
