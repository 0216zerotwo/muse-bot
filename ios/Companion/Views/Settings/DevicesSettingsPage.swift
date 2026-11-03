import SwiftUI
import UIKit

@MainActor
struct DevicesSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/devices")
    @State private var code: JSONValue = .null
    @State private var generating = false
    @State private var removing: String?
    var body: some View {
        Form {
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section("已连接的设备") {
                ForEach(SettingsDecoding.rows(resource.value, key: "devices", id: "id"), id: \.selfID) { device in
                    let mine = device["id"].stringValue == store.currentDeviceID
                    HStack(spacing: 12) {
                        Image(systemName: device["name"].stringValue.contains("电脑") ? "laptopcomputer" : "iphone").foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(device["name"].stringValue + (mine ? " · 这台" : ""))
                            Text("连接于 \(SettingsDecoding.date(device["paired_at"])) · 最近 \(SettingsDecoding.date(device["last_seen"]))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if !mine { Button("移除", role: .destructive) { removing = device["id"].stringValue }.disabled(resource.busy).frame(minHeight: 44) }
                    }
                }
                if resource.value != .null && resource.value["devices"].array.isEmpty { Text("这台是服务器本机，不需要配对。").foregroundStyle(.secondary) }
                Text("每台设备只需配对一次。移除后，那台设备要重新配对才能再连上。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section("添加新设备") {
                if let value = code["code"].nonemptyString {
                    Text(value).font(.title.monospaced()).textSelection(.enabled)
                    Text("\(SettingsDecoding.date(code["expires_at"])) 前有效，只能用一次。").font(.footnote).foregroundStyle(.secondary)
                    Button("复制连接链接") { UIPasteboard.general.string = code["link"].stringValue; Haptics.selection() }
                    Button("配好了，刷新列表") { Task { await resource.load(store) } }
                }
                Button {
                    generating = true; resource.error = nil
                    Task {
                        do { code = try await store.postResource("/api/pair/code", payload: [:]) }
                        catch { resource.error = error.localizedDescription }
                        generating = false
                    }
                } label: { HStack { Spacer(); if generating { ProgressView() } else { Text(code == .null ? "生成配对码" : "重新生成配对码") }; Spacer() } }
                    .buttonStyle(.borderedProminent).disabled(generating).frame(minHeight: 44)
            }
        }.settingsStyle("设备").task { await resource.load(store) }.refreshable { await resource.load(store) }
            .confirmationDialog("移除这台设备？", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
                Button("移除", role: .destructive) {
                    if let id = removing { Task { await resource.change(store, "companion.devices.remove", ["id": .string(id)]) } }
                    removing = nil
                }
            }
    }
}
