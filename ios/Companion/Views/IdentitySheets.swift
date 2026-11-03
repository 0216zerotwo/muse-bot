import MarkdownUI
import SwiftUI
import UIKit

/// The sheets the companion's page opens from below (as Muse's): a task, an approval, a standing yes,
/// a scheduled task and its runs, SOUL.md / MEMORY.md, its name, its look.

// MARK: - Shared pieces

/// Dates as the page writes them: 今天 / 下午7:45 / 10月20日 / 10.16.26.
enum SheetTime {
    private static func formatter(_ format: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = format
        return formatter
    }
    private static let clockFormat = formatter("ah:mm")
    private static let monthDayFormat = formatter("M月d日")
    private static let weekdayFormat = formatter("M月d日 EEE")
    private static let fullFormat = formatter("M月d日ah:mm")
    private static let dottedFormat = formatter("MM.dd.yy")

    private static func date(_ ms: Double) -> Date { Date(timeIntervalSince1970: ms / 1000) }
    static func clock(_ ms: Double) -> String { clockFormat.string(from: date(ms)) }
    static func monthDay(_ ms: Double) -> String { monthDayFormat.string(from: date(ms)) }
    static func weekday(_ ms: Double) -> String { weekdayFormat.string(from: date(ms)) }
    static func full(_ ms: Double) -> String { fullFormat.string(from: date(ms)) }
    static func dotted(_ ms: Double) -> String { dottedFormat.string(from: date(ms)) }
    static func day(_ ms: Double) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date(ms)) { return "今天" }
        if calendar.isDateInYesterday(date(ms)) { return "昨天" }
        return monthDay(ms)
    }
}

/// 已完成 / 已被拒 / 已预定…: a small pill at the top of a sheet.
struct StatusChip: View {
    enum Tone { case good, bad, neutral, busy }
    let text: String
    let tone: Tone
    var body: some View {
        HStack(spacing: 5) {
            if tone == .busy { ProgressView().controlSize(.mini) }
            else if tone != .neutral { Circle().fill(color).frame(width: 6, height: 6) }
            Text(text).font(.system(size: 12, weight: .semibold))
        }
        .foregroundStyle(tone == .neutral ? Color.secondary : color)
        .padding(.horizontal, 9).padding(.vertical, 4)
        .background((tone == .neutral ? Color.secondary : color).opacity(0.13), in: Capsule())
    }
    private var color: Color {
        switch tone {
        case .good: return Color(red: 0.16, green: 0.62, blue: 0.32)
        case .bad: return .red
        case .busy: return .orange
        case .neutral: return .secondary
        }
    }
}

/// The top of a sheet: its chip and ×.
private struct SheetTop: View {
    let chip: StatusChip
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        HStack(alignment: .top) {
            chip
            Spacer()
            Button { dismiss() } label: {
                Image(systemName: "xmark").font(.system(size: 13, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 30, height: 30).background(CompanionPalette.elevated, in: Circle())
            }.buttonStyle(.plain).accessibilityLabel("关闭")
        }
    }
}

private extension View {
    /// A sheet from below, half height first (drag up for all of it).
    func pageSheet() -> some View {
        self.presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
            .presentationCornerRadius(28).presentationBackground(CompanionPalette.background)
            .tint(CompanionPalette.primary)
    }
}

// MARK: - A task

struct TaskSheet: View {
    let task: ActivityTask
    @EnvironmentObject private var store: CompanionStore
    @State private var report: JSONValue?
    @State private var reportError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                SheetTop(chip: chip)
                Text(task.title).font(.system(size: 18, weight: .semibold)).padding(.top, 14)
                if task.summary != task.title { Text(task.summary).font(.system(size: 15)).padding(.top, 6) }
                Text(SheetTime.full(task.at) + (task.end.map { $0 > task.at ? " · 用时 " + Elapsed.words($0 - task.at) : "" } ?? ""))
                    .font(.system(size: 13)).foregroundStyle(.secondary).padding(.top, 6)
                Divider().padding(.vertical, 16)
                if task.runID != nil { runBody } else if !task.reply.isEmpty {
                    Markdown(task.reply).markdownTextStyle { FontSize(15) }.textSelection(.enabled)
                }
                if !task.steps.isEmpty {
                    Text("步骤").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary).padding(.top, 20).padding(.bottom, 8)
                    ForEach(task.steps) { step in StepRow(step: step, live: task.status == .running && step.running) }
                }
            }.padding(20)
        }
        .task { await loadReport() }
        .pageSheet()
    }

    private var chip: StatusChip {
        switch task.status {
        case .running: return StatusChip(text: "进行中", tone: .busy)
        case .failed: return StatusChip(text: "没有完成", tone: .bad)
        case .done: return StatusChip(text: "已完成", tone: .good)
        }
    }

    @ViewBuilder private var runBody: some View {
        if let report {
            if report["silent"].boolValue {
                Text("这次运行按设定没有发消息（结果写进了动态等地方）。").font(.system(size: 15)).foregroundStyle(.secondary)
            } else if let text = report["text"].nonemptyString {
                if report["status"].stringValue == "error" {
                    Text(text).font(.system(size: 13, design: .monospaced)).foregroundStyle(.red).textSelection(.enabled)
                } else {
                    Markdown(text).markdownTextStyle { FontSize(15) }.textSelection(.enabled)
                }
            } else { Text("这次运行没有留下内容。").font(.system(size: 15)).foregroundStyle(.secondary) }
        } else if let reportError {
            Text(reportError).font(.system(size: 14)).foregroundStyle(.secondary)
        } else { ProgressView().frame(maxWidth: .infinity) }
    }

    private func loadReport() async {
        guard let job = task.jobID, let run = task.runID else { return }
        do { report = try await store.getResource("/api/webui/automations/\(job)/runs/\(run)") }
        catch { reportError = "这次运行的记录没能读取。" }
    }
}

/// One step: done (✓), failed (✗) or under way.
struct StepRow: View {
    let step: ActivityStep
    let live: Bool
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Group {
                if live { ProgressView().controlSize(.small) }
                else {
                    Image(systemName: step.failed ? "xmark.circle" : "checkmark.circle")
                        .foregroundStyle(step.failed ? Color.red : Color(red: 0.16, green: 0.62, blue: 0.32))
                }
            }.font(.system(size: 17)).frame(width: 20).padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(step.title).font(.system(size: 15, weight: .medium))
                if !step.detail.isEmpty { Text(step.detail).font(.system(size: 13)).foregroundStyle(.secondary).lineLimit(3) }
            }
            Spacer(minLength: 0)
        }.padding(.vertical, 7)
    }
}

// MARK: - An approval

struct ApprovalSheet: View {
    let approval: JSONValue
    var body: some View {
        let outcome = ApprovalWords.outcome(approval)
        let args = approval["arguments"]
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                SheetTop(chip: StatusChip(text: outcome.label, tone: outcome.allowed == false ? .bad : outcome.allowed == true ? .good : .busy))
                Text(ApprovalWords.title(approval)).font(.system(size: 18, weight: .semibold)).padding(.top, 14)
                let subject = ApprovalWords.subject(approval)
                if !subject.isEmpty { Text(subject).font(.system(size: 15)).padding(.top, 6) }
                Text(SheetTime.full(approval["decided_at_ms"].doubleValue ?? approval["created_at_ms"].doubleValue ?? 0))
                    .font(.system(size: 13)).foregroundStyle(.secondary).padding(.top, 6)
                if approval["status"].stringValue == "failed", let result = approval["result"].nonemptyString {
                    Text(result).font(.system(size: 13)).foregroundStyle(.red).padding(.top, 12).textSelection(.enabled)
                }
                let keys = args.object.keys.sorted().filter { !["steps", "account_name"].contains($0) && !ApprovalWords.argumentText(args[$0]).isEmpty }
                if !keys.isEmpty || approval["context"]["egress"] != .null {
                    Text("详细信息").font(.system(size: 13, weight: .semibold)).padding(.top, 20).padding(.bottom, 8)
                    VStack(alignment: .leading, spacing: 10) {
                        if approval["context"]["egress"] != .null {
                            detail("网站", approval["context"]["egress"]["host"].stringValue)
                            detail("内容", approval["context"]["egress"]["what"].stringValue)
                        }
                        ForEach(keys, id: \.self) { key in detail(ApprovalWords.argumentLabel(key), ApprovalWords.argumentText(args[key]), mono: key == "command") }
                    }
                    .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                    .background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 14))
                }
            }.padding(20)
        }
        .pageSheet()
    }

    private func detail(_ label: String, _ value: String, mono: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label).font(.system(size: 12)).foregroundStyle(.secondary)
            Text(value).font(mono ? .system(size: 13, design: .monospaced) : .system(size: 14)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A standing yes (始终允许 / 24 小时内允许), which can be taken back.
struct GrantSheet: View {
    let grant: JSONValue
    var changed: ([JSONValue]) -> Void
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SheetTop(chip: StatusChip(text: grant["scope"].stringValue == "24h" ? "24 小时内允许" : "始终允许", tone: .good))
            Text(grant["label"].stringValue).font(.system(size: 18, weight: .semibold)).padding(.top, 14)
            Text("从 " + SheetTime.full(grant["created_at_ms"].doubleValue ?? 0) + (grant["expires_at_ms"].doubleValue.map { " 到 " + SheetTime.full($0) } ?? " 起，一直有效"))
                .font(.system(size: 13)).foregroundStyle(.secondary).padding(.top, 6)
            Text("同样的事不再问你，直接去做。撤销后，下次会先问你。").font(.system(size: 14)).foregroundStyle(.secondary).padding(.top, 14)
            if let error { Text(error).font(.system(size: 13)).foregroundStyle(.red).padding(.top, 10) }
            Spacer(minLength: 20)
            Button { Task { await revoke() } } label: {
                Text(busy ? "正在撤销…" : "撤销").font(.system(size: 16, weight: .semibold)).frame(maxWidth: .infinity, minHeight: 46)
                    .background(Color.red, in: Capsule()).foregroundStyle(.white)
            }.buttonStyle(.plain).disabled(busy)
        }
        .padding(20)
        .pageSheet()
    }
    private func revoke() async {
        busy = true; error = nil
        defer { busy = false }
        do {
            let value = try await store.changeResource("approval.grants.revoke", payload: ["key": grant["key"]])
            changed(value["grants"].records); Haptics.selection(); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

// MARK: - A scheduled task

struct JobSheet: View {
    let job: JSONValue
    var changed: ([JSONValue]) -> Void
    var editInChat: () -> Void
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var current: JSONValue?
    @State private var busy = false
    @State private var error: String?
    @State private var removing = false

    private var shown: JSONValue { current ?? job }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    SheetTop(chip: chip)
                    Text(shown["name"].stringValue).font(.system(size: 18, weight: .semibold)).padding(.top, 14)
                    Text(Schedules.describe(shown)).font(.system(size: 14)).foregroundStyle(.secondary).padding(.top, 3)
                    Button("在聊天中编辑", action: editInChat).font(.system(size: 14, weight: .medium)).foregroundStyle(.blue).padding(.top, 8)
                    runs.padding(.top, 16)
                    HStack(spacing: 10) {
                        pill(shown["enabled"].boolValue ? "暂停" : "继续") { await act(shown["enabled"].boolValue ? "automation.disable" : "automation.enable") }
                        pill("现在运行") { await act("automation.run") }
                    }.padding(.top, 16)
                    if shown["protected"].boolValue {
                        Text("这是自带的任务（动态、主动关心），要关掉请到对应的页面里设置。").font(.system(size: 13)).foregroundStyle(.secondary).padding(.top, 14)
                    } else {
                        Button { removing = true } label: {
                            Text("删除").font(.system(size: 16, weight: .semibold)).frame(maxWidth: .infinity, minHeight: 46)
                                .background(Color(red: 0.75, green: 0.11, blue: 0.16), in: Capsule()).foregroundStyle(.white)
                        }.buttonStyle(.plain).disabled(busy).padding(.top, 14)
                    }
                    if let error { Text(error).font(.system(size: 13)).foregroundStyle(.red).padding(.top, 10) }
                }.padding(20)
            }
            .toolbar(.hidden, for: .navigationBar)
            .confirmationDialog("删除「\(shown["name"].stringValue)」？", isPresented: $removing, titleVisibility: .visible) {
                Button("删除", role: .destructive) { Task { if await act("automation.delete") { dismiss() } } }
            }
        }
        .pageSheet()
    }

    private var chip: StatusChip {
        if !shown["enabled"].boolValue { return StatusChip(text: "已暂停", tone: .neutral) }
        return shown["state"]["next_run_at_ms"].doubleValue == nil ? StatusChip(text: "已结束", tone: .neutral) : StatusChip(text: "已预定", tone: .neutral)
    }

    private var runs: some View {
        VStack(spacing: 0) {
            if shown["enabled"].boolValue, let next = shown["state"]["next_run_at_ms"].doubleValue {
                RunLine(at: next, trailing: "已预定", chevron: false)
                Divider()
            }
            ForEach(shown["runs"].array.prefix(3).map { IdentityView.Record(id: $0["id"].stringValue, value: $0) }) { run in
                NavigationLink { RunReportView(jobID: shown["id"].stringValue, run: run.value) } label: {
                    RunLine(at: run.value["at_ms"].doubleValue ?? 0, trailing: run.value["status"].stringValue == "error" ? "出错了" : "成功", chevron: true)
                }.buttonStyle(.plain)
                Divider()
            }
            NavigationLink { RunListView(job: shown) } label: {
                HStack {
                    Text("查看运行记录").font(.system(size: 15))
                    Spacer()
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(.tertiary)
                }.frame(minHeight: 46).contentShape(Rectangle())
            }.buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 14))
    }

    private func pill(_ title: String, _ run: @escaping () async -> Bool) -> some View {
        Button { Task { _ = await run() } } label: {
            Text(title).font(.system(size: 15, weight: .medium)).frame(maxWidth: .infinity, minHeight: 42)
                .background(CompanionPalette.elevated, in: Capsule()).foregroundStyle(.primary)
        }.buttonStyle(.plain).disabled(busy)
    }

    @discardableResult
    private func act(_ action: String) async -> Bool {
        busy = true; error = nil
        defer { busy = false }
        do {
            let value = try await store.changeResource(action, payload: ["id": shown["id"]])
            let jobs = value["jobs"].records
            changed(jobs)
            current = jobs.first { $0["id"] == shown["id"] }
            Haptics.selection()
            return true
        } catch { self.error = error.localizedDescription; return false }
    }
}

private struct RunLine: View {
    let at: Double
    let trailing: String
    let chevron: Bool
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(SheetTime.weekday(at)).font(.system(size: 15, weight: .medium))
                Text(SheetTime.clock(at)).font(.system(size: 13)).foregroundStyle(.secondary)
            }
            Spacer()
            Text(trailing).font(.system(size: 14)).foregroundStyle(trailing == "出错了" ? Color.red : Color.secondary)
            if chevron { Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(.tertiary) }
        }.padding(.vertical, 10).contentShape(Rectangle())
    }
}

/// 运行记录: every kept run of a task.
private struct RunListView: View {
    let job: JSONValue
    var body: some View {
        List {
            Section {
                if job["runs"].array.isEmpty { Text("还没有运行过。").foregroundStyle(.secondary) }
                ForEach(job["runs"].array.map { IdentityView.Record(id: $0["id"].stringValue, value: $0) }) { run in
                    NavigationLink { RunReportView(jobID: job["id"].stringValue, run: run.value) } label: {
                        RunLine(at: run.value["at_ms"].doubleValue ?? 0, trailing: run.value["status"].stringValue == "error" ? "出错了" : "成功", chevron: false)
                    }
                }
            } header: { Text("任务：" + job["name"].stringValue) }
        }
        .scrollContentBackground(.hidden).background(CompanionPalette.background)
        .navigationTitle("运行记录").navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
    }
}

/// One run: what it answered, or why it failed.
private struct RunReportView: View {
    let jobID: String
    let run: JSONValue
    @EnvironmentObject private var store: CompanionStore
    @State private var report: JSONValue?
    @State private var error: String?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                StatusChip(text: run["status"].stringValue == "error" ? "出错了" : "成功", tone: run["status"].stringValue == "error" ? .bad : .good)
                Text(SheetTime.weekday(run["at_ms"].doubleValue ?? 0) + " " + SheetTime.clock(run["at_ms"].doubleValue ?? 0))
                    .font(.system(size: 13)).foregroundStyle(.secondary)
                if let report {
                    if report["silent"].boolValue {
                        Text("这次运行按设定没有发消息（结果写进了动态等地方）。").foregroundStyle(.secondary)
                    } else if let text = report["text"].nonemptyString {
                        if report["status"].stringValue == "error" {
                            Text(text).font(.system(size: 13, design: .monospaced)).foregroundStyle(.red).textSelection(.enabled)
                        } else { Markdown(text).markdownTextStyle { FontSize(15) }.textSelection(.enabled) }
                    } else { Text("这次运行没有留下内容。").foregroundStyle(.secondary) }
                } else if let error { Text(error).foregroundStyle(.secondary) }
                else { ProgressView().frame(maxWidth: .infinity) }
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(CompanionPalette.background)
        .navigationTitle("这次运行").navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .task {
            do { report = try await store.getResource("/api/webui/automations/\(jobID)/runs/\(run["id"].stringValue)") }
            catch { self.error = "这次运行的记录没能读取。" }
        }
    }
}

// MARK: - SOUL.md / MEMORY.md

/// The file opened from its card: read as a page, ✎ to edit it, ··· for the rest.
struct CompanionDocumentSheet: View {
    enum Kind: String, Identifiable {
        case soul, memory
        var id: String { rawValue }
        var file: String { self == .soul ? "SOUL.md" : "MEMORY.md" }
        var about: String {
            self == .soul
                ? "这是伙伴的性格设定：它是谁、怎么说话、怎么称呼你。每次对话开始时它都会读这份文件。你可以随时修改，保存后从下一次对话开始生效。这段说明不属于文件内容。"
                : "这是伙伴的长期记忆：它从你们的聊天里记下的、关于你的事。每次对话开始时它都会读，每小时自己整理一次。你可以直接修改：删掉一条它就忘了，加一条它就记住。这段说明不属于文件内容。"
        }
    }

    let kind: Kind
    var saved: (Double) -> Void
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var original = ""
    @State private var loading = true
    @State private var editing = false
    @State private var busy = false
    @State private var note: String?
    @State private var error: String?
    @State private var changes: [JSONValue] = []
    @State private var showChanges = false
    @State private var discarding = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Button { if editing && text != original { discarding = true } else { dismiss() } } label: {
                    Image(systemName: "chevron.down").font(.system(size: 15, weight: .semibold)).frame(width: 40, height: 40)
                        .background(CompanionPalette.elevated, in: Circle())
                }.buttonStyle(.plain).accessibilityLabel("关闭")
                Spacer()
                Text(kind.file).font(.system(size: 16, weight: .semibold))
                Spacer()
                if editing {
                    Button(busy ? "正在保存" : "保存") { Task { await save() } }
                        .font(.system(size: 15, weight: .semibold)).disabled(busy || text == original).frame(minWidth: 44, minHeight: 40)
                } else {
                    Button { editing = true } label: { Image(systemName: "square.and.pencil").font(.system(size: 17)).frame(width: 40, height: 40) }
                        .buttonStyle(.plain).disabled(loading).accessibilityLabel("编辑")
                }
                Menu {
                    if kind == .memory {
                        Button("现在更新", systemImage: "arrow.clockwise") { Task { await learn() } }
                        Button(showChanges ? "收起最近的改动" : "最近的改动", systemImage: "clock.arrow.circlepath") { showChanges.toggle() }
                    }
                    Button("复制全文", systemImage: "doc.on.doc") { UIPasteboard.general.string = text; note = "已复制" }
                    if editing {
                        Button(role: .destructive) { text = original; editing = false } label: { Label("放弃修改", systemImage: "arrow.uturn.backward") }
                    }
                } label: { Image(systemName: "ellipsis").font(.system(size: 17)).frame(width: 40, height: 40) }
                    .buttonStyle(.plain).accessibilityLabel("更多")
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            if let note { Text(note).font(.system(size: 13)).foregroundStyle(.secondary).padding(.bottom, 4) }
            if let error { Text(error).font(.system(size: 13)).foregroundStyle(.red).padding(.horizontal, 20).padding(.bottom, 4) }
            if loading {
                Spacer()
                ProgressView("正在加载文件…")
                Spacer()
            } else if editing {
                TextEditor(text: $text).font(.system(size: 15, design: .monospaced)).scrollContentBackground(.hidden)
                    .padding(.horizontal, 14).accessibilityLabel(kind.file + " 内容").disabled(busy)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        (Text("关于这个文件。").bold() + Text(kind.about)).italic().font(.system(size: 14)).foregroundStyle(.secondary)
                        if showChanges { changeList }
                        Markdown(text).markdownTextStyle { FontSize(15) }.textSelection(.enabled)
                    }.padding(.horizontal, 20).padding(.bottom, 30).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .background(CompanionPalette.background)
        .task { await load() }
        .confirmationDialog("放弃没保存的修改？", isPresented: $discarding, titleVisibility: .visible) {
            Button("放弃修改", role: .destructive) { dismiss() }
        }
        .interactiveDismissDisabled(editing && text != original)
        .presentationDetents([.large]).presentationDragIndicator(.hidden).presentationCornerRadius(28)
        .tint(CompanionPalette.primary)
    }

    @ViewBuilder private var changeList: some View {
        let live = changes.filter { !$0["undone"].boolValue }.prefix(8)
        VStack(alignment: .leading, spacing: 10) {
            Text("最近记下和改过的").font(.system(size: 13, weight: .semibold))
            if live.isEmpty { Text("最近没有改动。").font(.system(size: 13)).foregroundStyle(.secondary) }
            ForEach(Array(live.enumerated()), id: \.offset) { _, change in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(Self.changeLabel[change["kind"].stringValue] ?? "改动").font(.system(size: 11)).foregroundStyle(.secondary)
                        if !change["before"].array.isEmpty {
                            Text(change["before"].array.map(\.stringValue).joined(separator: " / ")).strikethrough().foregroundStyle(.secondary)
                        }
                        if let after = change["after"].nonemptyString { Text(after) }
                    }.font(.system(size: 13)).frame(maxWidth: .infinity, alignment: .leading)
                    Button("撤销") { Task { await undo(change["id"].stringValue) } }.font(.system(size: 13, weight: .medium)).disabled(busy)
                }
            }
        }.padding(14).background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 14))
    }

    /// How each change came about, as the web's 记忆 page says it.
    private static let changeLabel = ["learn-add": "从聊天里记下", "learn-update": "从聊天里更正", "learn-remove": "过时了，忘掉",
                                      "merge": "合并重复", "drop": "整理掉", "fade": "很久没提，记个大概", "forget": "很久没提，忘掉"]

    private var path: String { "/api/webui/companion/" + (kind == .soul ? "soul" : "memory-doc") }

    private func load() async {
        loading = true; error = nil
        defer { loading = false }
        do {
            let value = try await store.getResource(path)
            text = value["text"].stringValue; original = text
            if kind == .memory { changes = (try? await store.getResource("/api/webui/companion/memory"))?["changes"].records ?? [] }
        } catch { self.error = "文件没能读取，请检查连接后重新打开。" }
    }

    private func save() async {
        busy = true; error = nil
        defer { busy = false }
        do {
            let value = try await store.changeResource(kind == .soul ? "companion.soul.update" : "companion.memory.doc", payload: ["text": .string(text)])
            text = value["text"].stringValue; original = text; editing = false; note = "已保存"
            saved(Date().timeIntervalSince1970 * 1000)
            Haptics.selection()
        } catch { self.error = "保存失败，改动还在，请再试一次。" }
    }

    private func learn() async {
        busy = true; error = nil; note = "正在从最近的聊天里更新…"
        defer { busy = false }
        do {
            let result = try await store.changeResource("companion.memory.learn")
            await load()
            note = result["learned"]["error"].nonemptyString == nil ? "记忆已更新" : "这会儿没连上模型，稍后再试"
            saved(Date().timeIntervalSince1970 * 1000)
        } catch { note = nil; self.error = "更新失败，请稍后再试。" }
    }

    private func undo(_ id: String) async {
        busy = true; error = nil
        defer { busy = false }
        do {
            _ = try await store.changeResource("companion.memory.undo", payload: ["id": .string(id)])
            await load()
            note = "已撤销"
        } catch { self.error = "撤销失败，请再试一次。" }
    }
}

// MARK: - Its name

struct RenameSheet: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("编辑名称").font(.system(size: 17, weight: .semibold))
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 13, weight: .bold)).foregroundStyle(.secondary)
                        .frame(width: 30, height: 30).background(CompanionPalette.elevated, in: Circle())
                }.buttonStyle(.plain).accessibilityLabel("关闭")
            }
            TextField("名字", text: $name).font(.system(size: 17)).focused($focused).submitLabel(.done).onSubmit { Task { await save() } }
                .padding(.horizontal, 16).frame(minHeight: 48).background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 14))
            if let error { Text(error).font(.system(size: 13)).foregroundStyle(.red) }
            Button { Task { await save() } } label: {
                Text(busy ? "正在保存…" : "保存").font(.system(size: 16, weight: .semibold)).frame(maxWidth: .infinity, minHeight: 48)
                    .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText)
            }.buttonStyle(.plain).disabled(busy || clean.isEmpty)
        }
        .padding(20)
        .onAppear { name = store.companionName; focused = true }
        .presentationDetents([.height(250)]).presentationDragIndicator(.visible).presentationCornerRadius(28)
        .presentationBackground(CompanionPalette.background)
    }
    private var clean: String { String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(40)) }
    private func save() async {
        guard !clean.isEmpty, !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        do {
            store.companionState = try await store.changeResource("companion.update", payload: ["changes": .object(["name": .string(clean)])])
            store.cacheState()
            Haptics.selection(); dismiss()
        } catch { self.error = "名字没能保存，请再试一次。" }
    }
}

// MARK: - Its look

/// 更换虚拟形象: the looks it has (and the built-in cat), or describing a new one in the chat.
struct LookPicker: View {
    var describeNew: () -> Void
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var error: String?

    private struct Look: Identifiable {
        let id: String
        let name: String
        let picture: String?
    }

    private var looks: [Look] {
        [Look(id: "pet:orange-cat", name: "小橘", picture: nil)]
            + store.avatars.map { item in
                Look(id: (item["kind"].stringValue == "pet" ? "custom:" : "image:") + item["id"].stringValue,
                     name: item["name"].nonemptyString ?? "形象", picture: item["image_url"].nonemptyString)
            }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 3), spacing: 14) {
                    ForEach(looks) { look in
                        let chosen = store.companionState["avatar"].stringValue == look.id
                        Button { Task { await choose(look.id) } } label: {
                            VStack(spacing: 6) {
                                ZStack {
                                    CompanionPalette.elevated
                                    if let picture = look.picture { RemotePicture(path: picture, baseURL: store.baseURL, maxPixel: 320, quiet: true).padding(6) }
                                    else { EmojiIcon(emoji: look.id == "pet:orange-cat" ? "🐱" : "🐾", size: 44) }
                                }
                                .aspectRatio(1, contentMode: .fit).clipShape(RoundedRectangle(cornerRadius: 16))
                                .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(chosen ? CompanionPalette.primary : .clear, lineWidth: 2.5))
                                Text(look.name).font(.system(size: 12)).lineLimit(1).foregroundStyle(chosen ? .primary : .secondary)
                            }
                        }.buttonStyle(.plain).disabled(busy).accessibilityLabel(look.name + (chosen ? "，正在用" : ""))
                    }
                }.padding(16)
                if let error { Text(error).font(.system(size: 13)).foregroundStyle(.red).padding(.horizontal, 16) }
                Button(action: describeNew) {
                    Label("描述一个新形象", systemImage: "sparkles").font(.system(size: 15, weight: .medium))
                        .frame(maxWidth: .infinity, minHeight: 44).background(CompanionPalette.elevated, in: Capsule())
                }.buttonStyle(.plain).padding(.horizontal, 16).padding(.bottom, 20)
            }
            .background(CompanionPalette.background)
            .navigationTitle("更换虚拟形象").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() } } }
        }
        .presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
    }

    private func choose(_ value: String) async {
        guard store.companionState["avatar"].stringValue != value else { return }
        busy = true; error = nil
        defer { busy = false }
        do {
            store.companionState = try await store.changeResource("companion.update", payload: ["changes": .object(["avatar": .string(value)])])
            store.cacheState()
            Haptics.selection()
        } catch { self.error = "没能换上，请再试一次。" }
    }
}
