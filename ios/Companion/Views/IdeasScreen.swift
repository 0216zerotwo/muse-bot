import SwiftUI

@MainActor
struct IdeasScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = TabResource()
    var onChat: () -> Void = {}
    private var ideas: [TabRecord] { TabRecords.items(resource.payload, key: "ideas", identity: "title") }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ScreenHeading(title: "点子", action: { Task { await refresh() } }, icon: "arrow.clockwise")
                if let error = resource.error { ResourceErrorView(message: error, retry: { Task { await load() } }) }
                if resource.busy { ProgressView("正在想点子…").frame(maxWidth: .infinity).padding(24) }
                if resource.hasLoaded && ideas.isEmpty { Text("暂时没有点子，点右上角换一批。").foregroundStyle(.secondary).padding(20) }
                ForEach(ideas) { idea in
                    Button {
                        Task {
                            await store.changeChat(store.mainChatID)
                            if store.send(text: idea["prompt"].nonemptyString ?? idea.title) { onChat() }
                        }
                    } label: {
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(idea.title).font(.system(size: 17, weight: .semibold))
                                if let reason = idea["reason"].nonemptyString { Text(reason).font(.system(size: 15)).foregroundStyle(.secondary) }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Text("去做").font(.system(size: 14, weight: .semibold)).foregroundStyle(CompanionPalette.primary)
                                .padding(.horizontal, 15).padding(.vertical, 9).background(CompanionPalette.elevated, in: Capsule())
                        }.padding(.horizontal, 20).padding(.vertical, 17).contentShape(Rectangle())
                    }.buttonStyle(.plain).disabled(resource.busy)
                    Divider().overlay(CompanionPalette.border)
                }
            }.padding(.top, 174).padding(.bottom, 24)
        }.refreshable { await load() }.task { await load() }
    }
    private func load() async { await resource.load("/api/webui/companion/ideas", store: store) }
    private func refresh() async { await resource.change("companion.ideas.refresh", payload: [:], store: store) }
}
