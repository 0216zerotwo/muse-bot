import SwiftUI

/// 模型, as the web's ModelPanel: three kinds. 对话 — who answers (picked automatically or one fixed
/// model), the chat models with their health, limits. 图像 / 视频 — the model in use (one at a
/// time), its price, 检测价格, add and delete.
@MainActor
struct ModelsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/models")
    @StateObject private var catalog = SettingsResource("/api/webui/companion/model-catalog")
    @State private var kind = "text"
    @State private var checking = false
    @State private var pricing: String?
    @State private var confirmDelete: String?
    @State private var adding = false

    private var vendors: [String: JSONValue] {
        Dictionary(catalog.value["vendors"].array.map { ($0["id"].stringValue, $0) }, uniquingKeysWith: { first, _ in first })
    }
    private var presets: [JSONValue] { SettingsDecoding.rows(resource.value, key: "presets", id: "name") }
    private var names: [String] { presets.compactMap { $0["name"].nonemptyString } }
    private var automatic: Bool { resource.value["settings"]["enabled"].boolValue }
    private var defaultName: String { resource.value["default"].stringValue }
    static let kindLabels = ["text": "对话", "image": "图像", "video": "视频"]

    var body: some View {
        Form {
            Section {
                Picker("模型种类", selection: $kind) {
                    ForEach(["text", "image", "video"], id: \.self) { value in
                        let count = value == "text" ? names.count : resource.value["media"][value]["models"].array.count
                        Text(count > 0 ? "\(Self.kindLabels[value] ?? "") \(count)" : Self.kindLabels[value] ?? "").tag(value)
                    }
                }.pickerStyle(.segmented).listRowBackground(Color.clear).listRowInsets(EdgeInsets())
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            if resource.value != .null {
                if kind == "text" { textSections } else { mediaSections(kind) }
            }
        }
        .settingsStyle("模型")
        .task { await resource.load(store) }
        .task { await catalog.load(store) }
        .refreshable { await resource.load(store) }
        .onChange(of: kind) { _, _ in resource.error = nil; confirmDelete = nil }  // an error belongs to its kind
        .sheet(isPresented: $adding) {
            AddModelSettingsPage(kind: kind, resource: resource)
                .presentationDetents([.large]).presentationDragIndicator(.visible).tint(CompanionPalette.primary)
        }
    }

    // MARK: 对话

    @ViewBuilder private var textSections: some View {
        let enough = names.count >= 2
        Section {
            Picker("回答用的模型", selection: Binding(get: { automatic }, set: { save(["enabled": .bool($0)]) })) {
                Text("自动选择").tag(true); Text("固定一个").tag(false)
            }.pickerStyle(.segmented).disabled(resource.busy || !enough)
            if automatic {
                Picker(selection: Binding(get: { defaultName }, set: { save(["default": .string($0)]) })) {
                    ForEach(names, id: \.self) { Text($0).tag($0) }
                } label: { Label("日常用", systemImage: "wand.and.stars") }.disabled(resource.busy)
            } else {
                Picker(selection: Binding(get: { resource.value["settings"]["fixed"].stringValue },
                                          set: { save(["fixed": $0.isEmpty ? .null : .string($0)]) })) {
                    Text(defaultName.isEmpty ? "默认" : "默认（\(defaultName)）").tag("")
                    ForEach(names, id: \.self) { Text($0).tag($0) }
                } label: { Label("一直使用", systemImage: "sparkles") }.disabled(resource.busy)
            }
        } footer: {
            Text(automatic ? "聊天一直用日常模型（同一段对话不来回换，回得更快也更省）；后台的小事交给最便宜的，日常模型看不了图时，先请能看图的模型看。"
                 : enough ? "每条消息都用这个模型回答。" : "至少有两个模型才能自动选择；先在下面多添加几个。")
        }

        Section {
            ForEach(presets, id: \.selfID) { model in
                let name = model["name"].stringValue
                let health = resource.value["health"][name]
                let vendor = vendors[model["vendor"].stringValue]
                NavigationLink { ModelDetailSettingsPage(name: name, resource: resource, vendor: vendor) } label: {
                    HStack(spacing: 12) {
                        if let vendor { VendorLogo(vendor: vendor, size: 28) } else { HealthDot(health: health) }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(name)
                            Text(detail(model, vendor: vendor, health: health)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        if vendor != nil { HealthDot(health: health) }
                    }
                }
            }
            Button { adding = true } label: { Label("添加对话模型", systemImage: "plus") }.foregroundStyle(.primary)
        } header: {
            HStack {
                Text("对话模型")
                Spacer()
                Button(checking ? "检查中…" : "检查能否使用") { checkAll() }
                    .font(.footnote.weight(.medium)).foregroundStyle(CompanionPalette.primary)
                    .textCase(nil).disabled(checking || resource.busy)
            }
        }

        Section {
            let steps = resource.value["max_tool_iterations"].intValue ?? 20
            Picker(selection: Binding(get: { steps }, set: { save(["max_tool_iterations": .number(Double($0))]) })) {
                ForEach(Array(Set([20, 50, 100, 200, 500, steps])).sorted(), id: \.self) { Text("\($0) 步").tag($0) }
            } label: { Label("每次最多做几步", systemImage: "figure.walk") }.disabled(resource.busy)
            if names.count > 1 {
                let fallbacks = resource.value["fallbacks"].array.compactMap(\.nonemptyString)
                NavigationLink { FallbackModelsSettingsPage(resource: resource) } label: {
                    HStack {
                        Label("出错时换用", systemImage: "lifepreserver")
                        Spacer()
                        Text(fallbacks.isEmpty ? "不换" : fallbacks.joined(separator: "、")).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
        } header: { Text("限制") } footer: { Text("一次回复里最多调用几次工具，防止任务一直转、花掉太多额度。") }
    }

    /// 「DeepSeek · 看图 · 思考最高 · 响应 1.5 秒」, as the web rows say it.
    private func detail(_ model: JSONValue, vendor: JSONValue?, health: JSONValue) -> String {
        let name = model["name"].stringValue
        var parts: [String] = []
        parts.append(health["ok"] == .bool(false) ? "用不了" : (vendor?["name"].nonemptyString ?? model["model"].stringValue))
        if resource.value["settings"]["models"][name]["vision"].boolValue { parts.append("看图") }
        parts.append("思考" + Self.effortLabel(model["reasoning_effort"].string))
        if health["ok"].boolValue, let ms = health["latency_ms"].doubleValue, ms > 0 {
            parts.append(String(format: "响应 %.1f 秒", ms / 1000))
        }
        return parts.joined(separator: " · ")
    }

    static let efforts: [(value: String, label: String)] = [("default", "默认"), ("none", "关闭"), ("low", "低"), ("medium", "中"), ("high", "高"), ("max", "最高")]
    static func effortLabel(_ effort: String?) -> String {
        guard let effort else { return "默认" }
        return efforts.first { $0.value == effort }?.label ?? effort
    }

    // MARK: 图像 / 视频

    @ViewBuilder private func mediaSections(_ kind: String) -> some View {
        let media = resource.value["media"][kind]
        let models = SettingsDecoding.rows(media, key: "models", id: "name")
        let label = Self.kindLabels[kind] ?? ""
        if models.isEmpty {
            Section {
                VStack(spacing: 10) {
                    Image(systemName: kind == "image" ? "photo" : "film").font(.title2)
                        .frame(width: 48, height: 48).background(CompanionPalette.elevated, in: RoundedRectangle(cornerRadius: 16))
                    Text(kind == "image" ? "还没有图像模型" : "还没有视频模型").font(.headline)
                    Text(kind == "image" ? "添加一个，伙伴就能画图、给自己换形象。" : "添加一个，伙伴就能把想法做成短视频。")
                        .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("添加\(label)模型") { adding = true }
                        .font(.headline).padding(.horizontal, 24).frame(minHeight: 44)
                        .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText)
                        .buttonStyle(.plain).padding(.top, 4)
                }.frame(maxWidth: .infinity).padding(.vertical, 18)
            }
        } else {
            Section {
                ForEach(models, id: \.selfID) { model in mediaRow(model, kind: kind, active: media["active"].stringValue) }
            } footer: { Text("\(kind == "image" ? "画图时用" : "做视频时用")打勾的那个；点另一个就换成它。") }
            Section {
                Button { adding = true } label: { Label("添加\(label)模型", systemImage: "plus") }.foregroundStyle(.primary)
            }
        }
    }

    private func mediaRow(_ model: JSONValue, kind: String, active: String) -> some View {
        let name = model["name"].stringValue
        let vendorID = model["vendor"].stringValue
        let vendor = vendors[vendorID]
        let inUse = name == active
        let detail = [vendor?["name"].nonemptyString, model["model"].stringValue != name ? model["model"].nonemptyString : nil,
                      Pricing.line(model["price"])].compactMap { $0 }.joined(separator: " · ")
        // A subscription (会员账号登录) has no price list to read: no 检测价格 for it.
        let canPrice = Pricing.summary(model["price"]) == nil && !vendorID.isEmpty && vendor != nil && vendor?["login"].nonemptyString == nil
        return HStack(spacing: 12) {
            Button {
                if !inUse && !resource.busy { save(["media_active": .object([kind: .string(name)])]) }
            } label: {
                HStack(spacing: 12) {
                    VendorLogo(vendor: vendor ?? .object(["name": .string(name)]), size: 28)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(name).foregroundStyle(.primary)
                        if !detail.isEmpty { Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                    }
                    Spacer(minLength: 4)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            if canPrice {
                Button(pricing == vendorID ? "检测中…" : "检测价格") { checkPrice(vendorID, kind: kind) }
                    .font(.footnote).foregroundStyle(CompanionPalette.primary).buttonStyle(.plain)
                    .disabled(pricing != nil || resource.busy)
            }
            if inUse {
                Image(systemName: "checkmark").font(.footnote.weight(.semibold)).foregroundStyle(CompanionPalette.primary).accessibilityLabel("正在用")
            } else if !canPrice {
                Button(confirmDelete == name ? "确认删除" : "删除") {
                    if confirmDelete == name { confirmDelete = nil; Task { await resource.change(store, "companion.models.remove", ["name": .string(name)]) } }
                    else { confirmDelete = name }
                }.font(.footnote).foregroundStyle(.red).buttonStyle(.plain).disabled(resource.busy)
            }
        }
    }

    // MARK: actions

    private func save(_ changes: [String: JSONValue]) { Task { await resource.change(store, "companion.models.update", ["changes": .object(changes)]) } }
    private func checkAll() {
        checking = true
        Task { await resource.change(store, "companion.models.check", [:]); checking = false }
    }
    /// 检测价格: the company's platform read now, then the models reloaded with the price found.
    private func checkPrice(_ vendor: String, kind: String) {
        pricing = vendor; resource.error = nil
        Task {
            do {
                _ = try await store.changeResource("companion.models.check", payload: ["vendor": .string(vendor), "kind": .string(kind)])
                await resource.load(store)
            } catch { resource.error = "检测没成功：\(error.localizedDescription)" }
            pricing = nil
        }
    }
}

/// Green when the last check passed, red when it failed, grey before any check.
struct HealthDot: View {
    let health: JSONValue
    var body: some View {
        Circle().fill(health == .null ? Color.secondary.opacity(0.6) : (health["ok"].boolValue ? Color.green : Color.red))
            .frame(width: 9, height: 9)
            .accessibilityLabel(health == .null ? "还没检查" : (health["ok"].boolValue ? "上次检查可用" : "上次检查失败"))
    }
}

/// A company's mark (its favicon on white), or its initial on its colour when there is none.
struct VendorLogo: View {
    let vendor: JSONValue
    var size: CGFloat = 28
    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.26)
        Group {
            if let url = URL(string: vendor["logo_url"].stringValue), vendor["logo_url"].nonemptyString != nil {
                AsyncImage(url: url) { phase in
                    if let image = phase.image { image.resizable().scaledToFit().padding(3).background(Color.white) }
                    else { initial }
                }
            } else { initial }
        }.frame(width: size, height: size).clipShape(shape).accessibilityHidden(true)
    }
    private var initial: some View {
        let letter = String(vendor["name"].stringValue.trimmingCharacters(in: .whitespaces).prefix(1)).uppercased()
        return ZStack {
            Color(hex: vendor["color"].stringValue) ?? Color.secondary.opacity(0.25)
            if letter.isEmpty { Image(systemName: "sparkles").font(.system(size: size * 0.42)).foregroundStyle(.white) }
            else { Text(letter).font(.system(size: size * 0.46, weight: .semibold)).foregroundStyle(.white) }
        }
    }
}

extension Color {
    /// "#0D0D0D" → a colour; nil for anything else.
    init?(hex: String) {
        let digits = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
        guard digits.count == 6, let value = UInt32(digits, radix: 16) else { return nil }
        self.init(red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255, blue: Double(value & 0xFF) / 255)
    }
}

/// 出错时换用: tap to add or take out; the number is the order they are tried in.
@MainActor
struct FallbackModelsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @ObservedObject var resource: SettingsResource
    var body: some View {
        let fallbacks = resource.value["fallbacks"].array.compactMap(\.nonemptyString)
        Form {
            Section {
                ForEach(SettingsDecoding.rows(resource.value, key: "presets", id: "name"), id: \.selfID) { row in
                    let name = row["name"].stringValue
                    let index = fallbacks.firstIndex(of: name)
                    Button {
                        let next = index != nil ? fallbacks.filter { $0 != name } : fallbacks + [name]
                        Task { await resource.change(store, "companion.models.update", ["changes": .object(["fallbacks": .array(next.map(JSONValue.string))])]) }
                    } label: {
                        HStack(spacing: 12) {
                            Text(index.map { "\($0 + 1)" } ?? "").font(.footnote.weight(.semibold)).frame(width: 20)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(name).foregroundStyle(.primary)
                                if resource.value["health"][name]["ok"] == .bool(false) { Text("用不了").font(.caption).foregroundStyle(.red) }
                            }
                            Spacer()
                            if index != nil { Image(systemName: "checkmark").foregroundStyle(CompanionPalette.primary) }
                        }.contentShape(Rectangle())
                    }.buttonStyle(.plain).disabled(resource.busy)
                }
            } footer: { Text("模型出错或超时，按顺序换下一个接着回答。全部不选时，出错后不换模型。") }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
        }.settingsStyle("出错时换用")
    }
}
