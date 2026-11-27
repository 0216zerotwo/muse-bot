import Charts
import SwiftUI

@MainActor
struct UsageSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/usage")
    @State private var range = "today"

    private var period: JSONValue { resource.value[range] }
    private var total: Double { period["by_source"].object.values.reduce(0) { $0 + ($1.doubleValue ?? 0) } }
    private var summary: UsageSummaryPayload { UsageSummaryPayload(json: resource.value) }
    private var dailyBuckets: [DailyUsageBucket] { summary.daily ?? [] }

    var body: some View {
        Form {
            introSection
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            if resource.value != .null {
                if summary.cacheHitRate != nil || summary.cacheSavedCost != nil {
                    UsageCacheSavingsSection(
                        cacheHitRate: summary.cacheHitRate,
                        cacheSavedCost: summary.cacheSavedCost
                    )
                }
                if !dailyBuckets.isEmpty {
                    DailyUsageChartSection(buckets: dailyBuckets)
                }
                periodDetailSections
            }
        }
        .settingsStyle("用量")
        .task { await resource.load(store) }
        .refreshable { await resource.load(store) }
    }

    private var introSection: some View {
        Section {
            Text("每次对话都会把说明、工具和之前的聊天一起发给模型；相同的开头会命中缓存。费用按「模型」里的价格估算，图片和视频按张、按秒算。")
                .font(.subheadline).foregroundStyle(.secondary)
            Picker("时间范围", selection: $range) {
                Text("今天").tag("today")
                Text("近 7 天").tag("week")
            }
            .pickerStyle(.segmented)
        }
    }

    @ViewBuilder
    private var periodDetailSections: some View {
        if total == 0 && period["media"].array.isEmpty {
            Text("这段时间还没有用量。").foregroundStyle(.secondary)
        } else {
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
                        HStack {
                            Text(row["preset"].nonemptyString ?? row["model"].stringValue)
                            Spacer()
                            Text(row["subscription"].boolValue ? "会员额度" : SettingsDecoding.cost(row["cost"]))
                        }
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
    }

    private func sourceName(_ key: String) -> String {
        ["user": "聊天", "cron": "后台任务", "router": "挑模型", "dream": "整理记忆", "system": "系统", "api": "API"][key] ?? key
    }

    nonisolated static func clampedProgress(value: Double, total: Double) -> Double {
        guard total > 0, value.isFinite, total.isFinite else { return 0 }
        return min(1, max(0, value / total))
    }

    nonisolated static func clampedPercent(part: Double, whole: Double) -> Int {
        guard whole > 0, part.isFinite, whole.isFinite else { return 0 }
        return Int(min(100, max(0, (part / whole) * 100)))
    }
}

/// Cache hit rate and estimated cost savings card powered by `cache_hit_rate` & `cache_saved_cost`.
@MainActor
private struct UsageCacheSavingsSection: View {
    let cacheHitRate: Double?
    let cacheSavedCost: Double?

    private var clampedRate: Double {
        guard let rate = cacheHitRate, rate.isFinite else { return 0 }
        return min(1, max(0, rate))
    }

    private var rateLabel: String {
        String(format: "%.1f%%", clampedRate * 100)
    }

    private var savedLabel: String {
        guard let saved = cacheSavedCost, saved.isFinite else { return "—" }
        if saved <= 0 { return "¥0.00" }
        return saved < 0.01 ? "不到 1 分钱" : String(format: "¥%.2f", saved)
    }

    var body: some View {
        Section("近 7 天 Prompt Cache 省钱概览") {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    Label("缓存命中率", systemImage: "bolt.shield.fill")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                    Text(cacheHitRate == nil ? "—" : rateLabel)
                        .font(.title2.bold().monospacedDigit())
                        .foregroundStyle(Color.teal)
                    ProgressView(value: clampedRate)
                        .tint(.teal)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.teal.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))

                VStack(alignment: .leading, spacing: 6) {
                    Label("累计节省估算", systemImage: "yensign.circle.fill")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                    Text(savedLabel)
                        .font(.title2.bold().monospacedDigit())
                        .foregroundStyle(CompanionPalette.primary)
                    Text("按缓存折扣与单价估算")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(CompanionPalette.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
            }
            .listRowInsets(EdgeInsets(top: 10, leading: 12, bottom: 10, trailing: 12))
        }
    }
}

/// Native iOS 17 Swift Charts 14-day stacked token trend bar chart.
@MainActor
private struct DailyUsageChartSection: View {
    private struct ChartSegment: Identifiable {
        let id: String
        let date: String
        let series: String
        let tokens: Int
    }

    let buckets: [DailyUsageBucket]
    @State private var selectedDate: String?

    private var hasAnyTokens: Bool {
        buckets.contains { $0.totalTokens > 0 }
    }

    private var segments: [ChartSegment] {
        var rows: [ChartSegment] = []
        rows.reserveCapacity(buckets.count * 3)
        for day in buckets {
            rows.append(ChartSegment(id: "\(day.date)-fresh", date: day.date, series: "新输入", tokens: day.freshInputTokens))
            rows.append(ChartSegment(id: "\(day.date)-cached", date: day.date, series: "缓存命中", tokens: day.cachedTokens))
            rows.append(ChartSegment(id: "\(day.date)-out", date: day.date, series: "输出", tokens: day.outputTokens))
        }
        return rows
    }

    private var axisDates: [String] {
        let count = buckets.count
        guard count > 4 else { return buckets.map(\.date) }
        let step = max(1, count / 4)
        var picked: [String] = []
        for idx in stride(from: 0, to: count, by: step) {
            picked.append(buckets[idx].date)
        }
        if let last = buckets.last?.date, picked.last != last {
            picked.append(last)
        }
        return picked
    }

    private var selectedBucket: DailyUsageBucket? {
        if let selectedDate, let match = buckets.first(where: { $0.date == selectedDate }) {
            return match
        }
        return buckets.last(where: { $0.totalTokens > 0 }) ?? buckets.last
    }

    var body: some View {
        Section("近 \(buckets.count) 天 Token 趋势") {
            if !hasAnyTokens {
                Text("近 \(buckets.count) 天还没有 Token 消耗记录。")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    stackedChart
                    if let active = selectedBucket {
                        dayDetailBanner(active)
                    }
                }
                .padding(.vertical, 6)
            }
        }
    }

    private var stackedChart: some View {
        Chart(segments) { seg in
            BarMark(
                x: .value("日期", seg.date),
                y: .value("Token", seg.tokens)
            )
            .foregroundStyle(by: .value("类型", seg.series))
            .cornerRadius(2)
        }
        .chartForegroundStyleScale([
            "新输入": CompanionPalette.primary,
            "缓存命中": Color.teal,
            "输出": Color.orange,
        ])
        .chartXSelection(value: $selectedDate)
        .chartXAxis {
            AxisMarks(values: axisDates) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let raw = value.as(String.self) {
                        Text(Self.shortDate(raw)).font(.caption2)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let num = value.as(Double.self) {
                        Text(Self.axisTokens(Int(num.rounded()))).font(.caption2)
                    }
                }
            }
        }
        .chartLegend(position: .top, alignment: .leading)
        .frame(height: 196)
    }

    private func dayDetailBanner(_ day: DailyUsageBucket) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(day.date)
                    .font(.caption.weight(.semibold).monospacedDigit())
                if let req = day.requests, req > 0 {
                    Text("\(req) 次请求")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if let cost = day.combinedCost {
                    Text(cost < 0.01 ? "不到 1 分钱" : String(format: "约 ¥%.2f", cost))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(CompanionPalette.primary)
                }
            }
            Text("新输入 \(SettingsDecoding.tokens(Double(day.freshInputTokens))) · 缓存 \(SettingsDecoding.tokens(Double(day.cachedTokens))) · 输出 \(SettingsDecoding.tokens(Double(day.outputTokens)))")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CompanionPalette.elevated.opacity(0.65), in: RoundedRectangle(cornerRadius: 12))
    }

    private static func shortDate(_ iso: String) -> String {
        let parts = iso.split(separator: "-")
        if parts.count == 3, let m = Int(parts[1]), let d = Int(parts[2]) {
            return "\(m)/\(d)"
        }
        return iso
    }

    private static func axisTokens(_ count: Int) -> String {
        if count >= 1_000_000 { return String(format: "%.1fM", Double(count) / 1_000_000) }
        if count >= 1_000 { return "\(count / 1_000)K" }
        return "\(count)"
    }
}
