import SwiftUI

/// Per-chat model preset / reasoning effort switcher + live context window occupancy & `/compact` action.
@MainActor
struct ChatSessionControlsSheet: View {
    let chatID: String
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedPreset = ""
    @State private var selectedEffort = "default"
    @State private var savingModel = false
    @State private var loadingContext = false
    @State private var contextStatus: ChatContextStatus?
    @State private var contextError: String?
    @State private var notice: String?

    private static let effortOptions: [(value: String, label: String)] = [
        ("default", "默认"),
        ("none", "不思考"),
        ("low", "低"),
        ("medium", "中"),
        ("high", "高"),
        ("max", "最高"),
    ]

    private var summary: ChatSummary? { store.sessionSummary(for: chatID) }

    private var presetNames: [String] {
        var list = store.availableModelPresets
        if !selectedPreset.isEmpty && !list.contains(selectedPreset) {
            list.insert(selectedPreset, at: 0)
        }
        return list
    }

    private var globalDefaultLabel: String {
        store.defaultModelPreset.isEmpty ? "跟随全局默认" : "跟随全局默认（\(store.defaultModelPreset)）"
    }

    var body: some View {
        NavigationStack {
            Form {
                if let notice {
                    Section {
                        Label(notice, systemImage: "checkmark.circle.fill")
                            .font(.subheadline)
                            .foregroundStyle(CompanionPalette.primary)
                    }
                }
                presetSection
                effortSection
                contextSection
            }
            .scrollContentBackground(.hidden)
            .background(CompanionPalette.surface)
            .navigationTitle("本聊算力与上下文")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
        .task(id: chatID) {
            syncFromStore()
            await loadInitialData()
        }
    }

    @ViewBuilder
    private var presetSection: some View {
        Section {
            presetRow(title: globalDefaultLabel, subtitle: "随全局配置自动切换", value: "")
            ForEach(presetNames, id: \.self) { name in
                presetRow(title: name, subtitle: nil, value: name)
            }
        } header: {
            Text("当前会话算力预设")
        } footer: {
            Text("仅影响本条会话，切换后立即生效且重启保留；不改变其他会话。")
        }
    }

    private func presetRow(title: String, subtitle: String?, value: String) -> some View {
        let active = selectedPreset == value
        return Button {
            guard !savingModel, selectedPreset != value else { return }
            let previous = selectedPreset
            selectedPreset = value
            Task { await applyModelChange(fallbackPreset: previous, fallbackEffort: selectedEffort) }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).foregroundStyle(.primary)
                    if let subtitle {
                        Text(subtitle).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 8)
                if savingModel && active {
                    ProgressView().controlSize(.small)
                } else if active {
                    Image(systemName: "checkmark")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(CompanionPalette.primary)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(savingModel || !store.connected)
    }

    @ViewBuilder
    private var effortSection: some View {
        Section {
            Picker("本聊思考深度", selection: Binding(
                get: { selectedEffort },
                set: { next in
                    guard !savingModel, next != selectedEffort else { return }
                    let previous = selectedEffort
                    selectedEffort = next
                    Task { await applyModelChange(fallbackPreset: selectedPreset, fallbackEffort: previous) }
                }
            )) {
                ForEach(Self.effortOptions, id: \.value) { option in
                    Text(option.label).tag(option.value)
                }
            }
            .pickerStyle(.segmented)
            .disabled(savingModel || !store.connected)
        } header: {
            Text("本聊思考深度")
        } footer: {
            Text("「默认」沿用所选预设本身的思考深度；不支持思考的模型会自动忽略。")
        }
    }

    @ViewBuilder
    private var contextSection: some View {
        Section {
            if loadingContext && contextStatus == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在读取上下文水位…").font(.subheadline).foregroundStyle(.secondary)
                }
            } else if let status = contextStatus, status.contextWindow > 0 {
                contextGaugeContent(status)
            } else if let contextError {
                VStack(alignment: .leading, spacing: 6) {
                    Text(contextError).font(.footnote).foregroundStyle(.secondary)
                    Button("重新读取水位") { Task { await refreshContext() } }
                        .font(.footnote.weight(.medium))
                }
            } else {
                Text("当前会话还没有上下文占用记录。").font(.subheadline).foregroundStyle(.secondary)
            }
        } header: {
            HStack {
                Text("上下文窗口水位")
                Spacer()
                Button {
                    Task { await refreshContext() }
                } label: {
                    Image(systemName: "arrow.clockwise").font(.caption.weight(.semibold))
                }
                .buttonStyle(.plain)
                .disabled(loadingContext || !store.connected)
                .accessibilityLabel("刷新上下文水位")
            }
        }
    }

    @ViewBuilder
    private func contextGaugeContent(_ status: ChatContextStatus) -> some View {
        let tintColor: Color = status.isHighWater ? .red : (status.isElevated ? .orange : CompanionPalette.primary)
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(status.percent)%")
                    .font(.title2.bold().monospacedDigit())
                    .foregroundStyle(tintColor)
                Spacer()
                Text("\(Self.formatCompactTokens(status.estimatedTokens)) / \(Self.formatCompactTokens(status.contextWindow)) token")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            ProgressView(value: status.clampedRatio)
                .tint(tintColor)

            if let model = status.model, !model.isEmpty {
                LabeledContent("当前生效后端", value: model)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if status.hasSummaryCheckpoint == true {
                Label("已存在历史摘要检查点", systemImage: "bookmark.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if status.isHighWater {
                Text("当前上下文水位偏高，建议立即压缩以释放窗口并节省 Token。")
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Button {
                Haptics.selection()
                Task {
                    await store.sendControl("/compact", chatID: chatID)
                    dismiss()
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.down.right.and.arrow.up.left")
                    Text(status.isHighWater ? "一键压缩上下文 (/compact)" : "手动压缩上下文 (/compact)")
                        .font(.subheadline.weight(.semibold))
                }
                .frame(maxWidth: .infinity, minHeight: 38)
                .foregroundStyle(status.isHighWater ? CompanionPalette.primaryText : CompanionPalette.primary)
                .background(
                    status.isHighWater ? CompanionPalette.primary : CompanionPalette.primary.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: 12)
                )
            }
            .buttonStyle(.plain)
            .disabled(!store.connected || store.running || status.estimatedTokens == 0)
            .accessibilityLabel("压缩当前上下文")
        }
        .padding(.vertical, 4)
    }

    private func syncFromStore() {
        selectedPreset = summary?.modelPreset ?? ""
        selectedEffort = summary?.reasoningEffort ?? "default"
    }

    private func loadInitialData() async {
        if store.availableModelPresets.isEmpty {
            await store.refreshModelStatus()
        }
        await refreshContext()
    }

    private func refreshContext() async {
        guard !chatID.isEmpty, store.connected else { return }
        loadingContext = true
        contextError = nil
        defer { loadingContext = false }
        do {
            let status = try await store.fetchChatContext(chatId: chatID)
            contextStatus = status
            if let preset = status.modelPreset { selectedPreset = preset }
            if let effort = status.reasoningEffort { selectedEffort = effort }
        } catch is CancellationError { }
        catch {
            contextError = error.localizedDescription
        }
    }

    private func applyModelChange(fallbackPreset: String, fallbackEffort: String) async {
        savingModel = true
        notice = nil
        defer { savingModel = false }
        let ok = await store.setChatModel(
            chatId: chatID,
            preset: selectedPreset.isEmpty ? nil : selectedPreset,
            effort: selectedEffort == "default" ? nil : selectedEffort
        )
        if ok {
            Haptics.selection()
            notice = selectedPreset.isEmpty ? "已切回全局默认预设" : "已切换为「\(selectedPreset)」"
            await refreshContext()
        } else {
            selectedPreset = fallbackPreset
            selectedEffort = fallbackEffort
        }
    }

    nonisolated static func formatCompactTokens(_ count: Int) -> String {
        if count >= 1_000_000 { return String(format: "%.1fM", Double(count) / 1_000_000) }
        if count >= 1_000 { return String(format: "%.1fK", Double(count) / 1_000) }
        return "\(count)"
    }
}
