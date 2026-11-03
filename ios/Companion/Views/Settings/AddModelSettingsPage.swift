import SwiftUI
import UIKit

/// 添加模型 for one kind, as the web's AddModelSheet: pick a company (grouped, only those with that
/// kind), sign in or paste its key once, pick a model from its catalog (recommended first, with
/// prices), or type a model id. Shown as a sheet; it closes itself once the model is added.
@MainActor
struct AddModelSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    var kind: String
    @ObservedObject var resource: SettingsResource
    @StateObject private var catalog = SettingsResource("/api/webui/companion/model-catalog")

    private static let groups: [(id: String, label: String)] = [("sub", "用会员账号登录"), ("cn", "国内"), ("global", "海外"), ("hub", "聚合平台")]
    private var offered: [JSONValue] {
        catalog.value["vendors"].array.filter { $0["id"].nonemptyString != nil && $0["kinds"].array.contains(.string(kind)) }
    }
    private var kindLabel: String { ModelsSettingsPage.kindLabels[kind] ?? "" }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text(kind == "text" ? "用会员账号登录，或选一家服务商填上 API key，再挑一个模型。"
                         : kind == "image" ? "伙伴画图、做头像时会用它。" : "伙伴做视频时会用它。")
                        .font(.subheadline).foregroundStyle(.secondary)
                    if catalog.value == .null && catalog.error == nil { ProgressView("正在读取…").frame(maxWidth: .infinity) }
                    if let error = catalog.error { ResourceErrorView(message: error) { Task { await catalog.load(store) } } }
                    ForEach(Self.groups, id: \.id) { group in
                        let members = offered.filter { $0["group"].stringValue == group.id }
                        if !members.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(group.label).font(.caption.weight(.medium)).foregroundStyle(.secondary).padding(.horizontal, 4)
                                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 3), spacing: 8) {
                                    ForEach(members, id: \.selfID) { vendor in
                                        NavigationLink {
                                            AddModelVendorPage(kind: kind, vendor: vendor, resource: resource, onAdded: { dismiss() },
                                                               onSignedIn: { Task { await catalog.load(store) } }, onClose: { dismiss() })
                                        } label: { tile(vendor) }.buttonStyle(.plain)
                                    }
                                }
                            }
                        }
                    }
                }.padding(20)
            }
            .background(CompanionPalette.surface)
            .navigationTitle("添加\(kindLabel)模型").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) {
                Button { dismiss() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭")
            } }
        }
        .task { await catalog.load(store) }
        .inAppLinks()  // 去获取 and the sign-in page open over this sheet
    }

    private func tile(_ vendor: JSONValue) -> some View {
        VStack(spacing: 7) {
            if vendor["id"].stringValue == "custom" {
                Image(systemName: "plus").font(.title3).frame(width: 40, height: 40)
                    .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 11))
            } else { VendorLogo(vendor: vendor, size: 40) }
            Text(vendor["name"].stringValue).font(.system(size: 12.5)).lineLimit(1)
            if vendor["has_key"].boolValue {
                Text(vendor["login"].nonemptyString != nil ? "已登录" : "已填 key").font(.system(size: 10.5)).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity).padding(.top, 14).padding(.bottom, 10).padding(.horizontal, 4)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18))
        .contentShape(RoundedRectangle(cornerRadius: 18))
    }
}

/// One company: its key or sign-in, then the model to add.
@MainActor
private struct AddModelVendorPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.openURL) private var openURL
    var kind: String
    @State var vendor: JSONValue
    @ObservedObject var resource: SettingsResource
    var onAdded: () -> Void
    var onSignedIn: () -> Void
    var onClose: () -> Void

    @State private var models: [JSONValue]?
    @State private var checked: JSONValue = .null
    @State private var canCheck = false
    @State private var checking = false
    @State private var showAll = false
    @State private var picked = ""
    @State private var ownID = ""
    @State private var apiKey = ""
    @State private var baseURL = ""
    @State private var busy = false
    @State private var error: String?

    private static let collapsed = 6
    private var custom: Bool { vendor["id"].stringValue == "custom" }
    private var subscription: Bool { vendor["login"].nonemptyString != nil }
    private var modelID: String { (picked == "__own" || custom ? ownID : picked).trimmingCharacters(in: .whitespacesAndNewlines) }
    private var ready: Bool {
        !modelID.isEmpty && (vendor["has_key"].boolValue || (!subscription && !apiKey.trimmingCharacters(in: .whitespaces).isEmpty))
            && (!custom || baseURL.trimmingCharacters(in: .whitespaces).range(of: #"^https?://\S+$"#, options: .regularExpression) != nil)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let note = vendor["note"].nonemptyString, !subscription { Text(note).font(.footnote).foregroundStyle(.secondary) }
                if custom {
                    field("服务地址") {
                        TextField("https://api.example.com/v1" as String, text: $baseURL)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                }
                if subscription {
                    SubscriptionSignIn(vendor: vendor) {
                        vendor = vendor.merging(["has_key": .bool(true)])
                        onSignedIn()
                        Task { await loadModels() }
                    }
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("API key").font(.footnote.weight(.medium))
                            Spacer()
                            if let console = vendor["console"].nonemptyString, let url = URL(string: console) {
                                Button { openURL(url) } label: { Label("去获取", systemImage: "arrow.up.right").labelStyle(TrailingIcon()) }
                                    .font(.footnote).foregroundStyle(CompanionPalette.primary).buttonStyle(.plain)
                            }
                        }
                        SecureField(vendor["has_key"].boolValue ? "已保存，不换就留空" : "粘贴 API key", text: $apiKey)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().modifier(FieldBox())
                    }
                }
                if !(subscription && !vendor["has_key"].boolValue) {
                    if custom {
                        field("模型 ID") {
                            TextField("比如 qwen-plus" as String, text: $ownID).textInputAutocapitalization(.never).autocorrectionDisabled()
                        }
                    } else { modelList }
                }
            }.padding(20)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(CompanionPalette.surface)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: 8) { VendorLogo(vendor: vendor, size: 24); Text(vendor["name"].stringValue).font(.headline).lineLimit(1) }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { onClose() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }.accessibilityLabel("关闭")
            }
        }
        .safeAreaInset(edge: .bottom) {
            if !(subscription && !vendor["has_key"].boolValue) {
                VStack(spacing: 8) {
                    if let error { Text(error).font(.footnote).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading) }
                    Button { Task { await add() } } label: {
                        Text(busy ? (kind == "text" ? "添加并检查中…" : "添加中…") : "添加")
                            .font(.headline).frame(maxWidth: .infinity).frame(minHeight: 50)
                            .background(CompanionPalette.primary.opacity(ready && !busy ? 1 : 0.4), in: Capsule())
                            .foregroundStyle(CompanionPalette.primaryText)
                    }.buttonStyle(.plain).disabled(!ready || busy)
                }.padding(.horizontal, 20).padding(.vertical, 10).background(.bar)
            }
        }
        .task { if custom { models = [] } else { await loadModels() } }
    }

    // MARK: models

    @ViewBuilder private var modelList: some View {
        let listed = models ?? []
        let recommended = listed.filter { $0["recommended"].boolValue }
        let rest = listed.filter { !$0["recommended"].boolValue }
        let visibleRest = showAll ? rest : Array(rest.prefix(max(0, Self.collapsed - recommended.count)))
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("选择模型").font(.footnote.weight(.medium))
                Spacer()
                if canCheck && !subscription {
                    Button { Task { await checkNow() } } label: {
                        Label(checking ? "检测中" : "检测 ID 和价格", systemImage: "arrow.clockwise")
                    }.font(.footnote).foregroundStyle(CompanionPalette.primary).buttonStyle(.plain).disabled(checking || models == nil)
                }
            }
            if !subscription, let status = checkStatus(listed) {
                Text(status).font(.caption2).foregroundStyle(.secondary)
            }
            if models == nil {
                HStack(spacing: 8) { ProgressView(); Text("正在读取模型…").foregroundStyle(.secondary) }.padding(.vertical, 12)
            } else {
                VStack(spacing: 0) {
                    if !recommended.isEmpty {
                        Label("推荐：最新的几款", systemImage: "sparkles").font(.caption2.weight(.medium)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 14).padding(.top, 10).padding(.bottom, 2)
                    }
                    ForEach(recommended + visibleRest, id: \.selfID) { model in
                        option(model)
                        Divider().padding(.leading, 46)
                    }
                    if !showAll && rest.count > visibleRest.count {
                        Button("显示全部 \(listed.count) 个") { showAll = true }
                            .font(.subheadline).foregroundStyle(CompanionPalette.primary).buttonStyle(.plain)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 14).padding(.vertical, 11)
                        Divider().padding(.leading, 14)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Button { picked = "__own" } label: {
                            HStack(spacing: 12) {
                                RadioMark(selected: picked == "__own")
                                Text(listed.isEmpty ? "填写模型 ID" : "列表里没有？自己填模型 ID").font(.subheadline).foregroundStyle(.primary)
                                Spacer()
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain)
                        if picked == "__own" {
                            TextField("模型 ID" as String, text: $ownID).textInputAutocapitalization(.never).autocorrectionDisabled().modifier(FieldBox())
                        }
                    }.padding(.horizontal, 14).padding(.vertical, 11)
                }
                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18))
            }
            if kind == "text" && listed.contains(where: { $0["vision"].boolValue }) {
                Label("能看图的模型可以直接看你发的照片", systemImage: "eye").font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 4)
            }
        }
    }

    private func option(_ model: JSONValue) -> some View {
        let id = model["id"].stringValue
        let facts = [model["vision"].boolValue ? (kind == "text" ? "看图" : "改图") : nil,
                     model["reasoning"].boolValue ? "深度思考" : nil,
                     kind == "text" ? Self.context(model["context"].doubleValue) : nil].compactMap { $0 }
        let summary = subscription ? nil : Pricing.summary(model["price"])
        let cost = subscription ? "会员额度" : summary?.main
        let note = model["note"].stringValue
        let describe = note.range(of: #"[一-鿿]"#, options: .regularExpression) != nil ? note : (model["name"].stringValue != id ? id : nil)
        let line = ([model["missing"].boolValue ? "官网上没找到了" : nil] + facts.map { Optional($0) } + [describe]).compactMap { $0 }.joined(separator: " · ")
        return Button { picked = id } label: {
            HStack(alignment: .top, spacing: 12) {
                RadioMark(selected: picked == id).padding(.top, 2)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(model["name"].nonemptyString ?? id).font(.system(size: 15, weight: .medium)).lineLimit(1)
                        Spacer(minLength: 6)
                        if let cost { Text(cost).font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                    }
                    if !line.isEmpty { Text(line).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                    if let rows = summary?.rows, !rows.isEmpty {
                        Text(rows.joined(separator: " · ")).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
            }.padding(.horizontal, 14).padding(.vertical, 11).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func checkStatus(_ listed: [JSONValue]) -> String? {
        if checking { return "正在读\(vendor["name"].stringValue)官网的模型和价格，大约半分钟…" }
        let parts = [checked["at"].doubleValue.map { "已按官网核对 · \(Self.ago($0))" } ?? (canCheck ? "还没和官网核对过，模型和价格可能不是最新的" : nil),
                     Pricing.unitHint(listed.map { $0["price"] })].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: actions

    private func loadModels() async {
        if subscription && !vendor["has_key"].boolValue { models = []; return }
        models = nil; error = nil
        do {
            let body = try await store.changeResource("companion.models.vendor", payload: ["vendor": vendor["id"], "kind": .string(kind)])
            apply(body, keepPick: false)
        } catch { models = []; self.error = error.localizedDescription }
    }

    /// 检测: the company's platform read now for its current model ids and prices.
    private func checkNow() async {
        checking = true; error = nil
        defer { checking = false }
        do {
            let body = try await store.changeResource("companion.models.check", payload: ["vendor": vendor["id"], "kind": .string(kind)])
            apply(body, keepPick: true)
        } catch { self.error = "检测没成功：\(error.localizedDescription)" }
    }

    private func apply(_ body: JSONValue, keepPick: Bool) {
        let list = body["models"].array.filter { $0["id"].nonemptyString != nil }
        models = list
        checked = body["checked"]
        canCheck = body["can_check"].boolValue
        // Keep the pick when the platform still has it; else its newest.
        if !(keepPick && (picked == "__own" || list.contains { $0["id"].stringValue == picked })) {
            picked = list.first { $0["recommended"].boolValue }?["id"].stringValue ?? ""
        }
    }

    private func add() async {
        guard ready, !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        var payload: [String: JSONValue] = ["kind": .string(kind), "vendor": vendor["id"], "model": .string(modelID),
                                            "api_key": .string(apiKey.trimmingCharacters(in: .whitespaces))]
        if custom { payload["base_url"] = .string(baseURL.trimmingCharacters(in: .whitespaces)) }
        do {
            let result = try await store.changeResource("companion.models.add", payload: payload)
            resource.value = result
            let health = result["added"].nonemptyString.map { result["health"][$0] } ?? .null
            if health["ok"] == .bool(false) { error = "已添加，但现在用不了：\(health["error"].nonemptyString ?? "出错了")" }
            else { Haptics.selection(); onAdded() }
        } catch { self.error = error.localizedDescription }
    }

    // MARK: pieces

    private func field<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.footnote.weight(.medium))
            content().modifier(FieldBox())
        }
    }

    /// "128K", "1M"
    static func context(_ tokens: Double?) -> String? {
        guard let tokens, tokens > 0 else { return nil }
        return tokens >= 1_000_000 ? "\(Pricing.yuan((tokens / 100_000).rounded() / 10))M" : "\(Int((tokens / 1024).rounded()))K"
    }
    /// "刚刚", "5 分钟前", "3 天前" (seconds since 1970).
    static func ago(_ seconds: Double) -> String {
        let minutes = Int((Date().timeIntervalSince1970 - seconds) / 60)
        if minutes < 1 { return "刚刚" }
        if minutes < 60 { return "\(minutes) 分钟前" }
        if minutes < 60 * 24 { return "\(minutes / 60) 小时前" }
        return "\(minutes / 60 / 24) 天前"
    }
}

/// Sign in with a subscription (ChatGPT, Grok): the server gets a short code, the user enters it on
/// the company's sign-in page, and the server notices when that is done.
@MainActor
private struct SubscriptionSignIn: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.openURL) private var openURL
    let vendor: JSONValue
    var onSignedIn: () -> Void
    @State private var login: JSONValue = .null
    @State private var busy = false
    @State private var copied = false
    @State private var error: String?

    private var account: String { ["chatgpt": "ChatGPT", "grok": "Grok"][vendor["login"].stringValue] ?? vendor["name"].stringValue }
    private var waiting: Bool { login["status"].stringValue == "waiting" }

    var body: some View {
        if vendor["has_key"].boolValue || login["status"].stringValue == "done" {
            Label("已登录 \(account) 账号", systemImage: "checkmark").font(.subheadline.weight(.medium)).foregroundStyle(.green)
        } else {
            VStack(spacing: 12) {
                if waiting {
                    Text("打开 \(account) 的登录页，登录你的会员账号，再输入这串代码：").font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
                    HStack {
                        Text(login["user_code"].stringValue).font(.system(size: 26, weight: .semibold, design: .monospaced)).tracking(4)
                            .textSelection(.enabled).frame(maxWidth: .infinity)
                        Button {
                            UIPasteboard.general.string = login["user_code"].stringValue; copied = true; Haptics.selection()
                        } label: { Image(systemName: copied ? "checkmark" : "doc.on.doc").frame(width: 40, height: 40).background(Color.primary.opacity(0.07), in: Circle()) }
                            .buttonStyle(.plain).accessibilityLabel("复制代码")
                    }
                    if let url = URL(string: login["url"].stringValue) {
                        primary("打开登录页", icon: "arrow.up.right") { openURL(url) }
                    }
                    HStack(spacing: 6) { ProgressView().controlSize(.small); Text("登录好了这里会自动继续") }.font(.caption).foregroundStyle(.secondary)
                } else {
                    if let note = vendor["note"].nonemptyString { Text(note).font(.subheadline).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading) }
                    if vendor["local_login"].boolValue {
                        // Signed in on the server machine already (Codex CLI / app): use that, no second sign-in.
                        primary("用这台电脑上已登录的 \(account)", icon: "checkmark") { Task { await importHere() } }
                        Button("换一个账号登录") { Task { await begin() } }.font(.footnote).foregroundStyle(.secondary).disabled(busy)
                    } else {
                        primary("用 \(account) 账号登录", icon: "person.crop.circle") { Task { await begin() } }
                    }
                }
                if let error { Text(error).font(.footnote).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading) }
            }
            .padding(16).background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 18))
            .task(id: login["id"].stringValue) { await poll() }
        }
    }

    private func primary(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) { if busy { ProgressView().controlSize(.small) } else { Image(systemName: icon) }; Text(title) }
                .font(.headline).frame(maxWidth: .infinity).frame(minHeight: 46)
                .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText)
        }.buttonStyle(.plain).disabled(busy)
    }

    private func begin() async {
        busy = true; error = nil
        defer { busy = false }
        do { login = try await store.changeResource("companion.oauth.start", payload: ["login": vendor["login"]]) }
        catch { self.error = error.localizedDescription }
    }

    private func importHere() async {
        busy = true; error = nil
        defer { busy = false }
        do { _ = try await store.changeResource("companion.oauth.import", payload: ["login": vendor["login"]]); onSignedIn() }
        catch { self.error = error.localizedDescription }
    }

    /// While the code waits to be entered, ask every two seconds whether the sign-in is done.
    private func poll() async {
        guard waiting, let id = login["id"].nonemptyString else { return }
        while !Task.isCancelled && waiting {
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, let next = try? await store.changeResource("companion.oauth.status", payload: ["id": .string(id)]) else { continue }
            login = next
            if next["status"].stringValue == "done" { onSignedIn() }
            if next["status"].stringValue == "failed" { error = next["error"].nonemptyString ?? "登录没有完成，再试一次" }
        }
    }
}

private struct RadioMark: View {
    let selected: Bool
    var body: some View {
        ZStack {
            Circle().strokeBorder(selected ? CompanionPalette.primary : Color.primary.opacity(0.2), lineWidth: 1.5)
                .background(Circle().fill(selected ? CompanionPalette.primary : .clear))
            if selected { Image(systemName: "checkmark").font(.system(size: 10, weight: .heavy)).foregroundStyle(CompanionPalette.primaryText) }
        }.frame(width: 20, height: 20).accessibilityHidden(true)
    }
}

private struct FieldBox: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(.horizontal, 14).frame(minHeight: 46)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
    }
}

/// "去获取 ↗": the icon after the words.
private struct TrailingIcon: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 2) { configuration.title; configuration.icon.imageScale(.small) }
    }
}

private extension JSONValue {
    func merging(_ fields: [String: JSONValue]) -> JSONValue {
        var object = self.object
        fields.forEach { object[$0.key] = $0.value }
        return .object(object)
    }
}
