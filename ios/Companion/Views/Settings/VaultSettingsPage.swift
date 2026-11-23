import Foundation
import SwiftUI
import UIKit

enum VaultValidation {
    static let forbiddenEnvNames: Set<String> = [
        "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HOME", "USER", "SHELL",
        "HERMES_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS", "PROMPT_COMMAND", "BASH_ENV", "ENV",
    ]
    static let forbiddenEnvPrefixes = [
        "COMPANION_", "HERMES_", "PYTHON", "LD_", "DYLD_", "GIT_SSH", "SSH_",
    ]

    static func isAllowedEnvName(_ raw: String) -> Bool {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard name.range(of: "^[A-Z][A-Z0-9_]{2,63}$", options: .regularExpression) != nil else { return false }
        if forbiddenEnvNames.contains(name) { return false }
        if forbiddenEnvPrefixes.contains(where: { name.hasPrefix($0) }) { return false }
        return true
    }

    static func envNameError(_ raw: String) -> String? {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return nil }
        if forbiddenEnvNames.contains(name) || forbiddenEnvPrefixes.contains(where: { name.hasPrefix($0) }) {
            return "这个变量名属于系统保留项，请换一个名称。"
        }
        guard name.range(of: "^[A-Z][A-Z0-9_]{2,63}$", options: .regularExpression) != nil else {
            return "变量名需以大写字母开头，由 3~64 位大写字母、数字或下划线组成。"
        }
        return nil
    }

    static func isValidOrigin(_ raw: String) -> Bool {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        let candidate: String
        if trimmed.range(of: "^[A-Za-z][A-Za-z0-9+.-]*://", options: .regularExpression) != nil {
            candidate = trimmed
        } else {
            candidate = "https://" + trimmed
        }
        guard let parts = URLComponents(string: candidate),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              let host = parts.host?.lowercased(), !host.isEmpty,
              parts.user == nil, parts.password == nil else {
            return false
        }
        return host == "localhost" || host.contains(".") || host.contains(":")
    }
}

/// Native encrypted credential form shared by `VaultSettingsPage` and inline `credential` chat cards.
@MainActor
struct VaultCredentialForm: View {
    @EnvironmentObject private var store: CompanionStore
    let kind: String
    var initialService = ""
    var initialOrigin = ""
    var initialEnv = ""
    var compact = false
    var onSaved: (JSONValue) -> Void

    @State private var label = ""
    @State private var site = ""
    @State private var identifier = ""
    @State private var secret = ""
    @State private var variable = ""
    @State private var busy = false
    @State private var error: String?
    @State private var initialized = false

    private var isLogin: Bool { kind == "login" }
    private var cleanLabel: String { label.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var cleanSite: String { site.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var cleanIdentifier: String { identifier.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var cleanVariable: String { variable.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() }
    private var cleanSecret: String { isLogin ? secret : secret.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var ready: Bool {
        if isLogin {
            return VaultValidation.isValidOrigin(cleanSite) && !cleanIdentifier.isEmpty && !secret.isEmpty
        }
        return VaultValidation.isAllowedEnvName(cleanVariable) && !cleanSecret.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 10 : 14) {
            if isLogin {
                if !compact {
                    field("名称（可选，比如 GitHub）", text: $label)
                }
                field("网站地址，比如 github.com", text: $site, keyboard: .URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                if !cleanSite.isEmpty && !VaultValidation.isValidOrigin(cleanSite) {
                    Text("请输入有效的网站域名或 http(s) 地址。").font(.caption).foregroundStyle(.red)
                }
                field("账号（邮箱、手机号或用户名）", text: $identifier)
                    .textContentType(.username)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                secureField("密码", text: $secret)
                    .textContentType(.password)
            } else {
                field("名称，比如 GitHub", text: $label)
                field("变量名，比如 GITHUB_TOKEN", text: $variable, monospaced: true)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .onChange(of: variable) { _, next in
                        let upper = next.uppercased()
                        if upper != next { variable = upper }
                    }
                if let envError = VaultValidation.envNameError(cleanVariable) {
                    Text(envError).font(.caption).foregroundStyle(.red)
                }
                secureField("密钥", text: $secret)
            }
            if let error {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            Button {
                Task { await submit() }
            } label: {
                HStack(spacing: 8) {
                    if busy { ProgressView().controlSize(.small) }
                    Text(busy ? "保存中…" : "加密保存").font(.system(size: 15, weight: .semibold))
                }
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(CompanionPalette.primary, in: Capsule())
                .foregroundStyle(CompanionPalette.primaryText)
                .opacity(ready && !busy ? 1 : 0.45)
            }
            .buttonStyle(.plain)
            .disabled(!ready || busy)

            Text("只存在你的服务器上，加密保存。它只能拿来帮你在对应的网站或服务上用，看不到内容。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .onAppear {
            guard !initialized else { return }
            initialized = true
            label = initialService
            site = initialOrigin
            variable = initialEnv.uppercased()
        }
    }

    private func field(_ placeholder: String, text: Binding<String>, keyboard: UIKeyboardType = .default, monospaced: Bool = false) -> some View {
        TextField(placeholder, text: text)
            .keyboardType(keyboard)
            .font(monospaced ? .system(size: 15, design: .monospaced) : .system(size: 15))
            .padding(.horizontal, 14)
            .frame(minHeight: 44)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
            .accessibilityLabel(placeholder)
    }

    private func secureField(_ placeholder: String, text: Binding<String>) -> some View {
        SecureField(placeholder, text: text)
            .font(.system(size: 15))
            .padding(.horizontal, 14)
            .frame(minHeight: 44)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
            .accessibilityLabel(placeholder)
    }

    private func submit() async {
        guard ready, !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        let payload: [String: JSONValue]
        if isLogin {
            payload = [
                "action": .string("add_login"),
                "label": .string(cleanLabel.isEmpty ? initialService : cleanLabel),
                "origin": .string(cleanSite),
                "identifier": .string(cleanIdentifier),
                "password": .string(secret),
            ]
        } else {
            payload = [
                "action": .string("add_key"),
                "label": .string(cleanLabel.isEmpty ? cleanVariable : cleanLabel),
                "env": .string(cleanVariable),
                "value": .string(cleanSecret),
            ]
        }
        do {
            let next = try await store.changeResource("companion.vault.change", payload: payload)
            secret = ""
            Haptics.selection()
            onSaved(next)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private enum VaultAddingKind: String, Identifiable {
    case login = "login"
    case apiKey = "api_key"
    var id: String { rawValue }
    var title: String { self == .login ? "添加网站登录" : "添加 API 密钥" }
}

@MainActor
struct VaultSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/vault")
    @State private var adding: VaultAddingKind?
    @State private var removing: JSONValue?

    var body: some View {
        Form {
            Section {
                Text("网站登录和服务密钥都加密存在你的服务器上。它需要时会在聊天里弹出卡片让你填，不用把密码发在聊天里。")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section {
                ForEach(SettingsDecoding.rows(resource.value, key: "logins", id: "id"), id: \.selfID) { login in
                    let host = login["origin"].stringValue.replacingOccurrences(of: "^https?://", with: "", options: .regularExpression)
                    let subtitle = [login["identifier"].nonemptyString, host.isEmpty ? nil : host].compactMap { $0 }.joined(separator: " · ")
                    credential(login, kind: "login", id: login["id"].stringValue, detail: subtitle)
                }
                if resource.value != .null && resource.value["logins"].array.isEmpty { Text("还没有。").foregroundStyle(.secondary) }
            } header: {
                HStack {
                    Text("网站登录")
                    Spacer()
                    Button("添加") { adding = .login }.textCase(nil)
                }
            }
            Section {
                ForEach(SettingsDecoding.rows(resource.value, key: "keys", id: "env"), id: \.selfID) { key in
                    let status = key["set"].boolValue ? "已设好" : "尚未设置"
                    let subtitle = [key["env"].nonemptyString, status].compactMap { $0 }.joined(separator: " · ")
                    credential(key, kind: "key", id: key["env"].stringValue, detail: subtitle)
                }
                if resource.value != .null && resource.value["keys"].array.isEmpty { Text("还没有。模型的密钥在「模型」里管理。").foregroundStyle(.secondary) }
            } header: {
                HStack {
                    Text("API 密钥")
                    Spacer()
                    Button("添加") { adding = .apiKey }.textCase(nil)
                }
            }
        }
        .settingsStyle("安全凭证")
        .task { await resource.load(store) }
        .refreshable { await resource.load(store) }
        .sheet(item: $adding) { which in
            NavigationStack {
                ScrollView {
                    VaultCredentialForm(kind: which.rawValue) { updated in
                        resource.value = updated
                        adding = nil
                    }
                    .padding(20)
                }
                .scrollDismissesKeyboard(.interactively)
                .background(CompanionPalette.surface)
                .navigationTitle(which.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("关闭") { adding = nil }
                    }
                }
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
            .tint(CompanionPalette.primary)
        }
        .confirmationDialog("移除这项凭证？", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("移除", role: .destructive) {
                if let item = removing {
                    Task { await resource.change(store, "companion.vault.change", ["action": .string("remove"), "kind": item["kind"], "id": item["id"]]) }
                }
                removing = nil
            }
        }
    }

    private func credential(_ item: JSONValue, kind: String, id: String, detail: String) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(item["label"].nonemptyString ?? id)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("移除", role: .destructive) { removing = .object(["kind": .string(kind), "id": .string(id)]) }
                .disabled(resource.busy).frame(minHeight: 44)
        }
    }
}
