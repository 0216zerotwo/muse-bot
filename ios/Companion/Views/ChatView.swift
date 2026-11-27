import SwiftUI
import MarkdownUI
import PhotosUI
import Photos
import UniformTypeIdentifiers
import UIKit
import CryptoKit

private struct ThreadEntry: Identifiable {
    let id: String
    let at: Double
    let message: ChatMessage?
    let approval: JSONValue?
    /// Under the last reply of a finished turn that did something: its steps (做了 6 步 ›).
    var task: ActivityTask?
    /// What it saved about the user in that turn (已记住：…).
    var remembered: [String] = []
}

@MainActor
struct ChatView: View {
    var topInset: CGFloat = 174
    /// Explicit chat ID when presenting an isolated helper/group conversation (`HelperChatScreen`).
    var chatID: String? = nil
    /// A helper's or a group's chat: shown chat-app style (faces beside messages).
    var messenger: MessengerStyle? = nil
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var text = ""
    @State private var quote: String?
    @State private var drafts: [DraftUpload] = []
    @State private var photoPicker = false
    @State private var videoPicker = false
    @State private var camera = false
    @State private var attach = false
    @State private var attachChoice: AttachSheet.Choice?
    @State private var filePicker = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var encoding = false
    @State private var composerError: String?
    @State private var media: MediaDestination?
    @State private var followingBottom = true
    @State private var positioned = false
    /// Prevents async media/file card expansion during initial open from clearing `followingBottom`.
    @State private var settlingBottom = false
    @State private var settleGeneration = UUID()
    /// Shown once it sits at the newest message (opening a chat never shows it scrolling down).
    @State private var revealed = false
    @State private var paging = false
    @State private var draftKey = ""
    @State private var unreadTracker = ChatUnreadTracker()
    /// The steps under 正在… are shown (kept from one task to the next).
    @State private var stepsOpen = false
    @State private var openTask: ActivityTask?
    /// 选择文字: a message's text in a sheet where any part of it can be selected.
    @State private var selecting: SelectableText?
    /// 编辑并重发: user message being edited in a lightweight sheet.
    @State private var editingMessage: ChatMessage?
    /// 来源详情: citation drawer / knowledge snippet details sheet.
    @State private var citationSheet: CitationSheetContext?
    /// Contextual follow-up prompt chips shown after the latest completed reply.
    @State private var followupSuggestions: [String] = []
    /// Search-jump target message ID briefly highlighted with a soft pulse.
    @State private var highlightedMessageID: String?
    @State private var highlightTask: Task<Void, Never>?
    /// Learned skills refreshed whenever `/` autocomplete opens.
    @State private var learnedSkills: [JSONValue] = []
    @State private var skillsLoadedOnce = false
    @State private var loadingSkills = false
    /// Empty-chat model onboarding sheet when no chat model is configured yet.
    @State private var modelSetup = false
    /// The words in the box came from voice input; the answer to them is read out (设置 can turn it off).
    @State private var dictated = false
    @State private var readReply = false
    @AppStorage(ReadAloud.afterVoiceKey) private var readAfterVoice = true
    @StateObject private var voice = VoiceRecorder()
    @FocusState private var focused: Bool

    private var isIsolatedHelperChat: Bool { chatID != nil }
    private var activeChatID: String { chatID ?? store.currentChatID }
    private var chatMessages: [ChatMessage] { isIsolatedHelperChat ? store.helperMessages : store.messages }
    private var chatApprovals: [JSONValue] { isIsolatedHelperChat ? store.helperApprovals : store.approvals }
    private var chatRunning: Bool { isIsolatedHelperChat ? store.helperRunning : store.running }
    private var chatHasOlder: Bool { isIsolatedHelperChat ? store.helperHasOlder : store.hasOlder }
    private var chatLoadingHistory: Bool { isIsolatedHelperChat ? store.helperLoadingHistory : store.loadingHistory }
    private var chatGoal: GoalState? { isIsolatedHelperChat ? store.helperGoal : store.currentGoal }
    private var chatActivityLabel: String {
        if !isIsolatedHelperChat { return store.activityLabel }
        if chatApprovals.contains(where: { $0["status"].stringValue == "pending" }) { return "等你确认" }
        return Presentation.currentActivity(messages: chatMessages, streaming: chatRunning)?.label ?? (store.connected ? "正在做事" : "等待连接")
    }

    private var entries: [ThreadEntry] {
        var items: [ThreadEntry] = []
        var placed: Set<String> = []
        let currentMessages = chatMessages
        let currentApprovals = chatApprovals
        for message in currentMessages {
            for event in message.toolEvents {
                if let approval = currentApprovals.first(where: { $0["call_id"].nonemptyString == event["call_id"].nonemptyString && $0["call_id"].nonemptyString != nil }),
                   placed.insert(approval["id"].stringValue).inserted {
                    items.append(ThreadEntry(id: "approval-" + approval["id"].stringValue, at: approval["created_at_ms"].doubleValue ?? message.createdAt, message: nil, approval: approval))
                }
            }
            if Presentation.isVisible(message) {
                items.append(ThreadEntry(id: "message-" + message.id, at: message.createdAt, message: message, approval: nil))
            }
        }
        for approval in currentApprovals where !placed.contains(approval["id"].stringValue) {
            let recent = approval["decided_at_ms"].doubleValue.map { Date().timeIntervalSince1970 * 1_000 - $0 < 15 * 60 * 1_000 } ?? false
            if approval["status"].stringValue == "pending" || recent {
                items.append(ThreadEntry(id: "approval-" + approval["id"].stringValue, at: approval["created_at_ms"].doubleValue ?? 0, message: nil, approval: approval))
            }
        }
        guard messenger == nil else { return items }
        var tasks: [String: ActivityTask] = [:]
        for task in ActivityLog.tasks(messages: currentMessages, running: chatRunning, jobs: [], detailed: false) where task.status != .running {
            tasks[task.id] = task
        }
        var lastReply: [String: Int] = [:]
        for (index, item) in items.enumerated() {
            if let message = item.message, message.role != "user", let turn = message.turnId { lastReply[turn] = index }
        }
        let notes = MemoryNotes.byTurn(currentMessages)
        for (turn, index) in lastReply {
            items[index].task = tasks[turn]
            items[index].remembered = notes[turn] ?? []
        }
        return items
    }
    /// The one reply whose cards still answer (worked out once per update, not once per row).
    private var answerableID: String? { chatRunning ? nil : Presentation.answerableReplyID(messages: chatMessages) }

    /// Latest completed assistant reply turn eligible for contextual follow-up chips.
    private var latestFollowupTurnID: String? {
        guard !chatRunning, store.connected, !activeChatID.isEmpty else { return nil }
        let current = chatMessages
        let lastUserIndex = current.lastIndex { $0.role == "user" } ?? -1
        for idx in stride(from: current.count - 1, through: max(0, lastUserIndex), by: -1) {
            let msg = current[idx]
            if msg.role != "user",
               msg.kind != "trace",
               !msg.isStreaming,
               msg.source?["kind"].stringValue != "notice",
               let tid = msg.turnId, !tid.isEmpty,
               !msg.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return tid
            }
        }
        return nil
    }

    private var followupTriggerKey: String {
        "\(activeChatID)|\(latestFollowupTurnID ?? "")|\(chatRunning)|\(store.connected)"
    }

    private var completedReplyIDs: [String] {
        chatMessages.compactMap { msg in
            msg.role != "user" && !msg.isStreaming && Presentation.isVisibleReply(msg) ? msg.id : nil
        }
    }

    var body: some View {
        applySheetsAndLifecycle(
            applyMediaPickers(
                ScrollViewReader { proxy in
                    applyScrollObservers(threadScrollView(proxy: proxy), proxy: proxy)
                }
            )
        )
    }

    @ViewBuilder
    private func threadScrollView(proxy: ScrollViewProxy) -> some View {
        let answerable = answerableID
        let items = entries  // built once per redraw (each use of `entries` rebuilds it)
        let currentMessages = chatMessages
        // The newest reply: copy, read out and 重新生成 under it, as ChatGPT and Claude show them.
        let lastReplyID = items.last(where: { entry in
            entry.message.map { $0.role != "user" && $0.source?["kind"].stringValue != "notice" } ?? false
        })?.id

        ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                if chatHasOlder {
                    Button { loadOlder(proxy) } label: {
                        HStack { Spacer(); if chatLoadingHistory || paging { ProgressView() }; Text("载入更早的消息"); Spacer() }.frame(minHeight: 44)
                    }.font(.footnote).disabled(chatLoadingHistory || paging)
                        .onAppear { if positioned && !followingBottom { loadOlder(proxy) } }
                }
                if items.isEmpty && !chatLoadingHistory { emptyState }
                ForEach(Array(items.enumerated()), id: \.element.id) { index, entry in
                    threadRow(
                        entry: entry,
                        index: index,
                        items: items,
                        answerable: answerable,
                        lastReplyID: lastReplyID,
                        currentMessages: currentMessages
                    )
                }
                if Presentation.awaitingReply(messages: currentMessages, streaming: chatRunning) {
                    let current = ActivityLog.current(currentMessages)
                    // A document it is writing shows as it will look, before it is sent.
                    if let draft = FileKinds.draft(currentMessages) { DraftCard(draft: draft).equatable() }
                    WorkingCard(label: chatActivityLabel, steps: current.steps, since: current.since, open: $stepsOpen)
                }
                Color.clear.frame(height: 1).id("bottom")
                    .onAppear { followingBottom = true }
                    .onDisappear { if positioned && !settlingBottom { followingBottom = false } }
            }
            .frame(maxWidth: 700, alignment: .leading).padding(.horizontal, 16).padding(.bottom, 24).padding(.top, topInset)
            .opacity(revealed || chatMessages.isEmpty ? 1 : 0)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .overlay { if chatLoadingHistory && chatMessages.isEmpty { ProgressView("正在载入聊天…") } }
        .overlay(alignment: .bottomTrailing) {
            scrollToBottomOverlay(proxy: proxy)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 6) {
                if messenger == nil, let goal = chatGoal, goal.active {
                    let targetChat = activeChatID
                    GoalBannerView(goal: goal) {
                        Haptics.selection()
                        Task { await store.stopGoal(chatID: targetChat) }
                    }
                    .padding(.horizontal, 12)
                }
                composer
            }
        }
    }

    @ViewBuilder
    private func scrollToBottomOverlay(proxy: ScrollViewProxy) -> some View {
        if !followingBottom && !chatMessages.isEmpty {
            let count = unreadTracker.unreadCount
            Button {
                unreadTracker.reset(for: chatMessages)
                followingBottom = true
                scrollBottom(proxy)
                Haptics.selection()
            } label: {
                if count > 0 {
                    HStack(spacing: 6) {
                        Image(systemName: "arrow.down")
                            .font(.system(size: 13, weight: .bold))
                        Text("\(count) 条新回复")
                            .font(.system(size: 13.5, weight: .semibold))
                    }
                    .foregroundStyle(CompanionPalette.primaryText)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 40)
                    .background(CompanionPalette.primary, in: Capsule())
                    .shadow(color: .black.opacity(0.14), radius: 8, y: 3)
                } else {
                    Image(systemName: "arrow.down")
                        .frame(width: 44, height: 44)
                        .background(.regularMaterial, in: Circle())
                }
            }
            .buttonStyle(.plain)
            .accessibilityLabel(count > 0 ? "查看 \(count) 条新回复" : "滚动到最新消息")
            .padding(16)
        }
    }

    @ViewBuilder
    private func threadRow(
        entry: ThreadEntry,
        index: Int,
        items: [ThreadEntry],
        answerable: String?,
        lastReplyID: String?,
        currentMessages: [ChatMessage]
    ) -> some View {
        if showDate(index, in: items) {
            Text(dateLabel(entry.at)).font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity).padding(.vertical, 8)
        }
        if let message = entry.message {
            let targetChat = activeChatID
            MessageRow(store: store, message: message,
                       answerable: answerable == message.id,
                       isRunning: chatRunning,
                       highlighted: highlightedMessageID == message.id,
                       speaker: message.role == "user" ? nil : messenger?.speaker(message),
                       messenger: messenger != nil,
                       chatID: targetChat,
                       chatKey: "\(store.serverAddress).\(targetChat)",
                       onQuote: { quote = quoteText(message); focused = true; Haptics.selection() },
                       onMedia: { media = MediaDestination(item: $0, baseURL: store.baseURL) },
                       onSelect: { selecting = SelectableText(text: $0) },
                       onEdit: { editingMessage = message; Haptics.selection() },
                       onFork: {
                           Haptics.selection()
                           Task { await store.forkChat(chatId: targetChat, turnId: message.turnId) }
                       },
                       onOpenCitations: { sources, focusID in
                           citationSheet = CitationSheetContext(sources: sources, focusedID: focusID)
                       })
                .equatable()
            if !entry.remembered.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(entry.remembered, id: \.self) { note in
                        Label("已记住：" + note, systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                }.padding(.horizontal, 6).accessibilityElement(children: .combine)
            }
            let latest = entry.id == lastReplyID && !chatRunning
            if entry.task != nil || latest {
                HStack(spacing: 6) {
                    if let task = entry.task {
                        TaskChip(task: task) {
                            // The full task (what was asked, the answer) only when it is opened.
                            openTask = ActivityLog.tasks(messages: currentMessages, running: chatRunning, jobs: []).first { $0.id == task.id } ?? task
                        }
                    }
                    if latest {
                        let again: (() -> Void)? = canRegenerate(message) ? { Task { await store.regenerate(message, chatID: targetChat) } } : nil
                        ReplyActions(store: store, message: message, regenerate: again)
                    }
                }.padding(.top, -6)
            }
            if latest && !followupSuggestions.isEmpty {
                FollowupChipsRow(suggestions: followupSuggestions) { prompt in
                    followupSuggestions = []
                    _ = store.send(text: prompt, chatID: targetChat)
                    Haptics.send()
                }
            }
        } else if let approval = entry.approval {
            let targetChat = activeChatID
            NativeApprovalView(approval: approval) { decision, scope in
                await store.approve(id: approval["id"].stringValue, decision: decision, scope: scope, chatID: targetChat)
            }.frame(maxWidth: 520, alignment: .leading).id(entry.id)
        }
    }

    private func applyScrollObservers<Content: View>(_ content: Content, proxy: ScrollViewProxy) -> some View {
        content
            .onChange(of: chatMessages.last?.id) { _, _ in followIfNeeded(proxy) }
            // A reply growing stays pinned to the bottom, without an animation for every chunk.
            .onChange(of: chatMessages.last?.content) { _, _ in followIfNeeded(proxy, animated: false) }
            .onChange(of: chatApprovals) { _, _ in followIfNeeded(proxy) }
            .onChange(of: chatGoal) { _, _ in followIfNeeded(proxy) }
            .onChange(of: chatLoadingHistory) { _, loading in
                if !loading && !positioned { followIfNeeded(proxy) }
                if !loading { revealSearchHit(proxy) }
            }
            .onChange(of: activeChatID) { _, _ in
                voice.cancel()
                highlightTask?.cancel()
                highlightedMessageID = nil
                followupSuggestions = []
                drafts.forEach { $0.removeStagedBlob() }
                positioned = false; settlingBottom = false; settleGeneration = UUID()
                revealed = false; followingBottom = true; drafts = []
                unreadTracker.reset(for: chatMessages)
                restoreDraft()
                followIfNeeded(proxy); revealSoon()
            }
            .onChange(of: focused) { _, value in if value && followingBottom { scrollBottom(proxy) } }
            .onChange(of: text) { oldValue, newValue in
                if !draftKey.isEmpty { ChatDraftPersistence.saveText(newValue, server: store.serverAddress, chatID: activeChatID) }
                if composerError != nil && !newValue.isEmpty { composerError = nil }
                if newValue.isEmpty { dictated = false }
                loadAutocompleteContextIfNeeded(from: oldValue, to: newValue)
            }
            .onChange(of: quote) { _, newQuote in
                if !draftKey.isEmpty { ChatDraftPersistence.saveQuote(newQuote, server: store.serverAddress, chatID: activeChatID) }
            }
            .onChange(of: completedReplyIDs) { _, _ in
                unreadTracker.ingest(
                    messages: chatMessages,
                    followingBottom: followingBottom,
                    positioned: positioned && !settlingBottom,
                    paging: paging
                )
            }
            .onChange(of: followingBottom) { _, atBottom in
                if atBottom { unreadTracker.reset(for: chatMessages) }
            }
            .onChange(of: positioned) { _, isPositioned in
                if isPositioned { unreadTracker.reset(for: chatMessages) }
            }
            .onAppear {
                restoreDraft()
                acceptDraft()
                unreadTracker.reset(for: chatMessages)
                loadAutocompleteContextIfNeeded(from: "", to: text)
                followIfNeeded(proxy)
                revealSoon()
            }
            .onChange(of: store.suggestedDraft) { _, _ in acceptDraft() }
            .onChange(of: store.suggestedQuote) { _, _ in acceptDraft() }
            .onChange(of: revealed) { _, _ in revealSearchHit(proxy) }
            .onChange(of: store.revealTurn) { _, _ in revealSearchHit(proxy) }
            .task(id: followupTriggerKey) { await loadFollowups() }
    }

    private func applyMediaPickers<Content: View>(_ content: Content) -> some View {
        content
            .sheet(isPresented: $attach, onDismiss: openChosenPicker) {
                AttachSheet(onChoice: { attachChoice = $0; attach = false },
                            onAsset: { asset in attach = false; Task { await importAsset(asset) } })
                    .presentationDetents([.height(330)]).presentationCornerRadius(36)
                    .presentationBackground(.regularMaterial).presentationDragIndicator(.visible)
                    .tint(CompanionPalette.primary)
            }
            .sheet(isPresented: $modelSetup, onDismiss: { Task { await store.refreshModelStatus() } }) {
                NavigationStack { ModelsSettingsPage() }
                    .environment(\.settingsClose, { modelSetup = false })
                    .presentationDetents([.large])
                    .presentationDragIndicator(.visible)
                    .tint(CompanionPalette.primary)
            }
            .fullScreenCover(isPresented: $camera) {
                CameraPicker { shot in Task { await importShot(shot) } }.ignoresSafeArea()
            }
            .photosPicker(isPresented: $photoPicker, selection: $photos, maxSelectionCount: max(1, Self.maxAttachments - drafts.count), matching: .any(of: [.images, .videos]))
            .photosPicker(isPresented: $videoPicker, selection: $photos, maxSelectionCount: max(1, Self.maxAttachments - drafts.count), matching: .videos)
            .onChange(of: photos) { _, selection in Task { await importPhotos(selection) } }
            .fileImporter(isPresented: $filePicker, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
                Task { await importFiles(result) }
            }
    }

    private func applySheetsAndLifecycle<Content: View>(_ content: Content) -> some View {
        content
            .onDisappear {
                voice.cancel()
                highlightTask?.cancel()
            }
            .onChange(of: scenePhase) { _, phase in if phase == .background { voice.cancel() } }
            .inAppLinks()
            .fullScreenCover(item: $media) { MediaViewer(destination: $0) }
            .sheet(item: $openTask) { TaskSheet(task: $0) }
            .sheet(item: $selecting) { item in
                SelectTextSheet(text: item.text) { quoted in
                    quote = quoted
                    focused = true
                    Haptics.selection()
                }
            }
            .sheet(item: $editingMessage) { target in
                let targetChat = activeChatID
                EditMessageSheet(message: target) { updatedText in
                    guard let turnID = target.turnId else { return false }
                    followupSuggestions = []
                    let ok = await store.editAndResend(chatId: targetChat, turnId: turnID, content: updatedText)
                    if ok { Haptics.send() }
                    return ok
                }
            }
            .sheet(item: $citationSheet) { context in
                CitationSourcesSheet(context: context)
            }
            .onChange(of: chatRunning) { was, now in
                // The answer to a question asked by voice, read out when it is done.
                guard was, !now, readReply else { return }
                readReply = false
                let currentMessages = chatMessages
                let lastAsked = currentMessages.lastIndex { $0.role == "user" } ?? -1
                if let reply = currentMessages.indices.last(where: { $0 > lastAsked && currentMessages[$0].role != "user" && Presentation.isVisibleReply(currentMessages[$0]) }) {
                    ReadAloud.shared.speak(currentMessages[reply].id, text: currentMessages[reply].content, store: store)
                }
            }
    }

    /// 重新生成 is for the newest answer to a plain question (Hermes asks it again; it can't send
    /// pictures or files again).
    private func canRegenerate(_ message: ChatMessage) -> Bool {
        guard store.connected, message.source == nil, let turn = message.turnId, !turn.isEmpty,
              let asked = chatMessages.last(where: { $0.role == "user" }) else { return false }
        return asked.turnId == turn && asked.media.isEmpty
    }

    @ViewBuilder private var emptyState: some View {
        if let messenger {
            // A helper's or a group's empty chat: its face, large, a line to start with, things to ask.
            VStack(spacing: 6) {
                switch messenger.bigFace {
                case .speaker(let face): SpeakerFace(speaker: face, size: 72, baseURL: store.baseURL)
                case .group(let members): GroupFace(members: members, size: 72, baseURL: store.baseURL)
                }
                Text(messenger.title).font(.system(size: 19, weight: .semibold)).padding(.top, 8)
                Text(messenger.hint).font(.system(size: 14)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 300)
                if !messenger.starters.isEmpty {
                    VStack(spacing: 8) {
                        ForEach(messenger.starters, id: \.self) { starter in
                            Button { _ = store.send(text: starter, chatID: activeChatID); Haptics.send() } label: {
                                Text(starter).font(.system(size: 14.5)).frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.horizontal, 16).padding(.vertical, 10).modifier(FloatingPanel(radius: 16))
                            }.buttonStyle(.plain)
                        }
                    }.frame(maxWidth: 300).padding(.top, 14).accessibilityLabel("可以这样问")
                }
            }.frame(maxWidth: .infinity).padding(.horizontal, 32).padding(.top, 80).padding(.bottom, 64)
        } else {
            VStack(spacing: 16) {
                AvatarView(value: store.companionState["avatar"].string, library: store.avatars, size: 168, baseURL: store.baseURL)
                Text(greeting).font(.title2.weight(.semibold))
                if store.connected && store.modelsChecked && !store.hasConfiguredChatModel {
                    VStack(spacing: 10) {
                        Label("先配一个对话模型", systemImage: "sparkles")
                            .font(.headline)
                        Text("添加模型密钥或连接账号后，\(store.companionName)就能开始回复你。")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Button {
                            modelSetup = true
                        } label: {
                            Text("去设置对话模型")
                                .font(.system(size: 15, weight: .semibold))
                                .padding(.horizontal, 22)
                                .frame(minHeight: 44)
                                .background(CompanionPalette.primary, in: Capsule())
                                .foregroundStyle(CompanionPalette.primaryText)
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 2)
                    }
                    .padding(18)
                    .frame(maxWidth: 320)
                    .modifier(FloatingPanel(radius: 22))
                    .padding(.top, 4)
                } else {
                    VStack(spacing: 8) {
                        ForEach(ComposerAutocomplete.homeStarters, id: \.self) { starter in
                            Button {
                                _ = store.send(text: starter, chatID: activeChatID)
                                Haptics.send()
                            } label: {
                                Text(starter)
                                    .font(.system(size: 14.5))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.horizontal, 16)
                                    .padding(.vertical, 10)
                                    .modifier(FloatingPanel(radius: 16))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .frame(maxWidth: 300)
                    .padding(.top, 6)
                    .accessibilityLabel("可以这样问")
                }
            }.frame(maxWidth: .infinity).padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 64)
        }
    }
    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        return hour < 5 ? "这么晚还没睡？" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好"
    }

    private var slashMenuItems: [ComposerAutocomplete.SlashItem] {
        guard messenger == nil, let query = ComposerAutocomplete.slashQuery(in: text) else { return [] }
        return ComposerAutocomplete.matchingSlashItems(query: query, learnedSkills: learnedSkills)
    }

    private var mentionMenuHelpers: [JSONValue] {
        guard messenger == nil || messenger?.isGroup == true,
              let query = ComposerAutocomplete.mentionQuery(in: text) else { return [] }
        let pool: [JSONValue]
        if let messenger, messenger.isGroup, !messenger.groupMembers.isEmpty {
            pool = messenger.groupMembers
        } else {
            pool = store.helpers
        }
        return ComposerAutocomplete.matchingHelpers(query: query, helpers: pool)
    }

    private func loadAutocompleteContextIfNeeded(from oldText: String, to currentText: String) {
        let wasSlashOpen = ComposerAutocomplete.slashQuery(in: oldText) != nil
        let isSlashOpen = ComposerAutocomplete.slashQuery(in: currentText) != nil
        if messenger == nil, isSlashOpen, (!wasSlashOpen || !skillsLoadedOnce), !loadingSkills, store.connected {
            loadingSkills = true
            Task {
                defer { loadingSkills = false }
                do {
                    let payload = try await store.getResource("/api/webui/companion/skills")
                    learnedSkills = payload["learned"].array
                    skillsLoadedOnce = true
                } catch {
                    // Keep prior skills without locking out retry on next `/` open or keystroke.
                }
            }
        }
        let wasMentionOpen = ComposerAutocomplete.mentionQuery(in: oldText) != nil
        let isMentionOpen = ComposerAutocomplete.mentionQuery(in: currentText) != nil
        if (messenger == nil || messenger?.isGroup == true),
           isMentionOpen, (!wasMentionOpen || store.helpers.isEmpty), store.connected {
            Task { await store.refreshLibrary() }
        }
    }

    @ViewBuilder private var autocompleteMenu: some View {
        let slashes = slashMenuItems
        let mentions = mentionMenuHelpers
        if !slashes.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(slashes) { item in
                        Button {
                            text = item.text
                            Haptics.selection()
                            Task { @MainActor in focused = true }
                        } label: {
                            HStack(spacing: 10) {
                                Image(systemName: item.isSkill ? "graduationcap" : "command")
                                    .font(.system(size: 13, weight: .semibold))
                                    .foregroundStyle(CompanionPalette.primary)
                                    .frame(width: 24)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.label).font(.system(size: 14.5, weight: .semibold)).foregroundStyle(.primary)
                                    Text(item.detail).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(6)
            }
            .frame(maxHeight: 220)
            .modifier(FloatingPanel(radius: 20))
            .accessibilityLabel("快捷指令建议")
        } else if !mentions.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(mentions, id: \.selfID) { helper in
                        let name = helper["name"].stringValue
                        Button {
                            text = ComposerAutocomplete.replacingMention(in: text, with: name)
                            Haptics.selection()
                            Task { @MainActor in focused = true }
                        } label: {
                            HStack(spacing: 10) {
                                HelperAvatar(emoji: helper["emoji"].stringValue, avatar: helper["avatar"].stringValue, size: 28, baseURL: store.baseURL)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text("@" + name).font(.system(size: 14.5, weight: .semibold)).foregroundStyle(.primary)
                                    if let job = helper["title"].nonemptyString {
                                        Text(job).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(6)
            }
            .frame(maxHeight: 210)
            .modifier(FloatingPanel(radius: 20))
            .accessibilityLabel("选择帮手")
        }
    }

    private var composer: some View {
        VStack(spacing: 8) {
            autocompleteMenu
            if let quote {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "quote.opening").foregroundStyle(CompanionPalette.primary).accessibilityHidden(true)
                    Text(quote).font(.caption).foregroundStyle(.secondary).lineLimit(3).frame(maxWidth: .infinity, alignment: .leading)
                    Button { self.quote = nil } label: { Image(systemName: "xmark.circle.fill").frame(width: 44, height: 44) }.accessibilityLabel("取消引用")
                }.padding(.leading, 16).background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
            }
            if !drafts.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(drafts) { draft in
                            HStack(spacing: 6) {
                                Image(systemName: draft.mimeType.hasPrefix("image/") ? "photo" : draft.mimeType.hasPrefix("video/") ? "video" : "doc")
                                Text(draft.name).font(.caption).lineLimit(1)
                                Button {
                                    draft.removeStagedBlob()
                                    drafts.removeAll { $0.id == draft.id }
                                } label: { Image(systemName: "xmark.circle.fill").frame(width: 44, height: 44) }
                                    .accessibilityLabel("移除附件 \(draft.name)")
                            }.padding(.leading, 12).background(Color.primary.opacity(0.06), in: Capsule())
                        }
                    }
                }
            }
            if let composerError {
                Label(composerError, systemImage: "exclamationmark.circle.fill").font(.footnote)
                    .foregroundStyle(.red).padding(.horizontal, 12).padding(.vertical, 7)
                    .modifier(FloatingPanel(radius: 16))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let error = voice.error { Text(error).font(.footnote).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading) }
            if voice.recording {
                HStack {
                    Label("正在录音，点麦克风结束", systemImage: "waveform").foregroundStyle(.red).font(.caption)
                    Spacer()
                    Button("取消") { voice.cancel() }.frame(minHeight: 44)
                }
            }
            if !isIsolatedHelperChat && store.queuedCount > 0 {
                Text("\(store.queuedCount) 条消息等待发送").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(alignment: .bottom, spacing: 8) {
                Button { focused = false; attach = true; Haptics.selection() } label: {
                    Image(systemName: "plus").font(.title3).frame(width: 44, height: 44).contentShape(Rectangle())
                }.buttonStyle(.plain)
                    .accessibilityLabel("添加附件").disabled(encoding || voice.recording || voice.requestingPermission)
                TextField("发消息", text: $text, axis: .vertical)
                    .lineLimit(1...6).focused($focused).padding(.horizontal, 4).padding(.vertical, 12)
                    .accessibilityLabel("消息内容")
                if encoding || store.uploading || voice.transcribing || voice.requestingPermission { ProgressView().frame(width: 44, height: 44).accessibilityLabel(voice.transcribing ? "正在识别语音" : "正在准备") }
                else if voice.recording {
                    Button { Task { await toggleVoice() } } label: {
                        Image(systemName: "stop.circle.fill").font(.system(size: 22)).foregroundStyle(.red).frame(width: 44, height: 44)
                    }.accessibilityLabel("结束录音并识别")
                }
                else if chatRunning && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && drafts.isEmpty {
                    let targetChat = activeChatID
                    Button { Task { await store.stop(chatID: targetChat) }; Haptics.selection() } label: {
                        Image(systemName: "stop.fill").font(.body.weight(.bold)).frame(width: 44, height: 44)
                            .background(Color.primary.opacity(0.09), in: Circle())
                    }.accessibilityLabel("停止回复")
                } else if canSend {
                    Button(action: send) {
                        Image(systemName: "arrow.up").font(.title3.weight(.bold)).foregroundStyle(CompanionPalette.primaryText)
                            .frame(width: 44, height: 44).background(CompanionPalette.primary, in: Circle())
                    }.disabled(!canSend).accessibilityLabel(store.connected ? "发送消息" : "加入待发送队列")
                } else if store.sttAvailable {
                    Button { Task { await toggleVoice() } } label: {
                        Image(systemName: voice.recording ? "stop.circle.fill" : "mic").font(.system(size: 22)).frame(width: 44, height: 44)
                            .foregroundStyle(voice.recording ? Color.red : Color.primary)
                    }.disabled(!store.connected).accessibilityLabel(voice.recording ? "结束录音并识别" : "语音输入")
                } else {
                    Button {
                        composerError = "服务器还没有开启语音识别，先打字跟我说吧"
                        Haptics.selection()
                    } label: {
                        Image(systemName: "mic.slash").font(.system(size: 20)).frame(width: 44, height: 44)
                            .foregroundStyle(.tertiary)
                    }
                    .disabled(!store.connected)
                    .accessibilityLabel("语音输入未开启")
                }
            }.padding(.horizontal, 4).padding(.vertical, 4).modifier(FloatingPanel(radius: 28))
        }.padding(.horizontal, 12).padding(.vertical, 4)
    }

    private func toggleVoice() async {
        focused = false; Haptics.selection()
        if voice.recording {
            let chat = activeChatID
            let server = store.serverAddress
            if let transcript = await voice.finish(using: store), !transcript.isEmpty, chat == activeChatID, server == store.serverAddress {
                text += (text.isEmpty ? "" : " ") + transcript; focused = true
                dictated = true
            }
        } else {
            ReadAloud.shared.stop()  // not reading while it listens
            await voice.start()
        }
    }
    private func acceptDraft() {
        guard !isIsolatedHelperChat else { return }
        guard store.suggestedDraft != nil || store.suggestedQuote != nil else { return }
        if let next = store.suggestedDraft {
            text = next
            store.suggestedDraft = nil
        }
        if let nextQuote = store.suggestedQuote {
            quote = nextQuote.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : nextQuote
            store.suggestedQuote = nil
        }
        focused = true
    }

    private var canSend: Bool { !encoding && !voice.recording && !voice.transcribing && !voice.requestingPermission && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !drafts.isEmpty) }
    private func send() {
        guard canSend else { return }
        let content = UserQuote.format(content: text, quotedContext: quote)
        let targetChat = activeChatID
        guard store.send(text: content, attachments: drafts, chatID: targetChat) else { return }
        readReply = dictated && readAfterVoice
        dictated = false
        ReadAloud.shared.stop()
        ChatDraftPersistence.clear(server: store.serverAddress, chatID: targetChat)
        text = ""; drafts = []; quote = nil; composerError = nil; followingBottom = true
        unreadTracker.reset(for: chatMessages)
        Haptics.send()
    }
    private func quoteText(_ message: ChatMessage) -> String {
        let content = UserQuote.parse(ReplyContent.textWithoutCards(message.content)).content
        return content.isEmpty ? (message.media.first?.name ?? "附件") : content
    }
    private func restoreDraft() {
        let key = ChatDraftPersistence.textKey(server: store.serverAddress, chatID: activeChatID)
        guard key != draftKey else { return }
        draftKey = key
        let loaded = ChatDraftPersistence.load(server: store.serverAddress, chatID: activeChatID)
        text = loaded.text
        quote = loaded.quote
    }
    private func followIfNeeded(_ proxy: ScrollViewProxy, animated: Bool = true) {
        guard !paging, followingBottom || !positioned else { return }
        let first = !positioned
        positioned = !chatLoadingHistory && !entries.isEmpty
        guard first else { scrollBottom(proxy, animated: animated); return }
        // Opening: straight to the newest while unseen, then shown — no run down from the top.
        // Async file/picture/chart cards grow over the first second; keep `settlingBottom` active
        // so card expansion never flips `followingBottom` to false before the final settle scroll.
        guard positioned else {
            Task { @MainActor in
                await Task.yield()
                proxy.scrollTo("bottom", anchor: .bottom)
            }
            return
        }
        let token = UUID()
        settleGeneration = token
        settlingBottom = true
        Task { @MainActor in
            await Task.yield()
            guard settleGeneration == token else { return }
            proxy.scrollTo("bottom", anchor: .bottom)
            try? await Task.sleep(for: .milliseconds(120))
            guard settleGeneration == token else { return }
            proxy.scrollTo("bottom", anchor: .bottom)
            revealed = true
            try? await Task.sleep(for: .milliseconds(450))
            guard settleGeneration == token else { return }
            followingBottom = true
            proxy.scrollTo("bottom", anchor: .bottom)
            try? await Task.sleep(for: .milliseconds(550))
            guard settleGeneration == token else { return }
            followingBottom = true
            proxy.scrollTo("bottom", anchor: .bottom)
            try? await Task.sleep(for: .milliseconds(550))
            guard settleGeneration == token else { return }
            followingBottom = true
            proxy.scrollTo("bottom", anchor: .bottom)
            try? await Task.sleep(for: .milliseconds(150))
            if settleGeneration == token { settlingBottom = false }
        }
    }
    /// 查找聊天记录 → a message: expands `visibleCounts` (and older pages if needed), scrolls cleanly
    /// to the hit turn, and applies a gentle highlight pulse on the target bubble.
    private func revealSearchHit(_ proxy: ScrollViewProxy) {
        guard !isIsolatedHelperChat, let turn = store.revealTurn, revealed, !chatLoadingHistory else { return }
        let targetChat = activeChatID
        guard !targetChat.isEmpty else { return }
        store.revealTurn = nil
        settlingBottom = false
        settleGeneration = UUID()
        followingBottom = false
        Task { @MainActor in
            guard let target = await store.ensureTurnVisible(turn, in: targetChat),
                  targetChat == activeChatID else { return }
            await Task.yield()
            try? await Task.sleep(for: .milliseconds(260))
            guard targetChat == activeChatID else { return }
            if reduceMotion {
                proxy.scrollTo("message-" + target.id, anchor: .center)
            } else {
                withAnimation(.easeOut(duration: 0.28)) {
                    proxy.scrollTo("message-" + target.id, anchor: .center)
                }
            }
            pulseHighlight(messageID: target.id)
        }
    }

    private func pulseHighlight(messageID: String) {
        highlightTask?.cancel()
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.25)) {
            highlightedMessageID = messageID
        }
        highlightTask = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(2_200))
            guard !Task.isCancelled else { return }
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.45)) {
                if highlightedMessageID == messageID {
                    highlightedMessageID = nil
                }
            }
        }
    }

    private func loadFollowups() async {
        guard let turnID = latestFollowupTurnID else {
            followupSuggestions = []
            return
        }
        let targetChat = activeChatID
        if let cached = store.cachedFollowups(chatId: targetChat, turnId: turnID) {
            followupSuggestions = cached
            return
        }
        followupSuggestions = []
        let items = await store.fetchFollowups(chatId: targetChat, turnId: turnID)
        guard !Task.isCancelled, targetChat == activeChatID, latestFollowupTurnID == turnID else { return }
        followupSuggestions = items
    }
    /// Whatever happens, the chat shows within a moment (a chat with nothing to settle on included).
    private func revealSoon() {
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(1_200))
            revealed = true
        }
    }
    private func scrollBottom(_ proxy: ScrollViewProxy, animated: Bool = true) {
        Task { @MainActor in
            await Task.yield()
            if animated && !reduceMotion {
                withAnimation(.easeOut(duration: 0.18)) { proxy.scrollTo("bottom", anchor: .bottom) }
            } else {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }
    private func loadOlder(_ proxy: ScrollViewProxy) {
        guard !paging, !chatLoadingHistory, chatHasOlder else { return }
        paging = true
        let anchor = entries.first?.id
        let targetChat = activeChatID
        Task { @MainActor in
            await store.loadOlder(chatID: targetChat); await Task.yield()
            if let anchor { proxy.scrollTo(anchor, anchor: .top) }
            paging = false
        }
    }
    private static let maxAttachments = 6
    private func showDate(_ index: Int, in items: [ThreadEntry]) -> Bool { index == 0 || items[index].at - items[index - 1].at > 3_600_000 }
    private static let todayTimeFormatter: DateFormatter = {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "HH:mm"; return formatter
    }()
    private static let yesterdayTimeFormatter: DateFormatter = {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "昨天 HH:mm"; return formatter
    }()
    private static let dayTimeFormatter: DateFormatter = {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "M月d日 HH:mm"; return formatter
    }()
    private func dateLabel(_ milliseconds: Double) -> String {
        let date = Date(timeIntervalSince1970: milliseconds / 1_000)
        if Calendar.current.isDateInToday(date) { return Self.todayTimeFormatter.string(from: date) }
        if Calendar.current.isDateInYesterday(date) { return Self.yesterdayTimeFormatter.string(from: date) }
        return Self.dayTimeFormatter.string(from: date)
    }

    private func importPhotos(_ selection: [PhotosPickerItem]) async {
        guard !selection.isEmpty else { return }
        let chat = activeChatID
        encoding = true; composerError = nil
        defer { encoding = false; photos = [] }
        for item in selection {
            guard drafts.count < Self.maxAttachments else {
                composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
                break
            }
            do {
                guard let data = try await item.loadTransferable(type: Data.self) else { throw URLError(.cannotDecodeContentData) }
                guard chat == activeChatID else { return }
                let type = item.supportedContentTypes.first(where: { $0.conforms(to: .image) || $0.conforms(to: .movie) }) ?? .data
                let mime = type.preferredMIMEType ?? "application/octet-stream"
                guard data.count <= 20 * 1024 * 1024 || mime.hasPrefix("image/") else {
                    composerError = "单个附件不能超过 20 MB。"
                    continue
                }
                let name = "附件-\(drafts.count + 1).\(type.preferredFilenameExtension ?? "bin")"
                let staged = try await AttachmentEncoder.stagedDraft(data: data, name: name, mimeType: mime)
                guard chat == activeChatID else { staged.removeStagedBlob(); return }
                drafts.append(staged)
            } catch { composerError = "这个附件暂时无法读取，请尝试从「选择文件」添加。" }
        }
    }
    /// After the ＋ panel has gone: the picker it asked for (a sheet can't open over another).
    private func openChosenPicker() {
        let choice = attachChoice; attachChoice = nil
        guard choice == nil || drafts.count < Self.maxAttachments else {
            composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
            return
        }
        switch choice {
        case .camera:
            if CameraPicker.isAvailable { camera = true } else { composerError = "这台设备没有可用的相机。" }
        case .allPhotos: photoPicker = true
        case .videos: videoPicker = true
        case .files: filePicker = true
        case nil: break
        }
    }
    /// A photo or video tapped in the ＋ panel's strip.
    private func importAsset(_ asset: PHAsset) async {
        guard drafts.count < Self.maxAttachments else {
            composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
            return
        }
        let chat = activeChatID
        encoding = true; composerError = nil
        defer { encoding = false }
        guard let file = await RecentPhotos.file(for: asset), chat == activeChatID else {
            composerError = "这张没能读取（可能还在 iCloud 里），请从「查看全部」选择。"; return
        }
        guard file.data.count <= 20 * 1024 * 1024 || file.mimeType.hasPrefix("image/") else {
            composerError = "视频太大了（超过 20 MB），请选短一点的。"; return
        }
        do {
            let name = file.name.replacingOccurrences(of: ".", with: "-\(drafts.count + 1).", options: [], range: file.name.range(of: "."))
            let staged = try await AttachmentEncoder.stagedDraft(data: file.data, name: name, mimeType: file.mimeType)
            guard chat == activeChatID else { staged.removeStagedBlob(); return }
            drafts.append(staged)
        } catch { composerError = "这个附件暂时无法读取，请尝试从「添加文件」添加。" }
    }
    private func importShot(_ shot: CameraPicker.Shot) async {
        if case .video(let cleanupURL) = shot, drafts.count >= Self.maxAttachments {
            try? FileManager.default.removeItem(at: cleanupURL)
            composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
            return
        }
        guard drafts.count < Self.maxAttachments else {
            composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
            return
        }
        let chat = activeChatID
        encoding = true; composerError = nil
        defer { encoding = false }
        do {
            switch shot {
            case .photo(let image):
                guard let data = image.jpegData(compressionQuality: 0.9) else { throw URLError(.cannotDecodeContentData) }
                let staged = try await AttachmentEncoder.stagedDraft(data: data, name: "照片-\(drafts.count + 1).jpg", mimeType: "image/jpeg")
                guard chat == activeChatID else { staged.removeStagedBlob(); return }
                drafts.append(staged)
            case .video(let url):
                defer { try? FileManager.default.removeItem(at: url) }
                let data = try Data(contentsOf: url)
                guard data.count <= 20 * 1024 * 1024 else { composerError = "视频太大了（超过 20 MB），录短一点再试。"; return }
                let staged = try await AttachmentEncoder.stagedDraft(data: data, name: "视频-\(drafts.count + 1).mov", mimeType: "video/quicktime")
                guard chat == activeChatID else { staged.removeStagedBlob(); return }
                drafts.append(staged)
            }
        } catch { composerError = "刚拍的内容没能读取，请再拍一次。" }
    }
    private func importFiles(_ result: Result<[URL], Error>) async {
        let chat = activeChatID
        encoding = true; composerError = nil
        defer { encoding = false }
        do {
            for url in try result.get() {
                guard drafts.count < Self.maxAttachments else {
                    composerError = "一次最多添加 \(Self.maxAttachments) 个附件。"
                    break
                }
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let bytes = try Data(contentsOf: url)
                let type = (try? url.resourceValues(forKeys: [.contentTypeKey]))?.contentType ?? UTType(filenameExtension: url.pathExtension) ?? .data
                let mime = type.preferredMIMEType ?? "application/octet-stream"
                guard bytes.count <= 20 * 1024 * 1024 || mime.hasPrefix("image/") else {
                    composerError = "单个附件不能超过 20 MB。"
                    continue
                }
                let staged = try await AttachmentEncoder.stagedDraft(data: bytes, name: url.lastPathComponent, mimeType: mime)
                guard chat == activeChatID else { staged.removeStagedBlob(); return }
                drafts.append(staged)
            }
        } catch { composerError = "没有读到文件，请重新选择。" }
    }
}

@MainActor
/// One message. It redraws only when its own message (or answerability) changes — not on every
/// store update: a streamed word used to redraw every visible bubble's Markdown.
private struct MessageRow: View, Equatable {
    let store: CompanionStore  // for actions only; deliberately not observed
    let message: ChatMessage
    let answerable: Bool
    let isRunning: Bool
    let highlighted: Bool
    /// Chat-app style: who said this reply (their face beside it, and in a group their name).
    let speaker: Speaker?
    let messenger: Bool
    let chatID: String
    let chatKey: String
    var onQuote: () -> Void
    var onMedia: (MediaAttachment) -> Void
    var onSelect: (String) -> Void
    var onEdit: () -> Void
    var onFork: () -> Void
    var onOpenCitations: ([CitationSource], Int?) -> Void

    nonisolated static func == (a: MessageRow, b: MessageRow) -> Bool {
        a.message == b.message && a.answerable == b.answerable && a.isRunning == b.isRunning
            && a.highlighted == b.highlighted && a.speaker == b.speaker
            && a.messenger == b.messenger && a.chatID == b.chatID && a.chatKey == b.chatKey
    }

    private var isUser: Bool { message.role == "user" }
    private var isNotice: Bool { message.source?["kind"].stringValue == "notice" }
    private var canEdit: Bool {
        isUser && !isRunning && store.connected && !(message.turnId ?? "").isEmpty && message.deliveryStatus != "sending"
    }
    private var canFork: Bool {
        !messenger && store.connected && !(message.turnId ?? "").isEmpty && message.deliveryStatus != "sending"
    }
    private var media: [MediaAttachment] {
        var result = message.media
        if !message.isStreaming {
            for item in ReplyContent.linkedMedia(message.content) where !result.contains(where: { $0.url == item.url }) { result.append(item) }
        }
        return result
    }

    var body: some View {
        Group {
            if isNotice {
                Text(message.content).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity).padding(.horizontal, 20)
            } else {
                HStack(alignment: .top, spacing: 8) {
                    if isUser { Spacer(minLength: 28) }
                    if !isUser, let speaker { SpeakerFace(speaker: speaker, size: 36, baseURL: store.baseURL) }
                    VStack(alignment: isUser ? .trailing : .leading, spacing: 4) {
                        if let name = speaker?.name, !name.isEmpty {
                            Text(name).font(.system(size: 12.5)).foregroundStyle(.secondary).padding(.horizontal, 4)
                        } else if speaker == nil { sourceLabel }
                        if isUser { userContent } else { replyContent }
                        if !isUser, let citations = message.sources, !citations.isEmpty {
                            CitationPillsRow(sources: citations) { focusID in
                                onOpenCitations(citations, focusID)
                            }
                        }
                        ForEach(media) { attachment in
                            AttachmentView(item: attachment, baseURL: store.baseURL) { onMedia(attachment) }
                        }
                        if ["queued", "pending", "sending"].contains(message.deliveryStatus ?? "") {
                            Label("等待发送", systemImage: "clock").font(.caption2).foregroundStyle(.secondary)
                        } else if message.deliveryStatus == "failed" {
                            Label("发送状态待确认，长按可检查后重试", systemImage: "exclamationmark.circle").font(.caption2).foregroundStyle(.red)
                        }
                    }.frame(maxWidth: 520, alignment: isUser ? .trailing : .leading)
                    if isUser && messenger { MeFace(size: 36) }
                    if !isUser { Spacer(minLength: 20) }
                }
                .padding(.vertical, highlighted ? 4 : 0)
                .padding(.horizontal, highlighted ? 6 : 0)
                .background(
                    RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .fill(highlighted ? CompanionPalette.primary.opacity(0.13) : .clear)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .strokeBorder(highlighted ? CompanionPalette.primary.opacity(0.55) : .clear, lineWidth: 1.5)
                )
                .contextMenu {
                    Button("引用", systemImage: "quote.bubble", action: onQuote)
                    Button("复制", systemImage: "doc.on.doc") { UIPasteboard.general.string = message.content; Haptics.selection() }
                    Button("选择文字", systemImage: "selection.pin.in.out") {
                        onSelect(isUser ? UserQuote.parse(message.content).content : ReplyContent.textWithoutCards(message.content))
                    }
                    if canEdit {
                        Button("编辑并重发", systemImage: "pencil.line", action: onEdit)
                    }
                    if canFork {
                        Button("从这里分叉旁聊", systemImage: "arrow.triangle.branch", action: onFork)
                    }
                    if !isUser {
                        Button("朗读", systemImage: "speaker.wave.2") { ReadAloud.shared.toggle(message.id, text: message.content, store: store) }
                    }
                    if message.deliveryStatus == "failed" {
                        Button("重新发送", systemImage: "arrow.clockwise") { Task { await store.retryMessage(message, chatID: chatID) } }
                    }
                    Button("删除", systemImage: "trash", role: .destructive) { Task { await store.deleteMessage(message, chatID: chatID) } }
                }
                .simultaneousGesture(LongPressGesture(minimumDuration: 0.4).onEnded { _ in Haptics.hold() })
                .accessibilityAction(named: Text("引用"), onQuote)
                .accessibilityAction(named: Text("复制")) { UIPasteboard.general.string = message.content }
            }
        }.id("message-" + message.id)
    }

    @ViewBuilder private var sourceLabel: some View {
        if message.source?["kind"].stringValue == "helper" {
            // A helper speaking for itself (in a chat that handed it work): its drawn face and name, as the web.
            let label = message.source?["label"].stringValue ?? "帮手"
            let words = label.split(separator: " ", maxSplits: 1).map(String.init)
            HStack(spacing: 6) {
                EmojiIcon(emoji: words.first ?? "🤖", size: 20)
                Text(words.count > 1 ? words[1] : label).font(.system(size: 13, weight: .semibold))
            }.padding(.horizontal, 4)
        } else if Presentation.isProactive(message) {
            Label(message.source?["label"].nonemptyString ?? "主动消息", systemImage: "bell.badge")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var userContent: some View {
        let parsed = UserQuote.parse(message.content)
        return VStack(alignment: .trailing, spacing: 6) {
            if let quote = parsed.quote {
                HStack(spacing: 8) {
                    Rectangle().fill(CompanionPalette.border).frame(width: 2)
                    Text(quote).font(.caption).foregroundStyle(.secondary).lineLimit(4)
                }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 4)
            }
            if !parsed.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text(parsed.content).padding(.horizontal, 16).padding(.vertical, 11)
                    .foregroundStyle(.white)
                    .background(LinearGradient(colors: [CompanionPalette.userBubble.opacity(0.88), CompanionPalette.userBubble], startPoint: .top, endPoint: .bottom),
                                in: Bubble.user(messenger: messenger))
            }
        }
    }

    private var replyContent: some View {
        VStack(alignment: .leading, spacing: 6) {
            let blocks = ReplyContent.blocks(message.content, streaming: message.isStreaming)
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                switch block {
                case .bubble(let text):
                    // Consecutive bubbles read as one message: tight corners where they meet, a tail on the last.
                    let joined = index > 0 && { if case .bubble = blocks[index - 1] { return true }; return false }()
                    Markdown(text, baseURL: store.baseURL, imageBaseURL: store.baseURL)
                        .markdownTheme(.basic)
                        .markdownBlockStyle(\.codeBlock) { ChatCodeBlock(store: store, configuration: $0) }
                        .markdownImageProvider(ChatMarkdownImageProvider(open: onMedia))
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .modifier(BubbleFill(shape: Bubble.reply(messenger: messenger, joined: joined)))
                case .card(let card):
                    NativeCardView(card: card, stateKey: cardStateKey(card, index: index), baseURL: store.baseURL,
                                   send: answerable ? { store.send(text: $0, chatID: chatID) } : nil)
                case .pending(let kind):
                    HStack { ProgressView(); Text(kind == "ui" ? "正在准备互动内容…" : "正在整理…").font(.subheadline).foregroundStyle(.secondary) }
                        .padding(16).background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 21))
                }
            }
        }
    }
    private func cardStateKey(_ card: ReplyCard, index: Int) -> String {
        let encoder = JSONEncoder(); encoder.outputFormatting = .sortedKeys
        let data = (try? encoder.encode(card.data)) ?? Data()
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return "\(chatKey).\(message.turnId ?? message.id).\(index).\(digest)"
    }
}

/// MarkdownUI 2.4.1's provider protocol predates SwiftUI's main-actor isolation.
/// Its view is always constructed by Markdown's SwiftUI body on the main actor.
@MainActor
private struct ChatMarkdownImageProvider: @preconcurrency ImageProvider {
    var open: (MediaAttachment) -> Void

    @ViewBuilder func makeImage(url: URL?) -> some View {
        if let url, ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            Button { open(MediaAttachment(url: url.absoluteString, name: url.lastPathComponent, kind: "image")) } label: {
                RemotePicture(path: url.absoluteString, baseURL: nil, maxPixel: 1_100)
                    .frame(maxHeight: 300).clipShape(RoundedRectangle(cornerRadius: 16))
            }.buttonStyle(.plain).accessibilityLabel("打开图片，可保存或分享")
        } else {
            Image(systemName: "photo").foregroundStyle(.secondary).accessibilityLabel("图片暂时无法显示")
        }
    }
}

/// While it works: what it is doing, how long it has been at it, and (a tap) the steps so far, so a
/// task of a few minutes is not just a spinner.
@MainActor
private struct WorkingCard: View {
    let label: String
    let steps: [ActivityStep]
    /// When the turn began (ms).
    let since: Double?
    @Binding var open: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button { if !steps.isEmpty { withAnimation(.snappy(duration: 0.25)) { open.toggle() } } } label: {
                HStack(spacing: 10) {
                    ProgressView()
                    VStack(alignment: .leading, spacing: 2) {
                        Text(label + "…").font(.subheadline).foregroundStyle(.secondary)
                        if !steps.isEmpty {
                            Text("已做 \(steps.count) 步" + (open ? "" : "，点开看看")).font(.caption).foregroundStyle(.tertiary)
                        }
                    }
                    Spacer(minLength: 10)
                    if let since {
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            let ms = context.date.timeIntervalSince1970 * 1000 - since
                            Text(ms >= 5000 ? Elapsed.clock(ms) : "").font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                        }
                    }
                    if !steps.isEmpty {
                        Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(open ? 180 : 0))
                    }
                }.contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(label + (steps.isEmpty ? "" : "，已做 \(steps.count) 步"))
            .accessibilityHint(steps.isEmpty ? "" : (open ? "收起步骤" : "展开步骤"))
            if open {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(steps.suffix(15)) { step in StepRow(step: step, live: step.running) }
                }.padding(.top, 2).transition(.opacity)
            }
        }
        .padding(14)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 20))
        .frame(maxWidth: 520, alignment: .leading)
    }
}

/// Under a reply that took steps: how many and how long; a tap shows them (as on the companion's page).
private struct TaskChip: View {
    let task: ActivityTask
    var open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 4) {
                Image(systemName: task.status == .failed ? "exclamationmark.circle" : "checklist").font(.system(size: 11))
                Text(words).font(.system(size: 12))
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Color.primary.opacity(0.05), in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("查看步骤：" + words)
    }

    private var words: String {
        let took = task.end.map { $0 > task.at ? " · 用时 " + Elapsed.words($0 - task.at) : "" } ?? ""
        return "做了 \(task.steps.count) 步" + took
    }
}

/// Under the newest reply: copy it, read it out, ask again (as ChatGPT and Claude show them).
@MainActor
private struct ReplyActions: View {
    let store: CompanionStore
    let message: ChatMessage
    var regenerate: (() -> Void)?
    @ObservedObject private var reader = ReadAloud.shared
    @State private var copied = false

    var body: some View {
        HStack(spacing: 0) {
            action(copied ? "checkmark" : "doc.on.doc", copied ? "已复制" : "复制") {
                UIPasteboard.general.string = ReplyContent.textWithoutCards(message.content)
                Haptics.selection()
                copied = true
                Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
            }
            let reading = reader.speakingID == message.id
            let loadingAudio = reader.loadingID == message.id
            if loadingAudio {
                Button {
                    reader.stop()
                } label: {
                    ProgressView().controlSize(.mini).frame(width: 34, height: 30)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("取消生成语音")
            } else {
                action(reading ? "stop.circle" : "speaker.wave.2", reading ? "停止朗读" : "朗读") {
                    reader.toggle(message.id, text: message.content, store: store)
                }
            }
            if let regenerate {
                action("arrow.clockwise", "重新生成") { Haptics.selection(); regenerate() }
            }
        }
        .foregroundStyle(.secondary)
    }

    private func action(_ icon: String, _ label: String, perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            Image(systemName: icon).font(.system(size: 13, weight: .medium)).frame(width: 34, height: 30).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

struct SelectableText: Identifiable {
    let id = UUID()
    let text: String
}

/// 选择文字: the message in full, where any part of it can be selected, copied, or quoted for follow-up.
@MainActor
private struct SelectTextSheet: View {
    let text: String
    var onQuote: ((String) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                Text(text).font(.body).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(20)
            }
            .navigationTitle("选择文字").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
                ToolbarItemGroup(placement: .primaryAction) {
                    if let onQuote {
                        Button("引用追问") {
                            let clip = (UIPasteboard.general.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                            let chosen = (!clip.isEmpty && text.contains(clip)) ? clip : text
                            onQuote(chosen)
                            dismiss()
                        }
                    }
                    Button("全部复制") { UIPasteboard.general.string = text; Haptics.selection(); dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

/// A code block in a reply: its language, an interactive 预览 button for HTML/SVG/Markdown, a 复制 button,
/// and the code scrolling sideways instead of wrapping.
@MainActor
private struct ChatCodeBlock: View {
    let store: CompanionStore
    let configuration: CodeBlockConfiguration
    @State private var copied = false
    @State private var previewItem: ArtifactPreviewItem?

    private var previewLanguage: String? {
        ArtifactPreviewItem.previewableLanguage(configuration.language, content: configuration.content)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Text(configuration.language.flatMap { $0.isEmpty ? nil : $0 } ?? "代码").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if let lang = previewLanguage {
                    Button {
                        Haptics.selection()
                        let title = lang == "svg" ? "SVG 预览" : lang == "md" ? "Markdown 预览" : "HTML 预览"
                        previewItem = ArtifactPreviewItem(title: title, language: lang, code: configuration.content)
                    } label: {
                        Label("预览", systemImage: "eye").font(.caption.weight(.medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(CompanionPalette.primary)
                    .accessibilityLabel("预览 \(lang.uppercased()) 代码")
                }
                Button {
                    UIPasteboard.general.string = configuration.content
                    Haptics.selection()
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: {
                    Label(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc").font(.caption)
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .accessibilityLabel(copied ? "已复制代码" : "复制代码")
            }
            .padding(.horizontal, 12).padding(.vertical, 7)
            Divider()
            ScrollView(.horizontal, showsIndicators: false) {
                configuration.label
                    .markdownTextStyle { FontFamilyVariant(.monospaced); FontSize(.em(0.85)) }
                    .padding(12)
            }
        }
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 12))
        .markdownMargin(top: 4, bottom: 8)
        .sheet(item: $previewItem) { item in
            ArtifactPreviewSheet(item: item)
                .environmentObject(store)
        }
    }
}

/// Perplexity-style horizontal scrolling citation capsules under an assistant reply.
@MainActor
private struct CitationPillsRow: View {
    let sources: [CitationSource]
    var onOpenSheet: (Int?) -> Void
    @Environment(\.openURL) private var openURL

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(sources) { source in
                    Button {
                        Haptics.selection()
                        if source.isKnowledge {
                            onOpenSheet(source.id)
                        } else if let url = source.webURL {
                            openURL(url)
                        } else {
                            onOpenSheet(source.id)
                        }
                    } label: {
                        HStack(spacing: 5) {
                            Text("\(source.id)")
                                .font(.system(size: 10.5, weight: .bold, design: .rounded))
                                .foregroundStyle(CompanionPalette.primary)
                                .frame(minWidth: 16, minHeight: 16)
                                .background(CompanionPalette.primary.opacity(0.14), in: Circle())
                            Image(systemName: source.isKnowledge ? "books.vertical.fill" : "globe")
                                .font(.system(size: 10.5))
                                .foregroundStyle(.secondary)
                                .accessibilityHidden(true)
                            Text(source.displayDomain)
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                        }
                        .padding(.horizontal, 9)
                        .padding(.vertical, 6)
                        .background(CompanionPalette.surface, in: Capsule())
                        .overlay(Capsule().strokeBorder(CompanionPalette.border))
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        Button("查看来源详情", systemImage: "doc.text.magnifyingglass") { onOpenSheet(source.id) }
                        if let url = source.webURL {
                            Button("在应用内打开", systemImage: "safari") { openURL(url) }
                            Button("复制链接", systemImage: "link") { UIPasteboard.general.string = url.absoluteString; Haptics.selection() }
                        }
                        if !source.snippet.isEmpty {
                            Button("复制摘录", systemImage: "doc.on.doc") { UIPasteboard.general.string = source.snippet; Haptics.selection() }
                        }
                    }
                    .accessibilityLabel("来源 \(source.id)：\(source.title)")
                }

                Button {
                    Haptics.selection()
                    onOpenSheet(nil)
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "doc.text.magnifyingglass").font(.system(size: 10.5))
                        Text("来源详情").font(.system(size: 12, weight: .medium))
                    }
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Color.primary.opacity(0.05), in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("查看全部 \(sources.count) 个来源详情")
            }
            .padding(.vertical, 2)
        }
    }
}

private struct CitationSheetContext: Identifiable {
    let id = UUID()
    let sources: [CitationSource]
    let focusedID: Int?
}

@MainActor
private struct CitationSourcesSheet: View {
    let context: CitationSheetContext
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(context.sources) { source in
                        let focused = context.focusedID == source.id
                        VStack(alignment: .leading, spacing: 8) {
                            HStack(alignment: .top, spacing: 8) {
                                Text("\(source.id)")
                                    .font(.system(size: 12, weight: .bold, design: .rounded))
                                    .foregroundStyle(CompanionPalette.primaryText)
                                    .frame(width: 22, height: 22)
                                    .background(CompanionPalette.primary, in: Circle())
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(source.title.isEmpty ? source.displayDomain : source.title)
                                        .font(.system(size: 15, weight: .semibold))
                                        .fixedSize(horizontal: false, vertical: true)
                                    HStack(spacing: 6) {
                                        Label(source.isKnowledge ? "个人知识库" : source.displayDomain,
                                              systemImage: source.isKnowledge ? "books.vertical.fill" : "globe")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Spacer(minLength: 0)
                                if let url = source.webURL {
                                    Button {
                                        openURL(url)
                                    } label: {
                                        Image(systemName: "arrow.up.right.square")
                                            .font(.system(size: 16))
                                            .frame(width: 34, height: 34)
                                    }
                                    .buttonStyle(.plain)
                                    .foregroundStyle(CompanionPalette.primary)
                                    .accessibilityLabel("打开网页 \(source.title)")
                                }
                            }
                            if !source.snippet.isEmpty {
                                Text(source.snippet)
                                    .font(.system(size: 13.5))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                        .overlay(
                            RoundedRectangle(cornerRadius: 16)
                                .strokeBorder(focused ? CompanionPalette.primary : CompanionPalette.border, lineWidth: focused ? 1.5 : 1)
                        )
                    }
                }
                .padding(16)
            }
            .background(CompanionPalette.background)
            .navigationTitle("参考来源（\(context.sources.count)）")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
            }
        }
        .inAppLinks()
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
    }
}

/// Contextual follow-up suggestion pills displayed under the latest completed reply.
@MainActor
private struct FollowupChipsRow: View {
    let suggestions: [String]
    var onSelect: (String) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(suggestions, id: \.self) { prompt in
                    Button { onSelect(prompt) } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "sparkles")
                                .font(.system(size: 11))
                                .foregroundStyle(CompanionPalette.primary)
                                .accessibilityHidden(true)
                            Text(prompt)
                                .font(.system(size: 13.5))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(CompanionPalette.surface, in: Capsule())
                        .overlay(Capsule().strokeBorder(CompanionPalette.border))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("追问：\(prompt)")
                }
            }
            .padding(.top, 2)
        }
    }
}

/// Lightweight sheet to edit an existing user message and resubmit from that turn.
@MainActor
private struct EditMessageSheet: View {
    let message: ChatMessage
    var onSubmit: (String) async -> Bool
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @State private var quoteHeader: String?
    @State private var busy = false

    private var cleanDraft: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                Text("修改后将从这条消息重新生成后续回复。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                if let quoteHeader, !quoteHeader.isEmpty {
                    HStack(spacing: 8) {
                        Rectangle().fill(CompanionPalette.primary).frame(width: 2)
                        Text(quoteHeader).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                    }
                    .padding(.horizontal, 16)
                }
                TextEditor(text: $draft)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(12)
                    .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(CompanionPalette.border))
                    .padding(.horizontal, 16)
                    .disabled(busy)
                Spacer(minLength: 0)
            }
            .padding(.bottom, 16)
            .background(CompanionPalette.background)
            .navigationTitle("编辑并重发")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(busy)
                }
                ToolbarItem(placement: .primaryAction) {
                    Button(busy ? "发送中…" : "重发") {
                        Task { await submit() }
                    }
                    .fontWeight(.semibold)
                    .disabled(busy || cleanDraft.isEmpty)
                }
            }
        }
        .onAppear {
            let parsed = UserQuote.parse(message.content)
            quoteHeader = parsed.quote
            draft = parsed.content
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
    }

    private func submit() async {
        guard !busy, !cleanDraft.isEmpty else { return }
        busy = true
        let finalContent = UserQuote.format(content: cleanDraft, quotedContext: quoteHeader)
        let ok = await onSubmit(finalContent)
        busy = false
        if ok { dismiss() }
    }
}

/// Floating card while a `/goal` is in progress: objective, live summary or blocked notice, and stop action (`GoalBanner.tsx`).
@MainActor
private struct GoalBannerView: View {
    let goal: GoalState
    var onStop: () -> Void
    @State private var open = false

    private var title: String {
        let trimmed = goal.objective.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "进行中的目标" : trimmed
    }

    private var subtitle: String {
        if goal.isBlocked { return "卡住了，需要你看一下" }
        if let summary = goal.uiSummary, !summary.isEmpty { return summary }
        return "正在推进…"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: "target")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(goal.isBlocked ? Color.white : CompanionPalette.primaryText)
                    .frame(width: 32, height: 32)
                    .background(goal.isBlocked ? Color.red : CompanionPalette.primary, in: Circle())
                    .accessibilityHidden(true)

                Button {
                    withAnimation(.easeInOut(duration: 0.18)) { open.toggle() }
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 4) {
                            Text(title)
                                .font(.system(size: 14, weight: .semibold))
                                .lineLimit(1)
                            Image(systemName: "chevron.down")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .rotationEffect(.degrees(open ? 180 : 0))
                                .accessibilityHidden(true)
                        }
                        Text(subtitle)
                            .font(.system(size: 12))
                            .foregroundStyle(goal.isBlocked ? Color.red : Color.secondary)
                            .lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("进行中的目标：\(title)，\(subtitle)")
                .accessibilityHint(open ? "点按收起详情" : "点按展开详情")

                Button(action: onStop) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 12, weight: .bold))
                        .frame(width: 44, height: 44)
                        .background(Color.primary.opacity(0.08), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("停止这个目标")
            }

            if open {
                Divider()
                if !goal.objective.isEmpty {
                    Text(goal.objective)
                        .font(.system(size: 14))
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let summary = goal.uiSummary, !summary.isEmpty {
                    Text("当前：\(summary)")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let recap = goal.recap, !recap.isEmpty {
                    Text("回顾：\(recap)")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: 420)
        .modifier(FloatingPanel(radius: 20))
        .frame(maxWidth: .infinity)
    }
}
