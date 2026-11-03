import SwiftUI
import UIKit

/// The companion's page (tap its face), as Muse's: its face, name and state, then five panes behind
/// icons — 做过的事, 审核记录, 浏览器, 定时任务, 身份与记忆 — each row opening a sheet from below.
@MainActor
struct IdentityView: View {
    enum Pane: Int, CaseIterable {
        case activity, approvals, browser, schedules, identity
        var icon: String { ["list.bullet", "checkmark.shield", "display", "clock.arrow.circlepath", "touchid"][rawValue] }
        var label: String { ["做过的事", "审核记录", "浏览器", "定时任务", "身份与记忆"][rawValue] }
    }

    /// A row's record, for a sheet.
    struct Record: Identifiable {
        let id: String
        let value: JSONValue
    }

    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var pane: Pane = .activity
    @State private var jobs: [JSONValue] = []
    @State private var triggers: [JSONValue] = []
    @State private var records: [JSONValue] = []
    @State private var grants: [JSONValue] = []
    @State private var soulDate: Double?
    @State private var memoryDate: Double?
    @State private var loaded = false
    @State private var error: String?
    @State private var openTask: ActivityTask?
    @State private var openRecord: Record?
    @State private var openGrant: Record?
    @State private var openJob: Record?
    @State private var openDocument: CompanionDocumentSheet.Kind?
    @State private var renaming = false
    @State private var choosingLook = false
    @State private var shareItems: [Any]?

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                header
                paneBar.padding(.top, 18)
                Group {
                    switch pane {
                    case .activity: activityPane
                    case .approvals: approvalsPane
                    case .browser: ComputerView(asTask: true)
                    case .schedules: schedulesPane
                    case .identity: identityPane
                    }
                }.padding(.top, 20)
                if let error { ResourceErrorView(message: error) { Task { await load() } }.padding(.top, 16) }
            }
            .padding(.horizontal, 16).padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .background(CompanionPalette.background.ignoresSafeArea())
        .task {
            pane = store.pendingApprovals.isEmpty ? .activity : .approvals
            await load()
        }
        .sheet(item: $openTask) { task in TaskSheet(task: task) }
        .sheet(item: $openRecord) { record in ApprovalSheet(approval: record.value) }
        .sheet(item: $openGrant) { record in GrantSheet(grant: record.value) { grants = $0 } }
        .sheet(item: $openJob) { record in
            JobSheet(job: record.value, changed: { jobs = $0 }, editInChat: { editInChat(record.value) })
        }
        .sheet(item: $openDocument) { kind in
            CompanionDocumentSheet(kind: kind) { date in if kind == .soul { soulDate = date } else { memoryDate = date } }
        }
        .sheet(isPresented: $renaming) { RenameSheet() }
        .sheet(isPresented: $choosingLook) {
            LookPicker {
                // 描述一个新形象: to the main chat, where it draws one.
                choosingLook = false
                Task { await toMainChat("把虚拟形象改为") }
            }
        }
        .sheet(isPresented: Binding(get: { shareItems != nil }, set: { if !$0 { shareItems = nil } })) {
            ShareSheet(items: shareItems ?? []).presentationDetents([.medium, .large])
        }
        .inAppLinks()
    }

    // MARK: Header

    private var header: some View {
        ZStack(alignment: .top) {
            HStack {
                RoundIconButton(icon: "xmark", label: "关闭状态") { dismiss() }
                Spacer()
                RoundIconButton(icon: "square.and.arrow.up", label: "分享") { Task { await share() } }
            }
            VStack(spacing: 4) {
                Menu {
                    Button("更换虚拟形象", systemImage: "person.crop.circle") { choosingLook = true }
                    Button("编辑名称", systemImage: "pencil") { renaming = true }
                } label: {
                    AvatarView(value: store.companionState["avatar"].string, library: store.avatars,
                               animation: store.animation, size: 92, baseURL: store.baseURL)
                        .overlay(alignment: .bottomTrailing) {
                            Image(systemName: "pencil").font(.system(size: 11, weight: .bold)).foregroundStyle(.primary)
                                .frame(width: 24, height: 24).background(CompanionPalette.surface, in: Circle())
                                .shadow(color: .black.opacity(0.15), radius: 3, y: 1).offset(x: 2, y: -4)
                        }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("更换形象或名称")
                Text(store.companionName).font(.system(size: 22, weight: .semibold)).padding(.top, 4)
                status
                if store.running {
                    Button { Task { await store.stop() } } label: {
                        Label("停下", systemImage: "stop.fill").font(.system(size: 13.5, weight: .semibold))
                            .padding(.horizontal, 16).frame(minHeight: 32)
                            .background(Color.primary, in: Capsule()).foregroundStyle(CompanionPalette.background)
                    }.buttonStyle(.plain).padding(.top, 6)
                }
            }.frame(maxWidth: .infinity)
        }.padding(.top, 14)
    }

    private var status: some View {
        HStack(spacing: 5) {
            if store.running {
                ProgressView().controlSize(.mini)
                Text(store.activityLabel)
            } else if !store.pendingApprovals.isEmpty {
                Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                Text("等你确认")
            } else if store.connected {
                Image(systemName: "bolt.circle.fill").foregroundStyle(.green)
                Text("已连接")
            } else {
                Image(systemName: "bolt.slash.circle.fill").foregroundStyle(.secondary)
                Text("等待连接").foregroundStyle(.secondary)
            }
        }.font(.system(size: 15)).lineLimit(1)
    }

    private var paneBar: some View {
        HStack(spacing: 4) {
            ForEach(Pane.allCases, id: \.self) { item in
                Button { pane = item; Haptics.selection() } label: {
                    Image(systemName: item.icon).font(.system(size: 18, weight: .regular))
                        .frame(maxWidth: .infinity).frame(height: 40)
                        .background(pane == item ? CompanionPalette.surface : .clear, in: RoundedRectangle(cornerRadius: 12))
                        .shadow(color: pane == item ? .black.opacity(0.08) : .clear, radius: 3, y: 1)
                        .overlay(alignment: .topTrailing) {
                            let badge = item == .approvals ? store.pendingApprovals.count : 0
                            if badge > 0 {
                                Text("\(badge)").font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
                                    .frame(minWidth: 16, minHeight: 16).background(Color.red, in: Capsule()).offset(x: -6, y: 3)
                            }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.label)
                .accessibilityAddTraits(pane == item ? .isSelected : [])
            }
        }.padding(4).background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 16))
    }

    // MARK: 做过的事

    private var activity: [ActivityTask] {
        let main = store.mainChatID
        var tasks = ActivityLog.tasks(messages: store.projections[main]?.messages ?? [],
                                      running: store.runningChats.contains(main), jobs: jobs)
        let side = store.currentChatID
        if !side.isEmpty, side != main {
            tasks += ActivityLog.tasks(messages: store.projections[side]?.messages ?? [], running: store.runningChats.contains(side), jobs: [])
        }
        return tasks.sorted { $0.at > $1.at }
    }

    @ViewBuilder private var activityPane: some View {
        let tasks = activity
        if tasks.isEmpty {
            EmptyLine(text: loaded ? "还没做过什么事。让\(store.companionName)帮你查点东西、设个提醒，做过的事都会列在这里。" : nil)
        } else {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(Self.days(tasks), id: \.label) { day in
                    PaneHeading(text: day.label)
                    ForEach(day.tasks) { task in
                        Button { openTask = task } label: { TaskRow(task: task) }.buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private struct Day { let label: String; let tasks: [ActivityTask] }

    private static func days(_ tasks: [ActivityTask]) -> [Day] {
        var result: [Day] = []
        for task in tasks {
            let label = SheetTime.day(task.at)
            if result.last?.label == label { result[result.count - 1] = Day(label: label, tasks: result[result.count - 1].tasks + [task]) }
            else { result.append(Day(label: label, tasks: [task])) }
        }
        return result
    }

    // MARK: 审核记录

    private var pending: [JSONValue] {
        var seen: Set<String> = []
        return (store.approvals + records).filter { $0["status"].stringValue == "pending" && seen.insert($0["id"].stringValue).inserted }
    }

    @ViewBuilder private var approvalsPane: some View {
        let waiting = pending
        let history = records.filter { $0["status"].stringValue != "pending" }
        VStack(alignment: .leading, spacing: 0) {
            if !waiting.isEmpty {
                PaneHeading(text: "等你确认")
                VStack(spacing: 12) {
                    ForEach(waiting.map { Record(id: $0["id"].stringValue, value: $0) }) { record in
                        let approval = record.value
                        NativeApprovalView(approval: approval) { decision, scope in
                            await store.approve(id: approval["id"].stringValue, decision: decision, scope: scope)
                            await loadApprovals()
                        }
                    }
                }.padding(.bottom, 18)
            }
            PaneHeading(text: "审核记录")
            if history.isEmpty && grants.isEmpty {
                EmptyLine(text: loaded ? "还没有需要你确认过的事。发邮件、付款、在网页上提交这类事，\(store.companionName)都会先问你。" : nil)
            }
            ForEach(grants.map { Record(id: $0["key"].stringValue, value: $0) }) { record in
                let grant = record.value
                Button { openGrant = Record(id: grant["key"].stringValue, value: grant) } label: {
                    RecordRow(icon: "checkmark.seal", title: grant["label"].stringValue, detail: "",
                              status: (grant["scope"].stringValue == "24h" ? "24 小时内允许" : "始终允许") + " · " + SheetTime.monthDay(grant["created_at_ms"].doubleValue ?? 0))
                }.buttonStyle(.plain)
            }
            ForEach(history.map { Record(id: $0["id"].stringValue, value: $0) }) { record in
                let approval = record.value
                Button { openRecord = Record(id: approval["id"].stringValue, value: approval) } label: {
                    RecordRow(icon: ApprovalWords.icon(approval), title: ApprovalWords.title(approval), detail: ApprovalWords.subject(approval),
                              status: ApprovalWords.outcome(approval).label + " · "
                                + SheetTime.monthDay(approval["decided_at_ms"].doubleValue ?? approval["created_at_ms"].doubleValue ?? 0))
                }.buttonStyle(.plain)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: 定时任务

    private static func group(_ job: JSONValue) -> String {
        let schedule = job["schedule"]
        switch schedule["kind"].stringValue {
        case "at": return "提醒"
        case "every": return "时间间隔"
        case "cron":
            let words = Schedules.cron(schedule["expr"].stringValue)
            if words.hasPrefix("每天") || words.hasPrefix("工作日") { return "每日" }
            if words.hasPrefix("每周") { return "每周" }
            if words.hasPrefix("每月") { return "每月" }
            return "其他"
        default: return "其他"
        }
    }

    @ViewBuilder private var schedulesPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            if jobs.isEmpty && triggers.isEmpty {
                EmptyLine(text: loaded ? "还没有定时任务。跟\(store.companionName)说「每天早上 8 点提醒我喝水」，就会设好。" : nil)
            }
            ForEach(["提醒", "每日", "时间间隔", "每周", "每月", "其他"], id: \.self) { name in
                let members = jobs.filter { Self.group($0) == name }
                if !members.isEmpty {
                    PaneHeading(text: name)
                    ForEach(members.map { Record(id: $0["id"].stringValue, value: $0) }) { record in
                        let job = record.value
                        Button { openJob = Record(id: job["id"].stringValue, value: job) } label: { JobRow(job: job) }.buttonStyle(.plain)
                    }
                }
            }
            if !triggers.isEmpty {
                PaneHeading(text: "自动触发")
                ForEach(triggers.map { Record(id: $0["id"].stringValue, value: $0) }) { record in
                    let trigger = record.value
                    Menu {
                        let paused = trigger["paused"].boolValue
                        Button(paused ? "继续" : "暂停", systemImage: paused ? "play" : "pause") { Task { await changeTrigger(trigger, paused ? "resume" : "pause") } }
                        Button(role: .destructive) { Task { await changeTrigger(trigger, "remove") } } label: { Label("删除", systemImage: "trash") }
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            EmojiIcon(emoji: Schedules.triggerEmoji[trigger["kind"].stringValue] ?? "🔔", size: 34)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(trigger["name"].stringValue).font(.system(size: 16, weight: .semibold)).lineLimit(1)
                                Text(trigger["paused"].boolValue ? "已暂停" : Schedules.triggerWhen(trigger)).font(.system(size: 14)).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                        }.padding(.vertical, 9).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: 身份与记忆

    private var identityPane: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(store.companionName).font(.system(size: 20, weight: .semibold))
            Button { renaming = true } label: {
                Label("编辑", systemImage: "pencil").font(.system(size: 15, weight: .medium))
                    .frame(maxWidth: .infinity, minHeight: 38).background(CompanionPalette.elevated, in: Capsule())
                    .foregroundStyle(.primary)
            }.buttonStyle(.plain).accessibilityLabel("编辑名称")
            HStack(spacing: 12) {
                DocumentCard(kind: .soul, date: soulDate) { openDocument = .soul }
                DocumentCard(kind: .memory, date: memoryDate) { openDocument = .memory }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: Loading and actions

    private func load() async {
        error = nil
        if !store.mainChatID.isEmpty, store.projections[store.mainChatID] == nil { await store.hydrate(store.mainChatID) }
        let automations = try? await store.getResource("/api/webui/automations")
        if let automations { jobs = automations["jobs"].records } else { error = "定时任务没能读取，请检查连接。" }
        await loadApprovals()
        loaded = true
        triggers = (try? await store.getResource("/api/webui/companion/triggers"))?["triggers"].records ?? []
        soulDate = (try? await store.getResource("/api/webui/companion/soul"))?["updated_at"].doubleValue
        memoryDate = (try? await store.getResource("/api/webui/companion/memory-doc"))?["updated_at"].doubleValue
    }

    private func loadApprovals() async {
        await store.refreshApprovals()
        let all = try? await store.getResource("/api/webui/approvals?all=1")
        if let all { records = all["approvals"].records }
        let standing = try? await store.getResource("/api/webui/approvals/grants")
        if let standing { grants = standing["grants"].records }
    }

    private func changeTrigger(_ trigger: JSONValue, _ action: String) async {
        do {
            let value = try await store.changeResource("companion.triggers.change", payload: ["id": trigger["id"], "action": .string(action)])
            triggers = value["triggers"].records
            Haptics.selection()
        } catch { self.error = error.localizedDescription }
    }

    /// 在聊天中编辑: the main chat, with the request started.
    private func editInChat(_ job: JSONValue) {
        openJob = nil
        Task { await toMainChat("修改定时任务「\(job["name"].stringValue)」：") }
    }

    private func toMainChat(_ draft: String) async {
        if !store.mainChatID.isEmpty { await store.changeChat(store.mainChatID) }
        store.composeDraft(draft)
        dismiss()
    }

    /// Its picture and name, to send to someone.
    private func share() async {
        var items: [Any] = ["我的 AI 伙伴：\(store.companionName)"]
        let look = store.companionState["avatar"].stringValue
        let id = String(look.split(separator: ":", maxSplits: 1).last ?? "")
        if let path = store.avatars.first(where: { $0["id"].stringValue == id })?["image_url"].nonemptyString,
           let url = resolvedURL(path, baseURL: store.baseURL), let data = await LocalStore.download(url), let picture = UIImage(data: data) {
            items.insert(picture, at: 0)
        }
        shareItems = items
    }
}

// MARK: - Rows

private struct TaskRow: View {
    let task: ActivityTask
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle().fill(Color.secondary.opacity(task.status == .failed ? 0.3 : 0.5))
                if task.status == .running { ProgressView().controlSize(.small).tint(.white) }
                else {
                    Image(systemName: task.jobID != nil ? "clock" : task.status == .failed ? "exclamationmark" : "circle.grid.2x2.fill")
                        .font(.system(size: 14, weight: .semibold)).foregroundStyle(.white)
                }
            }.frame(width: 36, height: 36)
            VStack(alignment: .leading, spacing: 2) {
                Text(task.title).font(.system(size: 16, weight: .semibold)).lineLimit(1)
                Text(task.summary).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(2)
                Text(SheetTime.clock(task.at)).font(.system(size: 13)).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 9).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

private struct RecordRow: View {
    let icon: String
    let title: String
    let detail: String
    let status: String
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: icon).font(.system(size: 17)).frame(width: 26).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 16, weight: .semibold)).lineLimit(1)
                if !detail.isEmpty { Text(detail).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(2) }
                Text(status).font(.system(size: 13)).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 9).contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

private struct JobRow: View {
    let job: JSONValue
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            EmojiIcon(emoji: job["schedule"]["kind"].stringValue == "every" ? "⏱️" : "🗓️", size: 34)
                .overlay(alignment: .bottomTrailing) {
                    if job["enabled"].boolValue {
                        Image(systemName: "clock.fill").font(.system(size: 10)).foregroundStyle(.orange)
                            .background(Circle().fill(CompanionPalette.background).padding(-1.5)).offset(x: 3, y: 3)
                    }
                }
            VStack(alignment: .leading, spacing: 2) {
                Text(job["name"].stringValue).font(.system(size: 16, weight: .semibold)).lineLimit(1)
                Text(Schedules.describe(job)).font(.system(size: 14)).foregroundStyle(.secondary)
                Text(when).font(.system(size: 13)).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 9).contentShape(Rectangle())
        .opacity(job["enabled"].boolValue ? 1 : 0.6)
        .accessibilityElement(children: .combine)
    }
    private var when: String {
        if !job["enabled"].boolValue { return "已暂停" }
        guard let next = job["state"]["next_run_at_ms"].doubleValue else { return "没有下一次" }
        return "下次 " + dayLabel(next)
    }
}

private struct DocumentCard: View {
    let kind: CompanionDocumentSheet.Kind
    let date: Double?
    var open: () -> Void
    var body: some View {
        Button(action: open) {
            ZStack(alignment: .topLeading) {
                LinearGradient(colors: kind == .soul
                               ? [Color(red: 0.50, green: 0.42, blue: 0.33), Color(red: 0.74, green: 0.53, blue: 0.43), Color(red: 0.86, green: 0.64, blue: 0.53)]
                               : [Color(red: 0.07, green: 0.50, blue: 0.56), Color(red: 0.06, green: 0.27, blue: 0.50), Color(red: 0.10, green: 0.42, blue: 0.72)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                RadialGradient(colors: [.white.opacity(0.22), .clear], center: .bottomLeading, startRadius: 4, endRadius: 140)
                VStack(alignment: .leading, spacing: 2) {
                    Text(kind == .soul ? "SOUL" : "记忆").font(.system(size: 20, weight: .heavy)).foregroundStyle(.white)
                    Text(kind == .soul ? "它是谁、怎么说话" : "它记得的你").font(.system(size: 10)).foregroundStyle(.white.opacity(0.72))
                    Spacer(minLength: 0)
                    HStack {
                        Text(date.map(SheetTime.dotted) ?? "").font(.system(size: 11, design: .monospaced)).foregroundStyle(.white.opacity(0.75))
                        Spacer()
                        Image(systemName: kind == .soul ? "heart.fill" : "bubble.left.fill").font(.system(size: 17)).foregroundStyle(.white.opacity(0.85))
                    }
                }.padding(12)
            }
            .aspectRatio(1.22, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 16))
            .shadow(color: .black.opacity(0.12), radius: 6, y: 3)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(kind == .soul ? "打开 SOUL.md" : "打开 MEMORY.md")
    }
}

private struct PaneHeading: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 16, weight: .semibold)).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 4).padding(.bottom, 6)
    }
}

private struct EmptyLine: View {
    /// nil while loading.
    let text: String?
    var body: some View {
        Group {
            if let text { Text(text).font(.system(size: 14)).foregroundStyle(.secondary) }
            else { ProgressView().frame(maxWidth: .infinity) }
        }.padding(.vertical, 18).frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// × and share at the top of the page, and × on its sheets: a round light button.
struct RoundIconButton: View {
    let icon: String
    let label: String
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 15, weight: .semibold)).foregroundStyle(.primary)
                .frame(width: 40, height: 40).background(CompanionPalette.surface, in: Circle())
                .shadow(color: .black.opacity(0.10), radius: 5, y: 2)
        }.buttonStyle(.plain).accessibilityLabel(label)
    }
}
