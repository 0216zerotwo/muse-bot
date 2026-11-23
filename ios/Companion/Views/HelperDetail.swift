import SwiftUI
import UIKit
import PhotosUI
import MarkdownUI

// MARK: - A helper's conversation, chat-app style

/// A helper's or a group's chat, as the web's Messenger: a slim bar on top (‹ back, the name and what
/// it is doing, ··· for its profile), faces beside the messages, no tab bar while inside.
@MainActor
struct HelperChatScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    let chatID: String
    @ObservedObject var resource: TabResource
    @State private var profile = false

    private var helpers: [JSONValue] { resource.payload["helpers"].array }
    private var helper: JSONValue? { helpers.first { $0["chat_id"].stringValue == chatID } }
    private var group: JSONValue? { resource.payload["groups"].array.first { $0["chat_id"].stringValue == chatID } }
    private var members: [JSONValue] { group?["members"].array.compactMap { id in helpers.first { $0["id"] == id } } ?? [] }
    private var chatRunning: Bool { store.helperChatID == chatID && store.helperRunning }
    private var chatActivity: String {
        Presentation.currentActivity(messages: store.helperMessages, streaming: chatRunning)?.label ?? "正在做事"
    }

    var body: some View {
        VStack(spacing: 0) {
            bar
            ChatView(topInset: 8, chatID: chatID, messenger: messenger)
        }
        .background(CompanionPalette.background.ignoresSafeArea())
        .sheet(isPresented: $profile) {
            Group {
                if let group {
                    GroupSheet(group: TabRecord(id: group["id"].stringValue, value: group), resource: resource,
                               onSaved: { _ in profile = false }, onDeleted: leaveDeletedConversation)
                } else {
                    HelperProfileView(chatID: chatID, resource: resource, onClose: { profile = false }, onDeleted: leaveDeletedConversation)
                }
            }.tint(CompanionPalette.primary)
        }
        .onChange(of: resource.payload) { _, _ in
            if resource.hasLoaded && helper == nil && group == nil {
                leaveDeletedConversation()
            }
        }
        // A swipe in from the left edge leaves the conversation, as ‹ does.
        .simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { value in
            if value.startLocation.x < 28 && value.translation.width > 80 && abs(value.translation.height) < 80 { dismiss() }
        })
    }

    private func leaveDeletedConversation() {
        profile = false
        store.purgeLocalChat(chatID)
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(150))
            dismiss()
        }
    }

    private var title: String {
        if let group { return "\(group["name"].stringValue)（\(members.count + 1)）" }
        return helper?["name"].nonemptyString ?? "帮手"
    }
    private var subtitle: String {
        if chatRunning { return chatActivity + "…" }
        if group != nil { return members.map { $0["name"].stringValue }.joined(separator: "、") }
        if helper?["setup"].boolValue == true { return "正在设定" }
        return helper?["title"].nonemptyString ?? "帮手"
    }

    private var bar: some View {
        HStack(spacing: 0) {
            Button { dismiss() } label: { Image(systemName: "chevron.left").font(.system(size: 20, weight: .medium)).frame(width: 44, height: 44) }
                .accessibilityLabel("返回帮手列表")
            Button { profile = true } label: {
                VStack(spacing: 1) {
                    Text(title).font(.system(size: 16.5, weight: .semibold)).lineLimit(1)
                    Text(subtitle).font(.system(size: 12)).lineLimit(1)
                        .foregroundStyle(chatRunning ? CompanionPalette.primary : Color.secondary)
                }.frame(maxWidth: .infinity)
            }.buttonStyle(.plain).accessibilityLabel("\(title)的资料")
            Button { profile = true } label: { Image(systemName: "ellipsis").font(.system(size: 20)).frame(width: 44, height: 44) }
                .accessibilityLabel("资料与设置")
        }
        .padding(.horizontal, 4).frame(height: 56)
        .background(CompanionPalette.background.opacity(0.92).ignoresSafeArea(edges: .top))
        .overlay(alignment: .bottom) { Divider().overlay(CompanionPalette.border) }
        .foregroundStyle(.primary)
    }

    private var messenger: MessengerStyle {
        let host = Speaker(companionPicture: companionPicture, companion: true, name: store.companionName)
        if group != nil {
            let members = self.members
            return MessengerStyle(
                title: title,
                hint: "说一件事，大家一起做。@ 谁就交给谁，不 @ 就由主助理来分派。",
                starters: [],
                isGroup: true,
                groupMembers: members,
                bigFace: .group(members),
                speaker: { message in
                    // A helper speaking for itself; otherwise the companion, hosting the group.
                    guard message.source?["kind"].stringValue == "helper" else { return host }
                    let id = message.source?["id"].stringValue ?? ""
                    let label = message.source?["label"].stringValue ?? ""
                    if let member = members.first(where: { $0["id"].stringValue == id || label == "\($0["emoji"].stringValue) \($0["name"].stringValue)" }) {
                        return Speaker(emoji: member["emoji"].stringValue, avatar: member["avatar"].stringValue, name: member["name"].stringValue)
                    }
                    return Speaker(name: label.split(separator: " ", maxSplits: 1).dropFirst().first.map(String.init) ?? label)
                })
        }
        let face = Speaker(emoji: helper?["emoji"].stringValue ?? "🤖", avatar: helper?["avatar"].stringValue ?? "")
        let job = helper?["title"].stringValue ?? ""
        // A default one still doing its default job: a few things to try.
        let starters = helper?["is_default"].boolValue == true
            ? resource.payload["templates"].array.first { $0["name"] == helper?["preset"] }?["starters"].array.compactMap(\.nonemptyString) ?? []
            : []
        return MessengerStyle(title: helper?["name"].nonemptyString ?? "帮手",
                              hint: job.isEmpty ? "跟它说说你想让它负责什么。" : "\(job)。有事直接说。",
                              starters: starters, isGroup: false, groupMembers: [], bigFace: .speaker(face), speaker: { _ in face })
    }

    /// The companion's still picture, for its face as a group's host.
    private var companionPicture: String? {
        let value = store.companionState["avatar"].stringValue
        guard value.hasPrefix("image:") else { return nil }
        let id = String(value.dropFirst(6))
        return store.avatars.first { $0["id"].stringValue == id }?["image_url"].nonemptyString
    }
}

/// How ChatView shows a conversation chat-app style.
struct MessengerStyle {
    enum BigFace { case speaker(Speaker), group([JSONValue]) }
    var title: String
    var hint: String
    var starters: [String]
    var isGroup = false
    var groupMembers: [JSONValue] = []
    var bigFace: BigFace
    var speaker: (ChatMessage) -> Speaker
}

// MARK: - A helper's profile

/// A helper's profile, as the web's HelperDetail: what it does, its habits, what it did, its
/// routines and triggers, its notes; 编辑资料, 分享设定, 删除.
@MainActor
struct HelperProfileView: View {
    @EnvironmentObject private var store: CompanionStore
    let chatID: String
    @ObservedObject var resource: TabResource
    var onClose: () -> Void
    var onDeleted: (() -> Void)? = nil
    @State private var confirmDelete = false
    @State private var copied = false

    private var helper: JSONValue? { resource.payload["helpers"].array.first { $0["chat_id"].stringValue == chatID } }

    var body: some View {
        NavigationStack {
            ScrollView {
                if let helper {
                    VStack(alignment: .leading, spacing: 0) {
                        VStack(spacing: 6) {
                            HelperAvatar(emoji: helper["emoji"].stringValue, avatar: helper["avatar"].stringValue, size: 64, baseURL: store.baseURL)
                            Text(helper["name"].stringValue).font(.system(size: 21, weight: .semibold)).padding(.top, 2)
                            if let title = helper["title"].nonemptyString { Text(title).font(.system(size: 14)).foregroundStyle(.secondary) }
                        }.frame(maxWidth: .infinity)
                        HStack(spacing: 8) {
                            NavigationLink {
                                HelperEditorView(start: helper.object, helperID: helper["id"].stringValue, resource: resource,
                                                 canReset: helper["preset"].nonemptyString != nil && !helper["is_default"].boolValue, title: "编辑帮手",
                                                 onSave: { fields in
                                                     var payload = fields
                                                     payload["action"] = .string("update"); payload["id"] = helper["id"]
                                                     return await resource.change("companion.helpers.change", payload: payload, store: store)
                                                 }, onDone: {})
                            } label: {
                                Text("编辑资料").font(.system(size: 15)).frame(maxWidth: .infinity).frame(minHeight: 44)
                                    .background(Color.primary.opacity(0.07), in: Capsule())
                            }.buttonStyle(.plain)
                            SecondaryButton(title: copied ? "已复制" : "分享设定", icon: copied ? "checkmark" : "doc.on.doc") {
                                UIPasteboard.general.string = HelperShare.text(helper); copied = true; Haptics.selection()
                                Task { try? await Task.sleep(for: .seconds(2)); copied = false }
                            }
                        }.padding(.top, 16)
                        if let instructions = helper["instructions"].nonemptyString {
                            Text(instructions).font(.system(size: 14)).foregroundStyle(.secondary).padding(.top, 16)
                        }
                        Text("模型：\(helper["model"].nonemptyString ?? "跟主助理一样")").font(.system(size: 13)).foregroundStyle(.secondary).padding(.top, 12)

                        HelperHabits(helper: helper, resource: resource)

                        sectionTitle("最近做的事")
                        let work = helper["work"].array
                        if work.isEmpty { Text("还没做过事").font(.system(size: 14)).foregroundStyle(.secondary).padding(.vertical, 8) }
                        ForEach(Array(work.enumerated()), id: \.offset) { _, item in
                            WorkRow(work: item)
                            Divider().overlay(CompanionPalette.border)
                        }

                        HelperRoutines(helper: helper)
                        HelperTriggers(helper: helper)

                        let notes = helper["notes"].array.compactMap(\.nonemptyString)
                        if !notes.isEmpty {
                            sectionTitle("工作笔记")
                            ForEach(notes, id: \.self) { note in
                                HStack(alignment: .top, spacing: 8) {
                                    Text(note).font(.system(size: 14)).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 4)
                                    Button { Task { await dropNote(note, in: helper) } } label: { Image(systemName: "xmark").frame(width: 32, height: 32) }
                                        .foregroundStyle(.secondary).accessibilityLabel("删掉这条笔记")
                                }.padding(.vertical, 4)
                                Divider().overlay(CompanionPalette.border)
                            }
                        }

                        if confirmDelete {
                            HStack(spacing: 8) {
                                SecondaryButton(title: "取消") { confirmDelete = false }
                                PrimaryButton(title: "删除", danger: true) {
                                    Task {
                                        if await resource.change("companion.helpers.change", payload: ["action": .string("delete"), "id": helper["id"]], store: store) {
                                            store.purgeLocalChat(chatID)
                                            if let onDeleted { onDeleted() } else { onClose() }
                                        }
                                    }
                                }
                            }.padding(.top, 20)
                        } else {
                            Button("删除这个帮手") { confirmDelete = true }.font(.system(size: 14)).foregroundStyle(.red)
                                .frame(maxWidth: .infinity, minHeight: 40).padding(.top, 20)
                        }
                        if let error = resource.error { Text(error).font(.footnote).foregroundStyle(.red).padding(.top, 8) }
                    }.padding(20)
                } else {
                    Text(resource.hasLoaded ? "这个帮手已经不在了。" : "正在读取…").foregroundStyle(.secondary).padding(40)
                }
            }
            .background(CompanionPalette.surface)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) {
                Button { onClose() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭")
            } }
        }
        .presentationDragIndicator(.visible)
    }

    private func dropNote(_ note: String, in helper: JSONValue) async {
        let notes = helper["notes"].array.filter { $0.stringValue != note }
        await resource.change("companion.helpers.change", payload: ["action": .string("update"), "id": helper["id"], "notes": .array(notes)], store: store)
    }
}

func sectionTitle(_ text: String) -> some View {
    Text(text).font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary).padding(.top, 22).padding(.bottom, 4)
}

/// One thing it was asked, its answer folded until tapped.
private struct WorkRow: View {
    let work: JSONValue
    @State private var open = false
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.15)) { open.toggle() } } label: {
                HStack(alignment: .top, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text([work["from"].nonemptyString, work["at"].doubleValue.map(dayLabel), work["done"].boolValue ? nil : "在做"]
                            .compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 12.5)).foregroundStyle(.secondary)
                        Text(work["task"].stringValue).font(.system(size: 15)).lineLimit(open ? nil : 2).multilineTextAlignment(.leading)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down").font(.system(size: 13)).foregroundStyle(.secondary).rotationEffect(.degrees(open ? 180 : 0)).padding(.top, 4)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            if open, let answer = work["answer"].nonemptyString {
                Markdown(answer).markdownTextStyle { FontSize(14.5) }
                    .padding(.horizontal, 14).padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 16))
            }
        }.padding(.vertical, 12)
    }
}

/// 它摸清的你的习惯: what it does your way by default, learned from the chats or told here.
private struct HelperHabits: View {
    @EnvironmentObject private var store: CompanionStore
    let helper: JSONValue
    @ObservedObject var resource: TabResource
    @State private var text = ""
    private static let source = ["you": "你告诉它的", "chat": "聊天里你说过", "learned": "慢慢摸清的"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle("它摸清的你的习惯")
            let habits = helper["habits"].array
            if habits.isEmpty {
                Text("还没有。你在聊天里表达过的偏好（比如“别把时间排太紧”），它会慢慢记住，以后默认照着来。")
                    .font(.system(size: 13.5)).foregroundStyle(.secondary).padding(.vertical, 8)
            }
            ForEach(habits, id: \.selfID) { habit in
                HStack(alignment: .top, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(habit["text"].stringValue).font(.system(size: 14.5))
                        Text([Self.source[habit["by"].stringValue], habit["at"].doubleValue.map(dayLabel)].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.top, 2)
                    Button { Task { await change(["action": .string("habit_remove"), "habit": habit["id"]]) } } label: {
                        Image(systemName: "xmark").frame(width: 32, height: 32)
                    }.foregroundStyle(.secondary).disabled(resource.busy).accessibilityLabel("忘掉：\(habit["text"].stringValue)")
                }.padding(.vertical, 6)
                Divider().overlay(CompanionPalette.border)
            }
            HStack(spacing: 8) {
                TextField("告诉它一个习惯", text: $text).font(.system(size: 14.5)).submitLabel(.done)
                    .onSubmit { remember() }.modifier(HelperField())
                Button("记住") { remember() }
                    .font(.system(size: 14, weight: .semibold)).padding(.horizontal, 16).frame(minHeight: 40)
                    .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText)
                    .buttonStyle(.plain).opacity(text.trimmingCharacters(in: .whitespaces).isEmpty ? 0.4 : 1)
                    .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty || resource.busy)
            }.padding(.top, 8)
        }
    }

    private func remember() {
        let value = text.trimmingCharacters(in: .whitespaces)
        guard !value.isEmpty else { return }
        Task { if await change(["action": .string("habit_add"), "text": .string(String(value.prefix(120)))]) { text = "" } }
    }

    private func change(_ payload: [String: JSONValue]) async -> Bool {
        var payload = payload
        payload["id"] = helper["id"]
        return await resource.change("companion.helpers.change", payload: payload, store: store)
    }
}

/// 例行: the scheduled jobs it set up from its chat.
private struct HelperRoutines: View {
    @EnvironmentObject private var store: CompanionStore
    let helper: JSONValue
    @StateObject private var jobs = TabResource()
    private var mine: [JSONValue] { jobs.payload["jobs"].array.filter { $0["owner"]["id"] == helper["id"] } }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle("例行")
            if jobs.hasLoaded && mine.isEmpty {
                Text("没有。在它的聊天里说一声（比如“每晚九点提醒我记账”），它就会自己定好。").font(.system(size: 13.5)).foregroundStyle(.secondary).padding(.vertical, 8)
            }
            ForEach(mine, id: \.selfID) { job in
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(job["name"].stringValue).font(.system(size: 14.5, weight: .medium)).lineLimit(1)
                        Text(Schedules.describe(job) + (job["enabled"].boolValue ? job["state"]["next_run_at_ms"].doubleValue.map { " · 下次 \(dayLabel($0))" } ?? "" : ""))
                            .font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    Toggle(job["name"].stringValue, isOn: Binding(get: { job["enabled"].boolValue }, set: { on in act(on ? "automation.enable" : "automation.disable", job) }))
                        .labelsHidden().disabled(jobs.busy)
                    Button { act("automation.delete", job) } label: { Image(systemName: "trash").frame(width: 36, height: 36) }
                        .foregroundStyle(.secondary).accessibilityLabel("删除 \(job["name"].stringValue)").disabled(jobs.busy)
                }.padding(.vertical, 6)
                Divider().overlay(CompanionPalette.border)
            }
        }
        .task(id: helper["id"].stringValue) { await jobs.load("/api/webui/automations", store: store) }
    }

    private func act(_ action: String, _ job: JSONValue) {
        Task { await jobs.change(action, payload: ["id": job["id"]], store: store) }
    }
}

/// 自动触发: what it does when something happens, each with 暂停 / 删除.
private struct HelperTriggers: View {
    @EnvironmentObject private var store: CompanionStore
    let helper: JSONValue
    @StateObject private var triggers = TabResource()
    @State private var copied: String?
    private var mine: [JSONValue] { triggers.payload["triggers"].array.filter { $0["owner"] == helper["id"] } }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle("自动触发")
            if triggers.hasLoaded && mine.isEmpty {
                Text("还没有。在\(helper["name"].stringValue)的聊天里说「来了新邮件就帮我分一下类」「这个网页变了告诉我」，它就会自己设好。")
                    .font(.system(size: 13.5)).foregroundStyle(.secondary).padding(.vertical, 8)
            }
            if !mine.isEmpty {
                VStack(spacing: 0) {
                    ForEach(mine, id: \.selfID) { trigger in
                        let paused = trigger["paused"].boolValue
                        HStack(spacing: 10) {
                            EmojiIcon(emoji: Schedules.triggerEmoji[trigger["kind"].stringValue] ?? "🔗", size: 20)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(trigger["name"].stringValue).font(.system(size: 14.5, weight: .medium)).lineLimit(1)
                                Text((paused ? "已暂停" : Schedules.triggerWhen(trigger)) + " · " + trigger["instruction"].stringValue)
                                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            if let hook = trigger["hook_url"].nonemptyString {
                                Button(copied == trigger["id"].stringValue ? "已复制" : "复制地址") {
                                    UIPasteboard.general.string = hook; copied = trigger["id"].stringValue
                                }.font(.system(size: 12.5, weight: .medium)).foregroundStyle(CompanionPalette.primary)
                            }
                            Button(paused ? "继续" : "暂停") { change(trigger, paused ? "resume" : "pause") }
                                .font(.system(size: 12.5)).foregroundStyle(.secondary)
                            Button("删除") { change(trigger, "remove") }.font(.system(size: 12.5)).foregroundStyle(.red)
                        }
                        .buttonStyle(.plain).padding(.horizontal, 14).padding(.vertical, 10).opacity(paused ? 0.6 : 1)
                        Divider().overlay(CompanionPalette.border)
                    }
                }.background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18))
            }
        }
        .task(id: helper["id"].stringValue) { await triggers.load("/api/webui/companion/triggers", store: store) }
    }

    private func change(_ trigger: JSONValue, _ action: String) {
        Task { await triggers.change("companion.triggers.change", payload: ["id": trigger["id"], "action": .string(action)], store: store) }
    }
}

// MARK: - 编辑帮手

/// Name, face, what it is for, how it works and its model, as the web's HelperEditor.
@MainActor
struct HelperEditorView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    let start: [String: JSONValue]
    let helperID: String?
    @ObservedObject var resource: TabResource
    let canReset: Bool
    let title: String
    var onSave: ([String: JSONValue]) async -> Bool
    var onDone: () -> Void
    @StateObject private var models = TabResource()
    @State private var emoji = "🤖"
    @State private var avatar = ""
    @State private var name = ""
    @State private var job = ""
    @State private var instructions = ""
    @State private var model = ""
    @State private var choosing = false
    @State private var drawing = false
    @State private var photo: PhotosPickerItem?
    @State private var error: String?
    @State private var filled = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HelperFieldBlock(label: "名字", hint: "在聊天里输入「@名字」就能叫它。") {
                    HStack(spacing: 10) {
                        Button { withAnimation(.easeOut(duration: 0.15)) { choosing.toggle() } } label: {
                            ZStack {
                                HelperAvatar(emoji: emoji, avatar: avatar, size: 44, baseURL: store.baseURL)
                                if drawing { Circle().fill(.black.opacity(0.4)).frame(width: 44, height: 44); ProgressView().tint(.white) }
                            }
                        }.buttonStyle(.plain).accessibilityLabel("换头像")
                        TextField("比如：记账", text: $name).modifier(HelperField())
                            .onChange(of: name) { _, value in if value.count > 12 { name = String(value.prefix(12)) } }
                    }
                    if choosing { faceChoices }
                }
                HelperFieldBlock(label: "它负责什么", hint: "一句话。显示在帮手列表里；主助理也看这句话，决定什么事交给它。") {
                    TextField("比如：记下每笔收支，按月算账", text: $job).modifier(HelperField())
                        .onChange(of: job) { _, value in if value.count > 16 { job = String(value.prefix(16)) } }
                }
                HelperFieldBlock(label: "做事要求", hint: "它每次做事都会照着这里来：怎么做、交出什么样的结果、哪些事不能做。一行写一条就好。") {
                    TextField("比如：\n- 每笔记下日期、金额、类别\n- 每月给我一个汇总\n- 不要替我付款", text: $instructions, axis: .vertical)
                        .lineLimit(6...14).modifier(HelperField(vertical: true))
                }
                HelperFieldBlock(label: "用哪个模型", hint: "难的活可以给它配更强的模型，简单的用便宜快的。模型在「设置 → 模型」里添加。") {
                    Picker("用哪个模型", selection: $model) {
                        Text("跟主助理一样").tag("")
                        ForEach(models.payload["presets"].array.compactMap { $0["name"].nonemptyString }, id: \.self) { Text($0).tag($0) }
                        if !model.isEmpty && !models.payload["presets"].array.contains(where: { $0["name"].stringValue == model }) { Text(model).tag(model) }
                    }.pickerStyle(.menu).frame(maxWidth: .infinity, alignment: .leading).modifier(HelperField())
                }
                if let error { Text(error).font(.system(size: 14)).foregroundStyle(.red) }
                PrimaryButton(title: resource.busy ? "保存中…" : "保存", enabled: !name.trimmingCharacters(in: .whitespaces).isEmpty && !drawing, busy: resource.busy) {
                    Task {
                        let fields: [String: JSONValue] = ["emoji": .string(emoji), "avatar": .string(avatar), "name": .string(name.trimmingCharacters(in: .whitespaces)),
                                                           "title": .string(job.trimmingCharacters(in: .whitespaces)),
                                                           "instructions": .string(instructions.trimmingCharacters(in: .whitespacesAndNewlines)), "model": .string(model)]
                        if await onSave(fields) { onDone(); dismiss() } else { error = resource.error }
                    }
                }
                if canReset, let helperID {
                    Button("恢复默认设置") {
                        Task {
                            if await resource.change("companion.helpers.change", payload: ["action": .string("reset"), "id": .string(helperID)], store: store) { onDone(); dismiss() }
                            else { error = resource.error }
                        }
                    }.font(.system(size: 14)).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 40).disabled(resource.busy)
                }
            }.padding(20)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(CompanionPalette.surface)
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
        .task { await models.load("/api/webui/companion/models", store: store) }
        .task(id: photo) { await usePhoto() }
        .onAppear(perform: fill)
    }

    @ViewBuilder private var faceChoices: some View {
        HStack(spacing: 8) {
            PhotosPicker(selection: $photo, matching: .images) { chip("用照片", icon: "photo.badge.plus") }
            if helperID != nil {
                Button { Task { await draw() } } label: { chip(drawing ? "画着呢，一分钟左右…" : "让 AI 画一个", icon: "sparkles") }
                    .buttonStyle(.plain).disabled(drawing)
            }
            if !avatar.isEmpty {
                Button("不用图片") { avatar = "" }.font(.system(size: 13.5)).foregroundStyle(.secondary)
            }
        }
        LazyVGrid(columns: Array(repeating: GridItem(.fixed(44), spacing: 6), count: 6), alignment: .leading, spacing: 6) {
            ForEach(DrawnIcons.faceChoices, id: \.self) { choice in
                Button { emoji = choice; avatar = ""; choosing = false } label: {
                    EmojiIcon(emoji: choice, size: 32).frame(width: 44, height: 44).background(CompanionPalette.elevated, in: Circle())
                        .overlay(Circle().strokeBorder(avatar.isEmpty && emoji == choice ? CompanionPalette.primary : .clear, lineWidth: 2))
                }.buttonStyle(.plain).accessibilityLabel(choice)
            }
        }
    }

    private func chip(_ text: String, icon: String) -> some View {
        Label(text, systemImage: icon).font(.system(size: 13.5)).padding(.horizontal, 12).frame(minHeight: 36)
            .background(CompanionPalette.elevated, in: Capsule()).foregroundStyle(.primary)
    }

    private func fill() {
        guard !filled else { return }
        filled = true
        emoji = start["emoji"]?.nonemptyString ?? "🤖"; avatar = start["avatar"]?.stringValue ?? ""
        name = start["name"]?.stringValue ?? ""; job = start["title"]?.stringValue ?? ""
        instructions = start["instructions"]?.stringValue ?? ""; model = start["model"]?.stringValue ?? ""
    }

    /// 让 AI 画一个: the image model draws its face (a minute or so); it is put on right away.
    private func draw() async {
        guard let helperID else { return }
        drawing = true; error = nil
        defer { drawing = false }
        let idea = "\(name.trimmingCharacters(in: .whitespaces))，\(job.trimmingCharacters(in: .whitespaces))"
        if await resource.change("companion.helpers.change", payload: ["action": .string("avatar"), "id": .string(helperID), "idea": .string(idea)], store: store) {
            avatar = resource.payload["helpers"].array.first { $0["id"].stringValue == helperID }?["avatar"].stringValue ?? ""
        } else { error = resource.error }
    }

    /// A photo, cut square and made small (192 px JPEG) to keep with the helper.
    private func usePhoto() async {
        guard let photo else { return }
        guard let data = try? await photo.loadTransferable(type: Data.self), let image = UIImage(data: data), image.size.width > 0 else {
            error = "这张图片打不开"; return
        }
        let side = min(image.size.width, image.size.height)
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let square = UIGraphicsImageRenderer(size: CGSize(width: 192, height: 192), format: format).image { _ in
            let scale = 192 / side
            image.draw(in: CGRect(x: -(image.size.width - side) / 2 * scale, y: -(image.size.height - side) / 2 * scale,
                                  width: image.size.width * scale, height: image.size.height * scale))
        }
        if let bytes = square.jpegData(compressionQuality: 0.85) {
            avatar = "data:image/jpeg;base64," + bytes.base64EncodedString()
            choosing = false
        }
    }
}

// MARK: - 群聊

/// A group's name and which helpers are in it (2~6), as the web's GroupSheet.
@MainActor
struct GroupSheet: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    let group: TabRecord?
    @ObservedObject var resource: TabResource
    var onSaved: (String?) -> Void
    var onDeleted: (() -> Void)? = nil
    @State private var name = ""
    @State private var members: [String] = []
    @State private var confirmDelete = false

    private var limit: Int { resource.payload["max_group"].intValue ?? 6 }

    var body: some View {
        SheetPage(title: group == nil ? "新建群聊" : "群聊设置") {
            VStack(alignment: .leading, spacing: 18) {
                HelperFieldBlock(label: "群名", hint: "不填就用成员的名字。") {
                    TextField("比如：买房小组", text: $name).modifier(HelperField())
                        .onChange(of: name) { _, value in if value.count > 20 { name = String(value.prefix(20)) } }
                }
                HelperFieldBlock(label: "成员 · \(members.count)/\(limit)", hint: "选 2~\(limit) 个帮手。在群里 @ 谁就交给谁，不 @ 由主助理来派。") {
                    VStack(spacing: 0) {
                        ForEach(resource.payload["helpers"].array, id: \.selfID) { helper in
                            let id = helper["id"].stringValue
                            let on = members.contains(id)
                            Button { toggle(id) } label: {
                                HStack(spacing: 12) {
                                    HelperAvatar(emoji: helper["emoji"].stringValue, avatar: helper["avatar"].stringValue, size: 34, baseURL: store.baseURL)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(helper["name"].stringValue).font(.system(size: 15, weight: .medium))
                                        if let title = helper["title"].nonemptyString { Text(title).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(1) }
                                    }
                                    Spacer()
                                    ZStack {
                                        Circle().strokeBorder(on ? .clear : CompanionPalette.border, lineWidth: 1.5)
                                            .background(Circle().fill(on ? CompanionPalette.primary : .clear))
                                        if on { Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(CompanionPalette.primaryText) }
                                    }.frame(width: 24, height: 24)
                                }.padding(.horizontal, 14).padding(.vertical, 10).contentShape(Rectangle())
                            }.buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
                            Divider().overlay(CompanionPalette.border)
                        }
                    }.background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18))
                }
                if let error = resource.error { Text(error).font(.system(size: 14)).foregroundStyle(.red) }
                PrimaryButton(title: resource.busy ? "保存中…" : (group == nil ? "建好，开始聊" : "保存"), enabled: members.count >= 2, busy: resource.busy) {
                    Task { await save() }
                }
                if let group {
                    if confirmDelete {
                        HStack(spacing: 8) {
                            SecondaryButton(title: "取消") { confirmDelete = false }
                            PrimaryButton(title: "删除", danger: true) {
                                Task {
                                    let groupChatID = group["chat_id"].stringValue
                                    if await resource.change("companion.groups.change", payload: ["action": .string("delete"), "id": .string(group.id)], store: store) {
                                        if !groupChatID.isEmpty { store.purgeLocalChat(groupChatID) }
                                        if let onDeleted { onDeleted() }
                                        else { onSaved(nil); dismiss() }
                                    }
                                }
                            }
                        }
                    } else {
                        Button("删除这个群聊") { confirmDelete = true }.font(.system(size: 14)).foregroundStyle(.red).frame(maxWidth: .infinity, minHeight: 40)
                    }
                }
            }
        }
        .onAppear {
            name = group?["name"].stringValue ?? ""
            members = group?["members"].array.compactMap(\.nonemptyString) ?? []
        }
    }

    private func toggle(_ id: String) {
        if let index = members.firstIndex(of: id) { members.remove(at: index) }
        else if members.count < limit { members.append(id) }
        Haptics.selection()
    }

    private func save() async {
        var payload: [String: JSONValue] = ["members": .array(members.map(JSONValue.string))]
        if let group {
            payload["action"] = .string("update"); payload["id"] = .string(group.id)
            payload["name"] = .string(name.trimmingCharacters(in: .whitespaces).isEmpty ? group["name"].stringValue : name.trimmingCharacters(in: .whitespaces))
        } else {
            payload["action"] = .string("create"); payload["name"] = .string(name.trimmingCharacters(in: .whitespaces))
        }
        if await resource.change("companion.groups.change", payload: payload, store: store) {
            onSaved(group == nil ? resource.payload["opened"].nonemptyString : nil)
            if group != nil { dismiss() }
        }
    }
}
