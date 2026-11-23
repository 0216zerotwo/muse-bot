import SwiftUI

@MainActor
struct NativeCardView: View {
    let card: ReplyCard
    let stateKey: String
    let baseURL: URL?
    var send: ((String) -> Bool)?
    @Environment(\.openURL) private var openURL
    @State private var selected: [String] = []
    @State private var submitted = false
    @State private var values: [String: JSONValue] = [:]
    @State private var checked: [Bool] = []
    @State private var error: String?
    @State private var settingsPage: String?

    private var data: JSONValue { card.data }
    private var locked: Bool { send == nil || submitted }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let title = card.title, !title.isEmpty { Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary) }
            content
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 22))
        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Color.primary.opacity(0.08)))
        .task(id: stateKey) { restore() }
        .sheet(isPresented: Binding(get: { settingsPage != nil }, set: { if !$0 { settingsPage = nil } })) {
            NavigationStack {
                if settingsPage == "connect" { ConnectionsSettingsPage() }
                else { VaultSettingsPage() }
            }.environment(\.settingsClose, { settingsPage = nil })
                .presentationDetents([.large]).presentationDragIndicator(.visible)
        }
    }

    @ViewBuilder private var content: some View {
        switch card.type {
        case "buttons", "choices": choices
        case "pictures": pictures
        case "weather": weather
        case "table": table(columns: data["columns"].array.map(\.stringValue), rows: data["rows"].array.map { $0.array.map(\.stringValue) })
        case "compare": comparison
        case "checklist": checklist
        case "timeline": timeline
        case "places": places
        case "form": form
        case "trains": trains
        case "ui", "widget": EmbeddedCardView(card: card, baseURL: baseURL, send: locked ? nil : send)
        case "credential": credentialCard
        case "connect":
            NativeConnectCardView(
                serviceName: data["service"].stringValue,
                reason: data["reason"].nonemptyString,
                submitted: submitted,
                locked: locked,
                openSettings: { settingsPage = "connect" },
                onConnected: { finish($0) }
            )
        default: webPlaceholder
        }
    }

    private var choiceItems: [JSONValue] { card.type == "buttons" ? data["items"].array : data["options"].array }
    private var choices: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(choiceItems.enumerated()), id: \.offset) { _, item in
                let label = item["label"].string ?? item.stringValue
                let chosen = selected.contains(label)
                Button {
                    guard !locked else { return }
                    Haptics.selection()
                    if data["multiple"].boolValue {
                        selected = chosen ? selected.filter { $0 != label } : selected + [label]
                    } else {
                        selected = [label]; finish(item["send"].string ?? label)
                    }
                } label: {
                    HStack(spacing: 8) {
                        if chosen { Image(systemName: "checkmark.circle.fill").accessibilityHidden(true) }
                        Text(label + (item["primary"].boolValue && card.type == "choices" ? " · 推荐" : ""))
                        Spacer(minLength: 0)
                    }.frame(minHeight: 44).padding(.horizontal, 12)
                        .background(chosen ? CompanionPalette.primary.opacity(0.16) : Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 14))
                }.buttonStyle(.plain).disabled(locked)
                    .accessibilityValue(chosen ? "已选择" : "未选择")
            }
            if data["multiple"].boolValue && !submitted && send != nil {
                Button(data["submit"].string ?? "就这些") {
                    let answer = choiceItems.filter { selected.contains($0["label"].stringValue) }
                        .map { $0["send"].string ?? $0["label"].stringValue }.joined(separator: ", ")
                    finish(answer)
                }.buttonStyle(.borderedProminent).foregroundStyle(Color(uiColor: .systemBackground)).frame(minHeight: 44).disabled(selected.isEmpty)
            }
        }
    }

    private var pictures: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { _, item in
                let label = item["label"].stringValue
                Button {
                    guard !locked else { return }
                    selected = [label]; finish(item["send"].string ?? label)
                } label: {
                    VStack(spacing: 8) {
                        RemotePicture(path: item["url"].stringValue, baseURL: baseURL, maxPixel: 600)
                            .aspectRatio(1, contentMode: .fit).frame(maxWidth: .infinity).clipped()
                        Label(label, systemImage: selected.contains(label) ? "checkmark.circle.fill" : "circle")
                            .font(.subheadline).lineLimit(2).frame(minHeight: 44)
                    }.padding(8).background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 16))
                        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(selected.contains(label) ? CompanionPalette.primary : Color.clear, lineWidth: 2))
                }.buttonStyle(.plain).disabled(locked).accessibilityLabel(label)
            }
        }
    }

    private var weather: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text([data["place"].stringValue, data["when"].stringValue].filter { !$0.isEmpty }.joined(separator: " · ")).font(.subheadline).foregroundStyle(.secondary)
                    Text(data["temp"].stringValue).font(.largeTitle.weight(.semibold))
                    Text(data["summary"].stringValue)
                }
                Spacer()
                Image(systemName: weatherIcon(data["icon"].string ?? data["summary"].stringValue))
                    .font(.largeTitle).foregroundStyle(CompanionPalette.primary).accessibilityHidden(true)
            }
            Text([data["high"].string.map { "最高 \($0)" }, data["low"].string.map { "最低 \($0)" }].compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
            if !data["hours"].array.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 20) {
                        ForEach(Array(data["hours"].array.enumerated()), id: \.offset) { _, hour in
                            VStack(spacing: 8) {
                                Text(hour["time"].stringValue).font(.caption)
                                Image(systemName: weatherIcon(hour["icon"].stringValue)).accessibilityHidden(true)
                                Text(hour["temp"].stringValue).font(.subheadline)
                            }
                        }
                    }
                }
            }
            ForEach(Array(data["details"].array.enumerated()), id: \.offset) { _, item in
                LabeledContent(item["label"].stringValue, value: item["value"].stringValue).font(.subheadline)
            }
        }
    }

    private func weatherIcon(_ hint: String) -> String {
        let text = hint.lowercased()
        for (terms, icon) in [( ["thunder", "storm", "雷"], "cloud.bolt.rain"), (["snow", "雪"], "cloud.snow"),
                             (["rain", "shower", "雨"], "cloud.rain"), (["fog", "mist", "雾", "霾"], "cloud.fog"),
                             (["wind", "风"], "wind"), (["moon", "night", "夜"], "moon.stars"),
                             (["partly", "多云"], "cloud.sun"), (["cloud", "阴", "云"], "cloud"), (["sun", "clear", "晴"], "sun.max")] {
            if terms.contains(where: { text.contains($0) }) { return icon }
        }
        return "cloud.sun"
    }

    private func table(columns: [String], rows: [[String]]) -> some View {
        ScrollView(.horizontal) {
            Grid(alignment: .topLeading, horizontalSpacing: 16, verticalSpacing: 12) {
                GridRow {
                    ForEach(Array(columns.enumerated()), id: \.offset) { _, column in
                        Text(column).font(.subheadline.weight(.semibold)).frame(minWidth: 80, alignment: .leading)
                    }
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Divider().gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        ForEach(Array(columns.enumerated()), id: \.offset) { index, _ in
                            Text(index < row.count ? row[index] : "—")
                                .font(.subheadline).frame(minWidth: 80, maxWidth: 200, alignment: .leading)
                        }
                    }
                }
            }.padding(.vertical, 4)
        }.accessibilityLabel("可横向滚动的表格")
    }

    private var comparison: some View {
        let best = data["best"].intValue
        let columns = [""] + data["options"].array.enumerated().map { index, value in
            value.stringValue + (index == best ? " · 推荐" : "")
        }
        let rows = data["rows"].array.map { [$0["label"].stringValue] + $0["values"].array.map(\.stringValue) }
        return table(columns: columns, rows: rows)
    }

    private var checklist: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(checked.filter { $0 }.count)/\(data["items"].array.count)").font(.caption).foregroundStyle(.secondary)
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { index, item in
                let done = index < checked.count && checked[index]
                Button {
                    guard index < checked.count else { return }
                    checked[index].toggle(); persist(); Haptics.selection()
                } label: {
                    Label(item["text"].stringValue, systemImage: done ? "checkmark.circle.fill" : "circle")
                        .strikethrough(done).foregroundStyle(done ? Color.secondary : Color.primary)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                }.buttonStyle(.plain).accessibilityValue(done ? "已完成" : "未完成")
            }
        }
    }

    private var timeline: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top, spacing: 12) {
                    Text(item["time"].stringValue).font(.caption.weight(.semibold)).foregroundStyle(CompanionPalette.primary).frame(width: 52, alignment: .trailing)
                    Image(systemName: "circle.fill").font(.system(size: 7)).foregroundStyle(CompanionPalette.primary).padding(.top, 6).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item["title"].stringValue).font(.body)
                        if let detail = item["detail"].string { Text(detail).font(.subheadline).foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }

    private var places: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { _, place in
                Button { if let url = placeURL(place) { openURL(url) } } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "map").foregroundStyle(CompanionPalette.primary).accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(place["name"].stringValue).font(.body.weight(.medium))
                            Text([place["address"].stringValue, place["note"].stringValue].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary)
                            if let rating = place["rating"].string { Text("评分 \(rating)").font(.caption).foregroundStyle(.secondary) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        Image(systemName: "arrow.up.right").font(.caption).accessibilityHidden(true)
                    }.frame(minHeight: 48)
                }.buttonStyle(.plain)
            }
        }
    }

    private func placeURL(_ place: JSONValue) -> URL? {
        if let url = resolvedURL(place["url"].stringValue, baseURL: nil) { return url }
        var components = URLComponents(string: "https://uri.amap.com/search")
        if let lat = place["lat"].doubleValue, let lng = place["lng"].doubleValue {
            components = URLComponents(string: "https://uri.amap.com/marker")
            components?.queryItems = [URLQueryItem(name: "position", value: "\(lng),\(lat)"), URLQueryItem(name: "name", value: place["name"].stringValue)]
        } else {
            components?.queryItems = [URLQueryItem(name: "keyword", value: place["name"].stringValue + " " + place["address"].stringValue)]
        }
        return components?.url
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(Array(card.fields.enumerated()), id: \.offset) { _, field in
                NativeFormField(field: field, value: Binding(get: { values[field.name] ?? .null }, set: {
                    values[field.name] = $0; error = nil
                })).disabled(locked)
            }
            if let error { Text(error).font(.footnote).foregroundStyle(.red).accessibilityAddTraits(.updatesFrequently) }
            if submitted {
                Label("已提交", systemImage: "checkmark.circle").font(.footnote).foregroundStyle(.secondary)
            } else if send != nil {
                Button(data["submit"].string ?? "提交") { submitForm() }
                    .buttonStyle(.borderedProminent).foregroundStyle(Color(uiColor: .systemBackground)).frame(minHeight: 44)
            }
        }
    }

    private func submitForm() {
        guard !locked else { return }
        if let missing = card.fields.first(where: { field in
            guard field.required else { return false }
            let value = values[field.name] ?? .null
            return value.array.isEmpty && value.nonemptyString == nil
        }) { error = "还没填「\(missing.label)」"; return }
        finish(ReplyContent.formMessage(card: card, values: values))
    }

    private var trains: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text([data["from"].stringValue, data["to"].stringValue, data["date"].stringValue].filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption)
            ForEach(Array(data["items"].array.enumerated()), id: \.offset) { _, train in
                Button { selected = [train["code"].stringValue]; finish(train["send"].string ?? "就选 \(train["code"].stringValue)") } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack { Text(train["depart"].stringValue); Spacer(); Text(train["code"].stringValue).font(.caption); Spacer(); Text(train["arrive"].stringValue) }
                        Text([train["duration"].stringValue, train["price"].stringValue].filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
                        ForEach(Array(train["seats"].array.enumerated()), id: \.offset) { _, seat in
                            Text(seat["name"].stringValue + " · " + seat["left"].stringValue).font(.caption)
                        }
                    }.padding(12).frame(minHeight: 44).background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 14))
                }.buttonStyle(.plain).disabled(locked)
            }
        }
    }

    private var credentialCard: some View {
        let kind = data["kind"].nonemptyString ?? "login"
        let service = data["service"].nonemptyString ?? "服务"
        let what = kind == "login" ? "登录" : "密钥"
        return VStack(alignment: .leading, spacing: 12) {
            Label("\(service) 的\(what)", systemImage: "lock.fill")
                .font(.headline)
            if let reason = data["reason"].nonemptyString {
                Text(reason).font(.subheadline).foregroundStyle(.secondary)
            }
            if submitted {
                Label("已加密保存", systemImage: "checkmark.circle.fill")
                    .font(.subheadline).foregroundStyle(.secondary)
            } else if !locked {
                VaultCredentialForm(
                    kind: kind,
                    initialService: service,
                    initialOrigin: data["origin"].stringValue,
                    initialEnv: data["env"].stringValue,
                    compact: true
                ) { _ in
                    finish("\(service) 的\(what)存好了，接着做")
                }
            } else {
                Text("密码和密钥不会进入聊天，可在安全凭证里查看或管理。")
                    .font(.footnote).foregroundStyle(.secondary)
                Button("打开安全凭证") { settingsPage = "credential" }
                    .buttonStyle(.bordered).frame(minHeight: 44)
            }
        }
    }

    private var webPlaceholder: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(card.type == "ui" || card.type == "widget" ? "互动内容" : "需要在网页版继续", systemImage: "rectangle.and.hand.point.up.left")
                .font(.headline)
            Text(data["summary"].string ?? data["reason"].string ?? "这张卡片的完整内容可以在网页版中打开。")
                .font(.subheadline).foregroundStyle(.secondary)
            if let url = resolvedURL("/companion.html", baseURL: baseURL) {
                Button("在网页版打开") { openURL(url) }.frame(minHeight: 44)
            }
        }
    }

    private func finish(_ text: String) {
        guard !locked, !text.isEmpty, let send, send(text) else { return }
        submitted = true; persist(); Haptics.send()
    }
    private func restore() {
        selected = []; submitted = false; error = nil
        checked = data["items"].array.map { $0["done"].boolValue }
        values = Dictionary(card.fields.map { field in
            let initial: JSONValue = field.value != .null ? inputValue(field.value) : (field.kind == "multi" ? .array([]) : .string(field.kind == "slider" ? String(format: "%g", field.min ?? 0) : ""))
            return (field.name, initial)
        }, uniquingKeysWith: { _, last in last })
        guard let saved = UserDefaults.standard.data(forKey: "card." + stateKey), let record = try? JSONDecoder().decode(JSONValue.self, from: saved) else { return }
        selected = record["selected"].array.map(\.stringValue)
        submitted = record["submitted"].boolValue
        if !record["values"].object.isEmpty { values = record["values"].object.mapValues(inputValue) }
        let prior = record["checked"].array.map(\.boolValue)
        if prior.count == checked.count { checked = prior }
    }
    private func persist() {
        let record = JSONValue.object(["selected": .array(selected.map(JSONValue.string)), "submitted": .bool(submitted),
                                       "values": .object(values), "checked": .array(checked.map(JSONValue.bool))])
        if let encoded = try? JSONEncoder().encode(record) { UserDefaults.standard.set(encoded, forKey: "card." + stateKey) }
    }
    private func inputValue(_ value: JSONValue) -> JSONValue {
        if case .array = value { return .array(value.array.map { .string($0.nonemptyString ?? "") }) }
        return .string(value.nonemptyString ?? "")
    }
}

@MainActor
private struct NativeFormField: View {
    let field: FormField
    @Binding var value: JSONValue
    @State private var date = Date()
    private var text: Binding<String> { Binding(get: { value.stringValue }, set: { value = .string($0) }) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(field.label + (field.required ? " *" : "")).font(.subheadline.weight(.medium))
            switch field.kind {
            case "select":
                Picker(field.label, selection: text) {
                    Text("请选择").tag("")
                    ForEach(field.options, id: \.self) { Text($0).tag($0) }
                }.pickerStyle(.menu).frame(minHeight: 44)
            case "chips", "multi":
                ForEach(field.options, id: \.self) { option in
                    let chosen = value.array.map(\.stringValue).contains(option) || value.stringValue == option
                    Button {
                        if field.kind == "chips" { value = .string(option) }
                        else {
                            var list = value.array.map(\.stringValue)
                            if chosen { list.removeAll { $0 == option } } else { list.append(option) }
                            value = .array(list.map(JSONValue.string))
                        }
                        Haptics.selection()
                    } label: {
                        Label(option, systemImage: chosen ? "checkmark.circle.fill" : "circle").frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }.buttonStyle(.plain).accessibilityValue(chosen ? "已选择" : "未选择")
                }
            case "slider":
                let lower = field.min ?? 0
                let upper = max(lower + 1, field.max ?? 100)
                Slider(value: Binding(get: { min(upper, max(lower, Double(value.stringValue) ?? lower)) }, set: { value = .string(String(format: "%g", $0)) }),
                       in: lower...upper, step: max(0.001, field.step ?? 1)) { Text(field.label) }
                Text(value.stringValue + (field.unit ?? "")).font(.subheadline.monospacedDigit())
            case "date", "time":
                DatePicker(field.label, selection: $date, displayedComponents: field.kind == "date" ? .date : .hourAndMinute)
                    .labelsHidden().frame(minHeight: 44)
                    .onChange(of: date) { _, updated in value = .string(formatter.string(from: updated)) }
                    .onAppear { if let preset = formatter.date(from: value.stringValue) { date = preset } }
                if value.stringValue.isEmpty {
                    Button("使用当前\(field.kind == "date" ? "日期" : "时间")") { value = .string(formatter.string(from: date)) }.font(.footnote).frame(minHeight: 44)
                }
            default:
                TextField(field.placeholder ?? field.label, text: text)
                    .textFieldStyle(.roundedBorder).keyboardType(field.kind == "number" ? .decimalPad : .default)
                    .frame(minHeight: 44).accessibilityLabel(field.label)
            }
        }
    }
    private var formatter: DateFormatter {
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = field.kind == "date" ? "yyyy-MM-dd" : "HH:mm"
        return formatter
    }
}

@MainActor
struct NativeApprovalView: View {
    let approval: JSONValue
    var decide: (String, String) async -> Void
    @State private var busy = false
    private var args: JSONValue { approval["arguments"] }
    private var action: String { ApprovalWords.action(approval) }
    private var title: String { ApprovalWords.title(approval) }
    private var confirm: String {
        if approval["context"]["browser"]["paying"].boolValue { return "确认付款" }
        return ["send_email": "发送", "forward_email": "转发", "delete_emails": "删除", "delete_event": "删除", "browser_submit": "点吧",
         "browser_upload": "上传", "run_command": "运行", "goal_plan": "用新计划", "send_private": "允许发送", "payment": "确认付款"][action] ?? "允许"
    }
    private var subject: String {
        if ["send_email", "forward_email"].contains(action) {
            let list = ApprovalWords.recipients(args)
            if !list.isEmpty { return "发给 " + list.joined(separator: "、") }
            return args["subject"].nonemptyString ?? ""
        }
        if action == "delete_emails" {
            let ids = ApprovalWords.emailIDs(args)
            if !ids.isEmpty { return "\(ids.count) 封邮件" }
        }
        if action == "run_command" { return args["reason"].nonemptyString ?? args["command"].stringValue }
        if action == "send_private" {
            let host = approval["context"]["egress"]["host"].stringValue
            return host.isEmpty ? "" : "发到 " + host
        }
        return ""
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(title, systemImage: "hand.raised.fill").font(.headline)
            if approval["status"].stringValue == "pending" { Text("等你确认").font(.subheadline).foregroundStyle(CompanionPalette.primary) }
            if let summary = ApprovalWords.browserSummary(approval) {
                Text(summary).font(.body).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(args.object.keys.sorted()), id: \.self) { key in
                if !hiddenKeys.contains(key) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(argumentLabel(key)).font(.caption).foregroundStyle(.secondary)
                        Text(argumentValueText(key: key, value: args[key])).font(key == "command" ? .footnote.monospaced() : .subheadline)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if action == "goal_plan" {
                Text(approval["context"]["goal"]["title"].stringValue).font(.subheadline.weight(.medium))
                ForEach(Array(approval["context"]["goal"]["steps"].array.enumerated()), id: \.offset) { _, step in
                    Text(step["text"].stringValue).font(.subheadline).strikethrough(step["status"].stringValue != "done").foregroundStyle(.secondary)
                }
                Text("新计划（已完成的步骤保留）").font(.caption).foregroundStyle(.secondary)
                ForEach(Array(args["steps"].array.enumerated()), id: \.offset) { index, step in Text("\(index + 1). \(step.stringValue)").font(.subheadline) }
            }
            if action == "send_private" {
                Text("这些内容来自你的邮件或日程，要发到这个网站。").font(.footnote).foregroundStyle(.secondary)
                Text(approval["context"]["egress"]["host"].stringValue).font(.subheadline.bold())
                Text(approval["context"]["egress"]["what"].stringValue).font(.subheadline).textSelection(.enabled)
            }
            if approval["status"].stringValue == "pending" {
                VStack(spacing: 8) {
                    Button(confirm + (standingScopes.isEmpty ? "" : "，仅这次")) { perform("approve", "once") }
                        .buttonStyle(.borderedProminent).tint(["delete_emails", "delete_event"].contains(action) ? .red : .accentColor)
                        .foregroundStyle(Color(uiColor: .systemBackground)).frame(minHeight: 44)
                    ForEach(standingScopes, id: \.self) { scope in
                        Button(standingLabel(scope)) { perform("approve", scope) }.buttonStyle(.bordered)
                    }
                    Button(action == "goal_plan" ? "保持原样" : "不要", role: .cancel) { perform("reject", "once") }.buttonStyle(.bordered)
                    if busy { ProgressView("处理中…") }
                }.buttonBorderShape(.capsule).frame(maxWidth: .infinity).disabled(busy)
            } else {
                Text(status).font(.subheadline).foregroundStyle(approval["status"].stringValue == "failed" ? Color.red : Color.secondary)
            }
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(CompanionPalette.primary.opacity(0.25)))
    }
    /// Said by the sentence above instead: a browser script, its address, what it clicks.
    private var hiddenKeys: Set<String> {
        ApprovalWords.browserSummary(approval) == nil ? ["steps"] : ["steps", "code", "element", "ref", "url", "paths", "target", "timeout"]
    }
    private var standingScopes: [String] { approval["scopes"].array.map(\.stringValue).filter { ["24h", "always"].contains($0) } }
    private func standingLabel(_ scope: String) -> String {
        let what = subject.isEmpty ? "同样的事" : "「\(subject)」"
        return scope == "24h" ? "24 小时内\(what)都允许" : "\(what)一直允许"
    }
    private var status: String {
        switch approval["status"].stringValue {
        case "executed": return "已执行" + (approval["scope"].stringValue == "24h" ? "，24 小时内不再问" : approval["scope"].stringValue == "always" ? "，一直允许" : "")
        case "rejected": return action == "goal_plan" ? "保持原来的计划" : "已取消，没有执行"
        default: return "没有成功：" + (approval["result"].string ?? "未知错误")
        }
    }
    private func perform(_ decision: String, _ scope: String) {
        guard !busy else { return }; busy = true; Haptics.selection()
        Task { await decide(decision, scope); busy = false }
    }
    private func argumentLabel(_ key: String) -> String { ApprovalWords.argumentLabel(key) }
    private func argumentText(_ value: JSONValue) -> String { ApprovalWords.argumentText(value) }
    private func argumentValueText(key: String, value: JSONValue) -> String {
        if ["recipients", "to", "cc", "bcc"].contains(key) {
            let parsed = ApprovalWords.recipients(.object(["recipients": value]))
            if !parsed.isEmpty { return parsed.joined(separator: "、") }
        }
        if key == "email_ids" || key == "email_id" {
            let ids = ApprovalWords.emailIDs(.object(["email_ids": value]))
            if !ids.isEmpty { return ids.joined(separator: "、") }
        }
        return argumentText(value)
    }
}

@MainActor
private struct NativeConnectCardView: View {
    @EnvironmentObject private var store: CompanionStore
    let serviceName: String
    let reason: String?
    let submitted: Bool
    let locked: Bool
    var openSettings: () -> Void
    var onConnected: (String) -> Void

    @State private var preset: JSONValue = .null
    @State private var loading = true
    @State private var values: [String: String] = [:]
    @State private var busy = false
    @State private var error: String?

    private var displayName: String {
        preset["display_name"].nonemptyString ?? (serviceName.isEmpty ? "服务" : serviceName)
    }
    private var isConnected: Bool {
        submitted || preset["installed"].boolValue || preset["configured"].boolValue
    }
    private var fields: [JSONValue] {
        preset["required_fields"].array.filter { $0["name"].nonemptyString != nil }
    }
    private var missingRequired: Bool {
        fields.contains { field in
            field["required"].boolValue && !field["configured"].boolValue
                && (values[field["name"].stringValue] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                if preset != .null {
                    VendorLogo(vendor: .object([
                        "name": .string(displayName),
                        "logo_url": preset["logo_url"],
                        "color": preset["brand_color"],
                    ]), size: 36)
                } else {
                    Image(systemName: "powerplug").font(.title3).foregroundStyle(CompanionPalette.primary)
                        .frame(width: 36, height: 36).background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(displayName).font(.headline)
                    Text(isConnected ? "已连接" : (reason ?? preset["description"].nonemptyString ?? "连上后才能接着做"))
                        .font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer(minLength: 0)
                if isConnected {
                    Label("已连接", systemImage: "checkmark.circle.fill")
                        .font(.footnote.weight(.medium)).foregroundStyle(.secondary)
                }
            }

            if loading {
                ProgressView().frame(maxWidth: .infinity, minHeight: 36)
            } else if isConnected {
                EmptyView()
            } else if preset != .null && !locked {
                if let note = preset["note"].nonemptyString {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                }
                ForEach(fields, id: \.selfID) { field in
                    let key = field["name"].stringValue
                    let label = field["label"].nonemptyString ?? key
                    let placeholder = field["configured"].boolValue ? "已保存，留空不变" : field["placeholder"].stringValue
                    VStack(alignment: .leading, spacing: 4) {
                        Text(label + (field["required"].boolValue ? "" : "（可选）")).font(.caption.weight(.medium))
                        Group {
                            if field["secret"].boolValue {
                                SecureField(placeholder, text: binding(for: key))
                            } else {
                                TextField(placeholder, text: binding(for: key))
                            }
                        }
                        .font(.system(size: 15))
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(.horizontal, 12)
                        .frame(minHeight: 42)
                        .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
                        .accessibilityLabel(label)
                    }
                }
                if let error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
                HStack(spacing: 10) {
                    Button {
                        Task { await connectService() }
                    } label: {
                        HStack(spacing: 6) {
                            if busy { ProgressView().controlSize(.small) }
                            Text(busy ? "连接中…" : (fields.isEmpty ? "连接" : "保存并连接"))
                                .font(.system(size: 15, weight: .semibold))
                        }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(CompanionPalette.primary, in: Capsule())
                        .foregroundStyle(CompanionPalette.primaryText)
                        .opacity(missingRequired || busy ? 0.45 : 1)
                    }
                    .buttonStyle(.plain)
                    .disabled(missingRequired || busy)

                    if fields.isEmpty {
                        Button("设置", action: openSettings)
                            .font(.subheadline)
                            .padding(.horizontal, 14)
                            .frame(minHeight: 44)
                            .background(Color.primary.opacity(0.07), in: Capsule())
                            .buttonStyle(.plain)
                    }
                }
            } else {
                Button("管理服务连接", action: openSettings)
                    .buttonStyle(.borderedProminent)
                    .frame(minHeight: 44)
            }
        }
        .task(id: serviceName) { await loadPreset() }
    }

    private func binding(for key: String) -> Binding<String> {
        Binding(get: { values[key] ?? "" }, set: { values[key] = $0; error = nil })
    }

    private func loadPreset() async {
        guard !serviceName.isEmpty else { loading = false; return }
        loading = true
        defer { loading = false }
        do {
            let payload = try await store.getResource("/api/settings/mcp-presets")
            if let found = payload["presets"].array.first(where: { $0["name"].stringValue == serviceName }) {
                preset = found
            }
        } catch {
            // Falls back to opening ConnectionsSettingsPage when preset metadata cannot be loaded.
        }
    }

    private func connectService() async {
        guard !busy, !missingRequired else { return }
        busy = true
        error = nil
        defer { busy = false }
        var payload: [String: JSONValue] = ["name": .string(serviceName)]
        for (key, value) in values {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { payload[key] = .string(trimmed) }
        }
        do {
            let result = try await store.changeResource("settings.mcp.enable", payload: payload)
            if let updated = result["presets"].array.first(where: { $0["name"].stringValue == serviceName }) {
                preset = updated
            }
            if result["last_action"]["ok"] == .bool(false) {
                error = result["last_action"]["error"].nonemptyString ?? result["last_action"]["message"].nonemptyString ?? "连接没有完成，请重试。"
                return
            }
            if result["hot_reload"]["ok"] == .bool(false) {
                error = result["hot_reload"]["message"].nonemptyString ?? "设置已保存，服务器还没有重新连接这个服务。"
                return
            }
            values = [:]
            if preset["installed"].boolValue || preset["configured"].boolValue {
                onConnected("\(displayName) 连好了，接着做刚才的事")
            }
        } catch {
            self.error = error.localizedDescription
        }
    }
}
