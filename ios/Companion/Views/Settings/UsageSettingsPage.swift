import SwiftUI

@MainActor
struct UsageSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/usage")
    @State private var range = "today"
    private var period: JSONValue { resource.value[range] }
    private var total: Double { period["by_source"].object.values.reduce(0) { $0 + ($1.doubleValue ?? 0) } }
    var body: some View {
        Form {
            Section {
                Text("每次对话都会把说明、工具和之前的聊天一起发给模型；相同的开头会命中缓存。费用按「模型」里的价格估算，图片和视频按张、按秒算。")
                    .font(.subheadline).foregroundStyle(.secondary)
                Picker("时间范围", selection: $range) { Text("今天").tag("today"); Text("近 7 天").tag("week") }.pickerStyle(.segmented)
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            if resource.value != .null && total == 0 && period["media"].array.isEmpty {
                Text("这段时间还没有用量。").foregroundStyle(.secondary)
            } else if resource.value != .null {
                Section {
                    Text("\(SettingsDecoding.tokens(total)) token").font(.title.bold())
                    if let input = period["input_tokens"].doubleValue, input > 0 {
                        let share = Self.clampedPercent(part: period["cached_tokens"].doubleValue ?? 0, whole: input)
                        Text("其中 \(share)% 是缓存命中").font(.subheadline)
                    }
                    Text(SettingsDecoding.cost(period["cost"])).foregroundStyle(.secondary)
                    ForEach(period["by_source"].object.keys.sorted(), id: \.self) { key in
                        let value = period["by_source"][key].doubleValue ?? 0
                        VStack(alignment: .leading, spacing: 5) {
                            LabeledContent(sourceName(key), value: SettingsDecoding.tokens(value))
                            ProgressView(value: Self.clampedProgress(value: value, total: total))
                        }
                    }
                }
                Section("按模型") {
                    ForEach(Array(period["by_model"].array.enumerated()), id: \.offset) { _, row in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack { Text(row["preset"].nonemptyString ?? row["model"].stringValue); Spacer(); Text(row["subscription"].boolValue ? "会员额度" : SettingsDecoding.cost(row["cost"])) }
                            Text("\(row["calls"].intValue ?? 0) 次 · 输入 \(SettingsDecoding.tokens(row["input_tokens"].doubleValue ?? 0)) · 输出 \(SettingsDecoding.tokens(row["output_tokens"].doubleValue ?? 0))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if !period["media"].array.isEmpty {
                    Section("图像和视频") {
                        ForEach(Array(period["media"].array.enumerated()), id: \.offset) { _, row in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(row["name"].stringValue)
                                Text("\(row["count"].intValue ?? 0) \(row["kind"].stringValue == "image" ? "张" : "段视频") · \(row["subscription"].boolValue ? "会员额度" : SettingsDecoding.cost(row["cost"]))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }.settingsStyle("用量").task { await resource.load(store) }.refreshable { await resource.load(store) }
    }
    private func sourceName(_ key: String) -> String { ["user": "聊天", "cron": "后台任务", "router": "挑模型", "dream": "整理记忆", "system": "系统", "api": "API"][key] ?? key }

    nonisolated static func clampedProgress(value: Double, total: Double) -> Double {
        guard total > 0, value.isFinite, total.isFinite else { return 0 }
        return min(1, max(0, value / total))
    }

    nonisolated static func clampedPercent(part: Double, whole: Double) -> Int {
        guard whole > 0, part.isFinite, whole.isFinite else { return 0 }
        return Int(min(100, max(0, (part / whole) * 100)))
    }
}
