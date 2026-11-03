import SwiftUI
import MarkdownUI

@MainActor
struct SkillsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/skills")
    @State private var showBundled = false
    @State private var teaching = false
    var body: some View {
        Form {
            Section {
                Button { teaching = true } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "graduationcap").font(.title2)
                        VStack(alignment: .leading, spacing: 5) {
                            Text("教它一个新技能").font(.headline)
                            Text("在浏览器里亲手做一遍，它记下步骤，以后照着做").font(.subheadline).foregroundStyle(.secondary)
                        }
                        Spacer(); Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                    }.foregroundStyle(.primary).padding(.vertical, 7).contentShape(Rectangle())
                }.buttonStyle(.plain)  // a card in plain text colours, as the web's, not a tinted link
                NavigationLink("直接写一个新技能") { SkillDetailSettingsPage(name: "", bundled: false, isNew: true, resource: resource) }
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section("它学会的") {
                ForEach(SettingsDecoding.rows(resource.value, key: "learned", id: "name"), id: \.selfID) { skill in skillLink(skill, bundled: false) }
                if resource.value != .null && resource.value["learned"].array.isEmpty { Text("还没有。它把一件事做成后会自己记成技能，你也可以亲手教它。").foregroundStyle(.secondary) }
            }
            Section {
                DisclosureGroup("自带的 \(resource.value["bundled"].array.count) 个", isExpanded: $showBundled) {
                    ForEach(SettingsDecoding.rows(resource.value, key: "bundled", id: "name"), id: \.selfID) { skill in skillLink(skill, bundled: true) }
                }
            }
        }.settingsStyle("技能").task { await resource.load(store) }.refreshable { await resource.load(store) }
            .fullScreenCover(isPresented: $teaching, onDismiss: { Task { await resource.load(store) } }) {
                NavigationStack {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 16) {
                            Text("打开浏览器，点「接管控制」，再从「···」里选「把接下来的操作教给\(store.companionName)」，亲手做一遍；做完后检查并保存整理出的技能。").font(.subheadline).foregroundStyle(.secondary)
                            ComputerView(openAtOnce: true)
                        }.padding(20)
                    }.navigationTitle("教它做").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { teaching = false } } }
                }
            }
    }
    private func skillLink(_ skill: JSONValue, bundled: Bool) -> some View {
        NavigationLink { SkillDetailSettingsPage(name: skill["name"].stringValue, bundled: bundled, resource: resource) } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(skill["name"].stringValue.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " "))
                Text(skill["description"].stringValue).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                if !bundled { Text(SettingsDecoding.date(skill["updated_at"])).font(.caption).foregroundStyle(.secondary) }
            }.padding(.vertical, 5)
        }
    }
}

@MainActor
struct SkillDetailSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    var name: String
    var bundled: Bool
    var isNew = false
    @ObservedObject var resource: SettingsResource
    @State private var content = ""
    @State private var newName = ""
    @State private var editing = false
    @State private var loading = false
    @State private var confirming = false
    var body: some View {
        Form {
            if isNew { Section("技能名称") { TextField("小写英文和横线", text: $newName).textInputAutocapitalization(.never).autocorrectionDisabled() } }
            if !isNew {
                Section {
                    Button("用一次") { draft("用「\(name)」技能：") }
                    Button("定时做") { draft("每天早上 9 点用「\(name)」技能做一遍") }
                }
            }
            if loading { ProgressView("正在读取…") }
            Section("步骤") {
                if editing || isNew { TextEditor(text: $content).font(.system(.subheadline, design: .monospaced)).frame(minHeight: 320) }
                else { Markdown(content).textSelection(.enabled) }
            }
            SettingsStatus(resource: resource) { Task { await load() } }
            if !bundled {
                if editing || isNew {
                    Button("保存技能") {
                        let finalName = isNew ? newName.trimmingCharacters(in: .whitespacesAndNewlines) : name
                        Task {
                            if await resource.change(store, "companion.skills.change", ["action": .string("save"), "name": .string(finalName), "content": .string(content), "new": .bool(isNew)]) {
                                if isNew { dismiss() } else { editing = false }
                            }
                        }
                    }.disabled(resource.busy || content.isEmpty || (isNew && newName.isEmpty))
                } else {
                    Button("编辑步骤") { editing = true }
                    Button("删除这个技能", role: .destructive) { confirming = true }.disabled(resource.busy)
                }
            }
        }.settingsStyle(isNew ? "新技能" : name.replacingOccurrences(of: "-", with: " ")).task { if !isNew { await load() } }
            .confirmationDialog("删除这个技能？", isPresented: $confirming) {
                Button("删除", role: .destructive) { Task { if await resource.change(store, "companion.skills.change", ["action": .string("delete"), "name": .string(name)]) { dismiss() } } }
            }
    }
    private func load() async {
        loading = true; resource.error = nil
        defer { loading = false }
        do { let reply = try await store.changeResource("companion.skills.change", payload: ["action": .string("read"), "name": .string(name)]); content = reply["content"].stringValue }
        catch { resource.error = error.localizedDescription }
    }
    private func draft(_ text: String) {
        Task {
            if !store.mainChatID.isEmpty { await store.changeChat(store.mainChatID) }
            store.composeDraft(text)
        }
    }
}
