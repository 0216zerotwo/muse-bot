import SwiftUI

/// One chat model, as the web's ModelDetail: how hard it thinks, how long and how freely it answers,
/// and how automatic choosing should see it. Each change is saved at once.
@MainActor
struct ModelDetailSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    var name: String
    @ObservedObject var resource: SettingsResource
    var vendor: JSONValue?
    @State private var priceIn = ""
    @State private var priceOut = ""
    @State private var checking = false
    @State private var confirmRemove = false
    @FocusState private var editingPrice: Bool

    private static let lengths = [1024, 2048, 4096, 8192, 16384, 32768, 65536]
    private static let styles: [(value: Double, label: String)] = [(0.1, "严谨"), (0.5, "平衡"), (0.9, "发散")]
    private static let tiers: [(value: String, label: String)] = [("cheap", "便宜快速"), ("balanced", "均衡"), ("strong", "最强")]

    private var preset: JSONValue { resource.value["presets"].array.first { $0["name"].stringValue == name } ?? .null }
    private var info: JSONValue { resource.value["settings"]["models"][name] }
    private var health: JSONValue { resource.value["health"][name] }

    var body: some View {
        Form {
            if preset == .null {
                Text(resource.value == .null ? "正在读取…" : "这个模型已经不在了。").foregroundStyle(.secondary)
            } else {
                Section {
                    HStack(spacing: 12) {
                        if let vendor { VendorLogo(vendor: vendor, size: 28) } else { HealthDot(health: health) }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(preset["model"].stringValue)
                            Text(vendor?["name"].nonemptyString ?? preset["provider"].stringValue).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(checking ? "检查中…" : "检查") { check() }
                            .font(.footnote.weight(.medium)).foregroundStyle(CompanionPalette.primary)
                            .buttonStyle(.plain).disabled(checking || resource.busy)
                    }
                } footer: {
                    if health["ok"] == .bool(false) { Text("用不了：\(health["error"].nonemptyString ?? "出错了")。自动选择会先避开它。") }
                    else if health != .null { Text("\(Self.checkedLabel(health["checked_at"].doubleValue))检查过，可以用。") }
                }

                Section {
                    Picker("思考强度", selection: Binding(get: { preset["reasoning_effort"].nonemptyString ?? "default" },
                                                      set: { limits(["reasoning_effort": $0 == "default" ? .null : .string($0)]) })) {
                        ForEach(ModelsSettingsPage.efforts, id: \.value) { Text($0.label).tag($0.value) }
                    }.pickerStyle(.segmented).disabled(resource.busy)
                } header: { Text("思考强度") } footer: {
                    Text("想得越多答得越准，但更慢、更费 token；「默认」按服务商的设置，不支持思考的模型会忽略。")
                }

                Section("回答") {
                    let length = preset["max_tokens"].intValue ?? 8192
                    Picker("单次最多", selection: Binding(get: { length }, set: { limits(["max_tokens": .number(Double($0))]) })) {
                        ForEach(Array(Set(Self.lengths + [length])).sorted(), id: \.self) { Text("\(Self.tokens($0)) token").tag($0) }
                    }.disabled(resource.busy)
                    let temperature = preset["temperature"].doubleValue ?? 0.1
                    let style = Self.styles.first { abs($0.value - temperature) < 0.05 }?.value ?? temperature
                    Picker("风格", selection: Binding(get: { style }, set: { limits(["temperature": .number($0)]) })) {
                        ForEach(Self.styles, id: \.value) { Text($0.label).tag($0.value) }
                        if !Self.styles.contains(where: { $0.value == style }) { Text("自定义 \(style, specifier: "%.1f")").tag(style) }
                    }.disabled(resource.busy)
                }

                Section("自动选择时") {
                    Picker("档位", selection: Binding(get: { info["tier"].nonemptyString ?? "balanced" }, set: { route(["tier": .string($0)]) })) {
                        ForEach(Self.tiers, id: \.value) { Text($0.label).tag($0.value) }
                    }.pickerStyle(.segmented).disabled(resource.busy)
                    Toggle(isOn: Binding(get: { info["vision"].boolValue }, set: { route(["vision": .bool($0)]) })) {
                        Label("能看图", systemImage: "eye")
                    }.disabled(resource.busy)
                    HStack(spacing: 10) {
                        priceField("输入价格", text: $priceIn)
                        priceField("输出价格", text: $priceOut)
                    }
                }

                if resource.value["presets"].array.count > 1 {
                    Section {
                        Button(role: .destructive) {
                            if confirmRemove { Task { if await resource.change(store, "companion.models.remove", ["name": .string(name)]) { dismiss() } } }
                            else { confirmRemove = true }
                        } label: { Label(confirmRemove ? "再点一次，确认删除" : "删除这个模型", systemImage: "trash") }
                            .disabled(resource.busy)
                    }
                }
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
        }
        .settingsStyle(name)
        .onAppear(perform: readPrices)
        .onChange(of: info) { _, _ in if !editingPrice { readPrices() } }
        .onChange(of: editingPrice) { _, editing in if !editing { savePrices() } }
    }

    private func priceField(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(title)（元/百万 token）").font(.caption).foregroundStyle(.secondary)
            TextField("可不填", text: text).keyboardType(.decimalPad).focused($editingPrice)
                .padding(.horizontal, 12).frame(minHeight: 40)
                .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
        }.frame(maxWidth: .infinity)
    }

    private func readPrices() {
        priceIn = info["price_in"].doubleValue.map { Pricing.yuan($0) } ?? ""
        priceOut = info["price_out"].doubleValue.map { Pricing.yuan($0) } ?? ""
    }

    private func savePrices() {
        func value(_ text: String) -> JSONValue? {
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { return .null }
            guard let number = Double(trimmed), number.isFinite, number >= 0 else { return nil }
            return .number(number)
        }
        guard let input = value(priceIn), let output = value(priceOut) else { resource.error = "价格要填大于或等于 0 的数字。"; return }
        guard input != info["price_in"] || output != info["price_out"] else { return }
        route(["price_in": input, "price_out": output])
    }

    /// Thinking, length, style: the preset's own limits.
    private func limits(_ changes: [String: JSONValue]) {
        Task { await resource.change(store, "companion.models.update", ["changes": .object(["presets": .object([name: .object(changes)])])]) }
    }
    /// How automatic choosing sees it: the model's info, all fields (the server replaces it whole).
    private func route(_ changes: [String: JSONValue]) {
        var next = info.object
        if next.isEmpty { next = ["description": .string(""), "tier": .string("balanced"), "vision": .bool(false), "price_in": .null, "price_out": .null] }
        changes.forEach { next[$0.key] = $0.value }
        Task { await resource.change(store, "companion.models.update", ["changes": .object(["models": .object([name: .object(next)])])]) }
    }
    private func check() {
        checking = true
        Task { await resource.change(store, "companion.models.check", ["names": .array([.string(name)])]); checking = false }
    }

    static func tokens(_ count: Int) -> String { count >= 1024 ? "\(Int((Double(count) / 1024).rounded()))K" : "\(count)" }
    /// 「今天 14:05」「昨天 09:12」「10月3日 08:00」, as the web's dayLabel.
    static func checkedLabel(_ milliseconds: Double?) -> String {
        guard let milliseconds else { return "" }
        let date = Date(timeIntervalSince1970: milliseconds / 1000)
        let time = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
        if Calendar.current.isDateInToday(date) { return "今天 \(time) " }
        if Calendar.current.isDateInYesterday(date) { return "昨天 \(time) " }
        let parts = Calendar.current.dateComponents([.month, .day], from: date)
        return "\(parts.month ?? 0)月\(parts.day ?? 0)日 \(time) "
    }
}
