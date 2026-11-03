import SwiftUI

struct GoalArea: Identifiable {
    let id: String
    let name: String
    let icon: String
    static let all: [GoalArea] = [
        .init(id: "health", name: "健康", icon: "heart"), .init(id: "finance", name: "财务", icon: "banknote"),
        .init(id: "career", name: "事业", icon: "briefcase"), .init(id: "learning", name: "学习", icon: "graduationcap"),
        .init(id: "relationships", name: "人际", icon: "heart.text.square"), .init(id: "family", name: "家人", icon: "person.2"),
        .init(id: "home", name: "居家", icon: "house"), .init(id: "travel", name: "出行", icon: "safari"),
        .init(id: "creative", name: "创作", icon: "paintpalette"), .init(id: "other", name: "其他", icon: "tag")
    ]
}

@MainActor
struct GoalsScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var goals = TabResource()
    @StateObject private var routines = TabResource()
    @StateObject private var jobs = TabResource()
    @StateObject private var triggers = TabResource()
    @State private var selected: TabRecord?
    @State private var creating = false
    @State private var proactive = false
    @State private var area: GoalArea?
    var onChat: () -> Void = {}
    var onLibrary: () -> Void = {}
    private var records: [TabRecord] { TabRecords.items(goals.payload, key: "goals") }
    private var tracked: [TabRecord] { records.filter { !$0["done"].boolValue }.sorted { !$0["paused"].boolValue && $1["paused"].boolValue } }
    private var visibleJobs: [TabRecord] {
        TabRecords.items(jobs.payload, key: "jobs").filter { !$0["protected"].boolValue && !["每日早报", "审批回执"].contains($0["name"].stringValue) && !$0["name"].stringValue.hasPrefix("目标跟进：") && !$0["name"].stringValue.hasPrefix("自动触发：") }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                ScreenHeading(title: "目标", action: { creating = true })
                if let error = goals.error { ResourceErrorView(message: error, retry: { Task { await load() } }) }
                if goals.busy { ProgressView("正在读取…").frame(maxWidth: .infinity) }
                ForEach(Array(store.pendingApprovals.filter { $0["action"].stringValue == "goal_plan" }.enumerated()), id: \.offset) { _, approval in
                    NativeApprovalView(approval: approval) { decision, scope in await store.approve(id: approval["id"].stringValue, decision: decision, scope: scope); await goals.load("/api/webui/companion/goals", store: store) }.padding(.horizontal, 20)
                }
                Label("在跟进", systemImage: "circle.fill").font(.system(size: 15, weight: .semibold)).foregroundStyle(.green).padding(.horizontal, 20)
                if goals.hasLoaded && tracked.isEmpty { Text("还没有在跟进的目标").font(.system(size: 14)).foregroundStyle(.secondary).padding(.horizontal, 20) }
                ForEach(tracked) { goal in goalRow(goal) }
                if records.contains(where: { $0["done"].boolValue }) {
                    Text("已完成").font(.headline).padding(.horizontal, 20)
                    ForEach(records.filter { $0["done"].boolValue }) { goal in goalRow(goal) }
                }
                Text("例行").font(.headline).padding(.horizontal, 20).padding(.top, 16)
                Button { proactive = true } label: {
                    HStack(spacing: 13) {
                        Image(systemName: "bell.badge").font(.title2).foregroundStyle(CompanionPalette.primary)
                        VStack(alignment: .leading, spacing: 4) { Text("早报与主动关心").font(.system(size: 16, weight: .semibold)); Text(routineSummary).font(.system(size: 13)).foregroundStyle(.secondary) }
                        Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary)
                    }.padding(.horizontal, 20).frame(minHeight: 58)
                }.buttonStyle(.plain)
                if let error = routines.error { ResourceErrorView(message: error, retry: { Task { await routines.load("/api/webui/companion/routines", store: store) } }) }
                ForEach(visibleJobs) { job in
                    HStack {
                        Label { VStack(alignment: .leading, spacing: 4) { Text(job["name"].stringValue); Text(schedule(job)).font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "bolt").foregroundStyle(CompanionPalette.primary) }
                        Spacer()
                        Toggle(job["name"].stringValue, isOn: Binding(get: { job["enabled"].boolValue }, set: { value in Task { await jobs.change(value ? "automation.enable" : "automation.disable", payload: ["id": .string(job.id)], store: store) } })).labelsHidden().disabled(jobs.busy)
                    }.padding(.horizontal, 20).contextMenu {
                        Button("现在运行") { Task { await jobs.change("automation.run", payload: ["id": .string(job.id)], store: store) } }
                        Button("删除", role: .destructive) { Task { await jobs.change("automation.delete", payload: ["id": .string(job.id)], store: store) } }
                    }
                }
                if let error = jobs.error { ResourceErrorView(message: error, retry: { Task { await jobs.load("/api/webui/automations", store: store) } }) }
                Text("自动触发").font(.system(size: 14, weight: .semibold)).foregroundStyle(.secondary).padding(.horizontal, 20).padding(.top, 8)
                if let error = triggers.error { ResourceErrorView(message: error, retry: { Task { await triggers.load("/api/webui/companion/triggers", store: store) } }) }
                if TabRecords.items(triggers.payload, key: "triggers").isEmpty {
                    Text("还没有。跟\(store.companionName)说「来了重要邮件告诉我」「开会前 15 分钟提醒我」或「这个网页降价了叫我」，就会设好。").font(.system(size: 14)).foregroundStyle(.secondary).padding(.horizontal, 20)
                }
                ForEach(TabRecords.items(triggers.payload, key: "triggers")) { trigger in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(trigger["name"].nonemptyString ?? "自动触发").font(.headline)
                            Spacer()
                            Toggle("启用", isOn: Binding(get: { !trigger["paused"].boolValue }, set: { on in Task { await triggers.change("companion.triggers.change", payload: ["id": .string(trigger.id), "action": .string(on ? "resume" : "pause")], store: store) } })).labelsHidden().disabled(triggers.busy)
                        }
                        Text(trigger["instruction"].stringValue).font(.subheadline).foregroundStyle(.secondary)
                        Button("删除", role: .destructive) { Task { await triggers.change("companion.triggers.change", payload: ["id": .string(trigger.id), "action": .string("remove")], store: store) } }
                    }.padding(.horizontal, 20)
                }
                Divider().padding(.top, 6)
                Text("定个新目标").font(.system(size: 18, weight: .semibold)).padding(.horizontal, 20)
                ForEach(GoalArea.all.filter { $0.id != "other" }) { choice in
                    Button { area = choice } label: {
                        HStack(spacing: 16) { Image(systemName: choice.icon).font(.system(size: 22)).frame(width: 26); Text(choice.name).font(.system(size: 16)); Spacer(); Image(systemName: "plus").foregroundStyle(.secondary) }.frame(minHeight: 44).padding(.horizontal, 20)
                    }.buttonStyle(.plain)
                }
            }.padding(.top, 174).padding(.bottom, 24)
        }.task { await load() }.refreshable { await load() }
            .sheet(item: $selected) { goal in GoalDetailScreen(resource: goals, record: goal, onSend: send, onLibrary: { selected = nil; onLibrary() }) }
            .sheet(isPresented: $creating) { NewGoalScreen(resource: goals, onSend: send) }
            .sheet(isPresented: $proactive) { RoutineScreen(resource: routines) }
            .sheet(item: $area) { choice in
                NavigationStack {
                    VStack(alignment: .leading, spacing: 18) {
                        Text("定一个\(choice.name)目标").font(.title2.bold())
                        Text("先在聊天里说清楚想要什么，定好后进度在这里看。").foregroundStyle(.secondary)
                        Button("开始") { area = nil; send("我想定一个\(choice.name)方面的目标。一次问我一个简短的问题（具体想达成什么、为什么、什么时候之前、多久问我一次进展），问清楚后用目标工具建好，拆成具体的步骤。") }.buttonStyle(.borderedProminent).frame(minHeight: 44)
                    }.padding(24).toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { area = nil } } }
                }.presentationDetents([.medium])
            }
    }
    private var routineSummary: String {
        var parts: [String] = []; if let time = routines.payload["brief_time"].nonemptyString { parts.append("早报 \(time)") }; if routines.payload["checkins"].boolValue { parts.append("主动关心") }; return parts.isEmpty ? "关闭" : parts.joined(separator: " · ")
    }
    private func load() async {
        await goals.load("/api/webui/companion/goals", store: store)
        await routines.load("/api/webui/companion/routines", store: store)
        await jobs.load("/api/webui/automations", store: store)
        await triggers.load("/api/webui/companion/triggers", store: store)
    }
    private func send(_ text: String) { Task { await store.changeChat(store.mainChatID); if store.send(text: text) { onChat() } } }
    private func goalRow(_ goal: TabRecord) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Button { Task { await changeGoal(goal, changes: ["done": .bool(!goal["done"].boolValue)]) } } label: { Image(systemName: goal["done"].boolValue ? "checkmark.square.fill" : "square").font(.title2).frame(width: 44, height: 44) }.accessibilityLabel(goal["done"].boolValue ? "标为未完成" : "标为完成")
            Button { selected = goal } label: {
                VStack(alignment: .leading, spacing: 4) { Text(goal.title).font(.system(size: 16, weight: .semibold)); Text(goal["paused"].boolValue ? "已暂停" : "\(goal["steps"].array.filter { $0["status"].stringValue == "done" }.count)/\(goal["steps"].array.count) 步已完成").font(.system(size: 13)).foregroundStyle(.secondary) }.frame(maxWidth: .infinity, alignment: .leading).frame(minHeight: 44)
            }.buttonStyle(.plain)
        }.padding(.horizontal, 12)
    }
    private func changeGoal(_ goal: TabRecord, changes: [String: JSONValue]) async { await goals.change("companion.goals.change", payload: ["action": .string("update"), "id": .string(goal.id), "changes": .object(changes)], store: store) }
    /// "每天 08:00", "工作日 07:30", "每 30 分钟", as the web words a schedule.
    private func schedule(_ job: TabRecord) -> String { Schedules.describe(job.value) }
}

@MainActor
private struct RoutineScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var resource: TabResource
    @State private var brief = false
    @State private var time = "08:00"
    @State private var checkins = false
    var body: some View {
        NavigationStack {
            Form {
                Section { Toggle("每日早报", isOn: $brief); if brief { TextField("每天几点（HH:MM）", text: $time).keyboardType(.numbersAndPunctuation) }; Text("今天的日程、重要邮件、该记得的事").font(.footnote).foregroundStyle(.secondary) }
                Section { Toggle("主动关心", isOn: $checkins); Text("白天大约每 3 小时看看它记得的事和最近的聊天，有值得说的才找你；晚上 10 点到早上 9 点不打扰。每次会用一点模型额度。").font(.footnote).foregroundStyle(.secondary) }
                if let error = resource.error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("早报与主动关心").navigationBarTitleDisplayMode(.inline).toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("保存") { Task {
                    do {
                        if brief { _ = try await store.changeResource("companion.update", payload: ["changes": .object(["timezone": .string(TimeZone.current.identifier)])]) }
                        if await resource.change("companion.routines.update", payload: ["changes": .object(["brief_time": brief ? .string(time) : .null, "checkins": .bool(checkins)])], store: store) { dismiss() }
                    } catch { resource.error = error.localizedDescription }
                } }.disabled(resource.busy) }
            }
        }.presentationDetents([.medium, .large]).onAppear { brief = resource.payload["brief_time"].nonemptyString != nil; time = resource.payload["brief_time"].nonemptyString ?? "08:00"; checkins = resource.payload["checkins"].boolValue }
    }
}

@MainActor
private struct NewGoalScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var resource: TabResource
    var onSend: (String) -> Void
    @State private var title = ""
    @State private var category = ""
    @State private var due = ""
    @State private var checkin = ""
    @State private var steps = ""
    var body: some View {
        NavigationStack {
            Form {
                Section("新目标") { TextField("你想达成什么？", text: $title); GoalMetadataFields(category: $category, due: $due, checkin: $checkin); TextField("步骤，一行一个", text: $steps, axis: .vertical).lineLimit(3...10) }
                Section {
                    Button("让\(store.companionName)来定计划") { dismiss(); onSend("帮我定一个目标：「\(title)」。用目标工具拆成具体的步骤，然后告诉我计划。") }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("创建") { Task {
                        let changes: [String: JSONValue] = ["title": .string(title), "steps": .array(steps.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.map(JSONValue.string)), "category": category.isEmpty ? .null : .string(category), "due": due.isEmpty ? .null : .string(due), "checkin": checkin.isEmpty ? .null : .string(checkin)]
                        if await resource.change("companion.goals.change", payload: ["action": .string("add"), "changes": .object(changes)], store: store) { dismiss() }
                    } }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || resource.busy)
                }
                if let error = resource.error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("新目标").navigationBarTitleDisplayMode(.inline).toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } } }
        }.presentationDetents([.large])
    }
}

@MainActor
private struct GoalDetailScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var resource: TabResource
    let record: TabRecord
    var onSend: (String) -> Void
    var onLibrary: () -> Void = {}
    @State private var step = ""
    @State private var category = ""
    @State private var due = ""
    @State private var checkin = ""
    @State private var removing = false
    @State private var watching = false
    private var current: TabRecord { TabRecords.items(resource.payload, key: "goals").first { $0.id == record.id } ?? record }
    var body: some View {
        NavigationStack {
            Form {
                if store.running {
                    Section("正在推进") {
                        Label(store.activityLabel.isEmpty ? "\(store.companionName)正在工作" : store.activityLabel, systemImage: "sparkles")
                        Button("查看正在使用的浏览器") { watching = true }
                    }
                }
                Section("目标") { Text(current.title).font(.headline); GoalMetadataFields(category: $category, due: $due, checkin: $checkin); Button("保存设置") { Task { await change(changes: ["category": category.isEmpty ? .null : .string(category), "due": due.isEmpty ? .null : .string(due), "checkin": checkin.isEmpty ? .null : .string(checkin)]) } }.disabled(resource.busy) }
                Section("计划") {
                    ForEach(Array(current["steps"].array.enumerated()), id: \.offset) { index, item in
                        HStack {
                            Button { Task { await change(changes: ["step": .number(Double(index + 1)), "status": .string(item["status"].stringValue == "done" ? "pending" : "done")]) } } label: { Image(systemName: item["status"].stringValue == "done" ? "checkmark.circle.fill" : item["status"].stringValue == "blocked" ? "exclamationmark.octagon" : "circle").frame(width: 44, height: 44) }.accessibilityLabel("切换第 \(index + 1) 步完成状态")
                            VStack(alignment: .leading, spacing: 5) { Text("\(index + 1). " + item["text"].stringValue).strikethrough(item["status"].stringValue == "done"); if let note = item["note"].nonemptyString { Text(note).font(.caption).foregroundStyle(.secondary) } }
                        }
                    }
                    TextField("加一步", text: $step)
                    Button("添加这一步") { Task { if await change(changes: ["add_step": .string(step)]) { step = "" } } }.disabled(step.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || resource.busy)
                }
                if !current["log"].array.isEmpty { Section("工作记录") { ForEach(Array(current["log"].array.enumerated()), id: \.offset) { _, item in
                    VStack(alignment: .leading, spacing: 5) {
                        if item["kind"].stringValue == "file" { Button { onLibrary(); dismiss() } label: { Label(item["text"].stringValue, systemImage: "doc.text") } }
                        else { Text(item["text"].stringValue) }
                        Text(TabRecords.day(item["at"])).font(.caption).foregroundStyle(.secondary)
                    }
                } } }
                Section {
                    Button(current["done"].boolValue ? "标为未完成" : "标为完成") { Task { await change(changes: ["done": .bool(!current["done"].boolValue)]) } }
                    Button(current["paused"].boolValue ? "继续" : "暂停") { Task { await change(changes: ["paused": .bool(!current["paused"].boolValue)]) } }
                    Button("现在就推进") { dismiss(); onSend("继续推进我的目标「\(current.title)」：看看下一步，能做的现在就做。") }
                    Button("在对话里聊聊这个目标") { dismiss(); onSend("说说我的目标「\(current.title)」现在进展怎么样，接下来该做什么？") }
                    if current["checkin"].nonemptyString != nil { Button("现在问问我") { Task { if await change(action: "checkin") { dismiss() } } } }
                    Button("删除目标", role: .destructive) { removing = true }
                }.disabled(resource.busy)
                if let error = resource.error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("目标详情").navigationBarTitleDisplayMode(.inline).toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
        }.presentationDetents([.large]).onAppear { category = current["category"].stringValue; due = current["due"].stringValue; checkin = current["checkin"].stringValue }
            .fullScreenCover(isPresented: $watching) {
                NavigationStack { ComputerView().padding().navigationTitle("浏览器").toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { watching = false } } } }
            }
            .confirmationDialog("删除「\(current.title)」？", isPresented: $removing, titleVisibility: .visible) { Button("删除", role: .destructive) { Task { if await change(action: "remove") { dismiss() } } } }
    }
    @discardableResult private func change(action: String = "update", changes: [String: JSONValue] = [:]) async -> Bool { await resource.change("companion.goals.change", payload: ["action": .string(action), "id": .string(record.id), "changes": .object(changes)], store: store) }
}

private struct GoalMetadataFields: View {
    @Binding var category: String
    @Binding var due: String
    @Binding var checkin: String
    var body: some View {
        Picker("生活的哪个方面", selection: $category) { Text("没有分类").tag(""); ForEach(GoalArea.all) { Text($0.name).tag($0.id) } }
        TextField("截止日期（如 2026-12-31，可不填）", text: $due).keyboardType(.numbersAndPunctuation)
        GoalCadenceFields(value: $checkin)
    }
}

private struct GoalCadenceFields: View {
    @Binding var value: String
    private var parts: [String] { value.split(separator: " ").map(String.init) }
    private var cadence: String { parts.first ?? "" }
    private var at: String { parts.last.flatMap { $0.contains(":") ? $0 : nil } ?? "09:00" }
    private var anchor: String { parts.count > 2 ? parts[1] : cadence == "monthly" ? "1" : "mon" }
    private func set(_ cadence: String, _ anchor: String, _ at: String) { value = cadence.isEmpty ? "" : ([cadence] + (["weekly", "monthly"].contains(cadence) ? [anchor] : []) + [at]).joined(separator: " ") }
    var body: some View {
        Picker("多久问一次进展", selection: Binding(get: { cadence }, set: { set($0, $0 == "monthly" ? "1" : "mon", at) })) { Text("不提醒").tag(""); Text("每天").tag("daily"); Text("工作日").tag("weekdays"); Text("每周").tag("weekly"); Text("每月").tag("monthly") }
        if cadence == "weekly" {
            Picker("每周哪一天", selection: Binding(get: { anchor }, set: { set(cadence, $0, at) })) { ForEach(Array(zip(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], ["周一", "周二", "周三", "周四", "周五", "周六", "周日"])), id: \.0) { day in Text(day.1).tag(day.0) } }
        }
        if cadence == "monthly" { Picker("每月几号", selection: Binding(get: { anchor }, set: { set(cadence, $0, at) })) { ForEach(1...28, id: \.self) { Text("\($0) 日").tag(String($0)) } } }
        if !cadence.isEmpty { TextField("提醒时间（HH:MM）", text: Binding(get: { at }, set: { set(cadence, anchor, $0) })).keyboardType(.numbersAndPunctuation) }
    }
}
