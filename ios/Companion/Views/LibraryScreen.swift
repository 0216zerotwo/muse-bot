import AVFoundation
import SwiftUI
import UIKit
import UniformTypeIdentifiers

@MainActor
struct LibraryScreen: View {
    enum Segment: Int, CaseIterable {
        case creations, knowledge
        var label: String { self == .creations ? "作品归档" : "知识库" }
    }

    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = TabResource()
    @State private var segment: Segment = .creations
    @State private var filter = "all"
    @State private var viewing: MediaDestination?

    // Knowledge Vault state
    @State private var knowledge = KnowledgePayload()
    @State private var kbLoaded = false
    @State private var kbBusy = false
    @State private var kbError: String?
    @State private var kbQuery = ""
    @State private var kbCollection = ""
    @State private var kbImporting = false
    @State private var kbUploading = false
    @State private var kbToast: String?
    @State private var selectedHit: KnowledgeSearchHit?
    @State private var deletingDoc: KnowledgeDocument?

    private let filters = [("all", "全部"), ("image", "图片"), ("video", "视频"), ("document", "文档"), ("audio", "音频")]
    private var items: [TabRecord] { TabRecords.items(resource.payload, key: "items").filter { $0["url"].nonemptyString != nil } }
    private var shown: [TabRecord] { items.filter { filter == "all" || $0["kind"].stringValue == filter } }

    private struct DaySection: Identifiable { let day: String; let items: [TabRecord]; var id: String { day } }
    private var byDay: [DaySection] {
        var order: [String] = []
        var groups: [String: [TabRecord]] = [:]
        for item in shown {
            let day = TabRecords.day(item["created_at"])
            if groups[day] == nil { order.append(day) }
            groups[day, default: []].append(item)
        }
        return order.map { DaySection(day: $0, items: groups[$0] ?? []) }
    }

    private var trimmedKBQuery: String { kbQuery.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        applyPresentations(libraryScrollBody)
    }

    private var libraryScrollBody: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                ScreenHeading(
                    title: "资源库",
                    action: segment == .knowledge ? { kbImporting = true; Haptics.selection() } : nil,
                    icon: "plus"
                )
                Picker("资源库分区", selection: $segment) {
                    ForEach(Segment.allCases, id: \.self) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)

                if let kbToast {
                    toastBanner(kbToast)
                }

                if segment == .creations {
                    creationsContent
                } else {
                    knowledgeContent
                }
            }
            .padding(.top, 174)
            .padding(.bottom, 24)
        }
        .refreshable {
            if segment == .creations { await loadCreations() }
            else { await loadKnowledge() }
        }
        .task { await loadCreations() }
        .onChange(of: segment) { _, next in
            if next == .knowledge && !kbLoaded {
                Task { await loadKnowledge() }
            }
        }
        .task(id: "\(segment == .knowledge)|\(kbCollection)|\(kbQuery)") {
            guard segment == .knowledge else { return }
            if !trimmedKBQuery.isEmpty {
                do { try await Task.sleep(for: .milliseconds(300)) } catch { return }
            }
            await loadKnowledge()
        }
    }

    private func toastBanner(_ text: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(CompanionPalette.primary)
            Text(text)
                .font(.subheadline)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button { self.kbToast = nil } label: {
                Image(systemName: "xmark").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }.buttonStyle(.plain)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(CompanionPalette.border))
        .padding(.horizontal, 16)
    }

    private func applyPresentations<Content: View>(_ content: Content) -> some View {
        content
            .fullScreenCover(item: $viewing) { MediaViewer(destination: $0) }
            .sheet(item: $selectedHit) { hit in
                KnowledgeHitDetailSheet(hit: hit, query: trimmedKBQuery) { prompt in
                    store.composeDraft(prompt)
                    selectedHit = nil
                }
            }
            .fileImporter(
                isPresented: $kbImporting,
                allowedContentTypes: [.pdf, .plainText, .html, .json, .sourceCode, .spreadsheet, .presentation, .data, .item],
                allowsMultipleSelection: true
            ) { result in
                Task { await importKnowledgeFiles(result) }
            }
            .confirmationDialog(
                "从知识库删除「\(deletingDoc?.title ?? "")」？",
                isPresented: Binding(get: { deletingDoc != nil }, set: { if !$0 { deletingDoc = nil } }),
                titleVisibility: .visible
            ) {
                Button("删除", role: .destructive) {
                    if let doc = deletingDoc {
                        deletingDoc = nil
                        Task { await deleteKnowledge(doc) }
                    }
                }
            } message: {
                Text("删除后该文档的所有索引片段将被移除，无法撤销。")
            }
    }

    // MARK: - 作品归档

    @ViewBuilder private var creationsContent: some View {
        let all = items
        let days = byDay
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(filters.filter { $0.0 != "audio" || (resource.payload["counts"]["audio"].intValue ?? 0) > 0 }, id: \.0) { option in
                    let count = option.0 == "all" ? all.count : (resource.payload["counts"][option.0].intValue ?? 0)
                    Button { filter = option.0; Haptics.selection() } label: {
                        HStack(spacing: 4) { Text(option.1); if count > 0 { Text("\(count)").opacity(0.6) } }
                            .font(.system(size: 14)).padding(.horizontal, 15).frame(minHeight: 38)
                            .foregroundStyle(filter == option.0 ? CompanionPalette.background : Color.primary)
                            .background(filter == option.0 ? Color.primary : CompanionPalette.surface, in: Capsule())
                    }.buttonStyle(.plain).accessibilityAddTraits(filter == option.0 ? .isSelected : [])
                }
            }.padding(.horizontal, 16)
        }
        if let error = resource.error { ResourceErrorView(message: error, retry: { Task { await loadCreations() } }) }
        if resource.busy && !resource.hasLoaded { ProgressView("正在读取…").frame(maxWidth: .infinity).padding(20) }
        if resource.hasLoaded && days.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "square.grid.2x2").font(.system(size: 44)).foregroundStyle(.secondary)
                Text("还没有作品").font(.headline)
                Text("做好的图片、视频和文档都在这里").foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity).padding(32)
        }
        ForEach(days) { section in
            let day = section.day
            let group = section.items
            let mediaItems = group.filter {
                let kind = $0["kind"].stringValue
                let name = $0["name"].nonemptyString ?? $0.title
                let url = $0["url"].stringValue
                return (kind == "image" || kind == "video") && !FileKinds.isSVG(name: name, url: url)
            }
            let docItems = group.filter { item in
                !mediaItems.contains { $0.id == item.id }
            }
            VStack(alignment: .leading, spacing: 8) {
                Text(day).font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary).padding(.horizontal, 20)
                if !mediaItems.isEmpty {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 3), spacing: 4) {
                        ForEach(mediaItems) { item in
                            Button { open(item) } label: {
                                GeometryReader { geometry in
                                    ZStack(alignment: .bottomLeading) {
                                        CompanionPalette.surface
                                        if item["kind"].stringValue == "image" {
                                            RemotePicture(path: item["url"].stringValue + "?thumb=1", baseURL: store.baseURL, maxPixel: 450)
                                                .frame(width: geometry.size.width, height: geometry.size.height).clipped()
                                        } else {
                                            LibraryVideoThumbnail(path: item["url"].stringValue, baseURL: store.baseURL)
                                            Label(item.title, systemImage: "play.fill").font(.system(size: 10)).lineLimit(2).foregroundStyle(.white)
                                                .padding(6).frame(maxWidth: .infinity, alignment: .leading).background(.black.opacity(0.5))
                                        }
                                    }.clipShape(RoundedRectangle(cornerRadius: 7))
                                }.aspectRatio(1, contentMode: .fit)
                            }.buttonStyle(.plain).accessibilityLabel("打开\(item.title)").contextMenu { creationMenu(item) }
                        }
                    }.padding(.horizontal, 16)
                }
                ForEach(docItems) { item in
                    let name = item["name"].nonemptyString ?? item.title
                    let kind = FileKind.of(name)
                    Button { open(item) } label: {
                        HStack(spacing: 12) {
                            FileBadge(name: name, kind: kind, size: 34)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.title).font(.system(size: 15, weight: .medium)).lineLimit(2)
                                Text(FileKinds.subtitle(kind, pages: nil, bytes: item["size"].intValue)).font(.caption).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }.padding(12).background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                    }.buttonStyle(.plain).contextMenu { creationMenu(item) }.padding(.horizontal, 16)
                }
            }
        }
    }

    // MARK: - 知识库 (Knowledge Vault)

    @ViewBuilder private var knowledgeContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            // Stats + upload bar
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(knowledge.stats.docCount) 篇文档 · \(knowledge.stats.chunkCount) 个片段")
                        .font(.system(size: 15, weight: .semibold))
                    Text(knowledge.stats.totalBytes > 0
                         ? "已索引 " + ByteCountFormatter.string(fromByteCount: Int64(knowledge.stats.totalBytes), countStyle: .file)
                         : "上传 PDF、Markdown、Office 或代码文档")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Button {
                    kbImporting = true
                    Haptics.selection()
                } label: {
                    HStack(spacing: 5) {
                        if kbUploading { ProgressView().controlSize(.mini).tint(CompanionPalette.primaryText) }
                        else { Image(systemName: "doc.badge.plus").font(.system(size: 13, weight: .semibold)) }
                        Text(kbUploading ? "导入中…" : "导入文档")
                            .font(.system(size: 14, weight: .semibold))
                    }
                    .padding(.horizontal, 14)
                    .frame(minHeight: 38)
                    .background(CompanionPalette.primary, in: Capsule())
                    .foregroundStyle(CompanionPalette.primaryText)
                }
                .buttonStyle(.plain)
                .disabled(kbUploading || !store.connected)
            }
            .padding(14)
            .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(CompanionPalette.border))
            .padding(.horizontal, 16)

            // Search bar
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索知识库片段、标题或关键词", text: $kbQuery)
                    .submitLabel(.search)
                    .accessibilityLabel("搜索知识库")
                if kbBusy && kbLoaded {
                    ProgressView().controlSize(.mini)
                } else if !kbQuery.isEmpty {
                    Button { kbQuery = "" } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary)
                    }.buttonStyle(.plain).accessibilityLabel("清除搜索")
                }
            }
            .padding(.horizontal, 14)
            .frame(minHeight: 42)
            .background(CompanionPalette.surface, in: Capsule())
            .overlay(Capsule().strokeBorder(CompanionPalette.border))
            .padding(.horizontal, 16)

            // Collection filter pills
            if !knowledge.collections.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        let totalAll = knowledge.collectionCounts.values.reduce(0, +)
                        collectionChip(title: "全部", value: "", count: totalAll > 0 ? totalAll : knowledge.stats.docCount)
                        ForEach(knowledge.collections, id: \.self) { col in
                            collectionChip(title: col, value: col, count: knowledge.collectionCounts[col] ?? 0)
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }

            if let kbError {
                ResourceErrorView(message: kbError) { Task { await loadKnowledge() } }
            } else if kbBusy && !kbLoaded {
                ProgressView("正在读取知识库…").frame(maxWidth: .infinity).padding(20)
            } else if !trimmedKBQuery.isEmpty {
                knowledgeHitsList
            } else {
                knowledgeDocumentsList
            }
        }
    }

    private func collectionChip(title: String, value: String, count: Int) -> some View {
        let active = kbCollection == value
        return Button {
            kbCollection = value
            Haptics.selection()
        } label: {
            HStack(spacing: 4) {
                Text(title)
                if count > 0 { Text("\(count)").opacity(0.6) }
            }
            .font(.system(size: 14))
            .padding(.horizontal, 14)
            .frame(minHeight: 36)
            .foregroundStyle(active ? CompanionPalette.background : Color.primary)
            .background(active ? Color.primary : CompanionPalette.surface, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }

    @ViewBuilder private var knowledgeHitsList: some View {
        let hits = knowledge.hits ?? []
        if hits.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "magnifyingglass").font(.system(size: 36)).foregroundStyle(.secondary)
                Text("没有找到与「\(trimmedKBQuery)」相关的知识片段").font(.subheadline).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(28)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text("命中 \(hits.count) 个知识片段")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 20)
                ForEach(hits) { hit in
                    Button { selectedHit = hit; Haptics.selection() } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 6) {
                                Image(systemName: "books.vertical.fill")
                                    .font(.caption)
                                    .foregroundStyle(CompanionPalette.primary)
                                Text(hit.title)
                                    .font(.system(size: 15, weight: .semibold))
                                    .lineLimit(1)
                                Spacer(minLength: 4)
                                Text(hit.collection)
                                    .font(.caption2.weight(.medium))
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 8).padding(.vertical, 3)
                                    .background(CompanionPalette.elevated, in: Capsule())
                            }
                            if !hit.heading.isEmpty {
                                Text(hit.heading)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            Text(highlightedSnippet(hit.snippet, matches: hit.matches))
                                .font(.system(size: 14))
                                .lineLimit(4)
                                .multilineTextAlignment(.leading)
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(CompanionPalette.border))
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 16)
                }
            }
        }
    }

    @ViewBuilder private var knowledgeDocumentsList: some View {
        if knowledge.documents.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "books.vertical").font(.system(size: 44)).foregroundStyle(.secondary)
                Text("知识库还是空的").font(.headline)
                Text("导入 PDF、Markdown、Office 或代码文档后，伙伴在回答问题时会自动检索并附带引用。")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 300)
            }
            .frame(maxWidth: .infinity)
            .padding(32)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(knowledge.documents) { doc in
                    let kind = FileKind.of(doc.filename)
                    HStack(spacing: 12) {
                        FileBadge(name: doc.filename, kind: kind, size: 34)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(doc.title).font(.system(size: 15, weight: .semibold)).lineLimit(2)
                            HStack(spacing: 6) {
                                Text(doc.collection)
                                    .font(.caption2.weight(.medium))
                                    .padding(.horizontal, 7).padding(.vertical, 2)
                                    .background(CompanionPalette.elevated, in: Capsule())
                                Text("\(doc.chunkCount) 片段 · \(ByteCountFormatter.string(fromByteCount: Int64(doc.sizeBytes), countStyle: .file))")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Text("\(doc.originLabel) · \(formatDocDate(doc.updatedAt))")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)

                        Menu {
                            Button("在对话中基于此文档提问", systemImage: "bubble.left.and.text.bubble.right") {
                                store.composeDraft("请结合知识库里的《\(doc.title)》帮我总结核心要点")
                                Haptics.selection()
                            }
                            Button("删除文档", systemImage: "trash", role: .destructive) {
                                deletingDoc = doc
                            }
                        } label: {
                            Image(systemName: "ellipsis")
                                .font(.system(size: 16, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .frame(width: 36, height: 40)
                                .contentShape(Rectangle())
                        }
                        .accessibilityLabel("更多操作：\(doc.title)")
                    }
                    .padding(12)
                    .background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(CompanionPalette.border))
                    .contextMenu {
                        Button("在对话中提问", systemImage: "bubble.left.and.text.bubble.right") {
                            store.composeDraft("请结合知识库里的《\(doc.title)》帮我总结核心要点")
                        }
                        Button("删除文档", systemImage: "trash", role: .destructive) {
                            deletingDoc = doc
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }
        }
    }

    // MARK: - Actions & Helpers

    private func loadCreations() async {
        await resource.load("/api/webui/companion/creations", store: store)
    }

    private func loadKnowledge() async {
        kbBusy = true
        kbError = nil
        defer { kbBusy = false }
        do {
            let col = kbCollection.isEmpty ? nil : kbCollection
            let q = trimmedKBQuery.isEmpty ? nil : trimmedKBQuery
            knowledge = try await store.fetchKnowledge(collection: col, query: q)
            kbLoaded = true
        } catch is CancellationError { }
        catch {
            kbError = error.localizedDescription
        }
    }

    private func open(_ item: TabRecord) {
        let name = item["name"].nonemptyString ?? item.title
        viewing = MediaDestination(
            item: MediaAttachment(url: item["url"].stringValue, name: name, kind: item["kind"].stringValue),
            baseURL: store.baseURL,
            creationID: item.id
        )
    }

    @ViewBuilder private func creationMenu(_ item: TabRecord) -> some View {
        let name = item["name"].nonemptyString ?? item.title
        let url = item["url"].stringValue
        if store.connected && FileKinds.canImportToKnowledge(name: name, url: url) {
            Button("加入知识库", systemImage: "books.vertical.fill") {
                Task { await importCreation(item) }
            }
        }
        Button("从资源库移除", systemImage: "trash", role: .destructive) {
            Task { await resource.change("companion.creations.hide", payload: ["id": .string(item.id)], store: store) }
        }
    }

    private func importCreation(_ item: TabRecord) async {
        do {
            let result = try await store.importCreationToKnowledge(creationID: item.id)
            knowledge = result.payload
            kbLoaded = true
            Haptics.selection()
            showToast(result.deduplicated ? "「\(item.title)」已在知识库中" : "已将「\(item.title)」加入知识库")
        } catch {
            kbError = error.localizedDescription
        }
    }

    private func importKnowledgeFiles(_ result: Result<[URL], Error>) async {
        kbUploading = true
        kbError = nil
        defer { kbUploading = false }
        do {
            let urls = try result.get()
            guard !urls.isEmpty else { return }
            let targetCol = kbCollection.isEmpty ? "默认" : kbCollection
            var importedCount = 0
            var dedupCount = 0
            for url in urls {
                let accessed = url.startAccessingSecurityScopedResource()
                defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                let bytes = try Data(contentsOf: url)
                let res = try await store.uploadKnowledgeFile(
                    data: bytes,
                    filename: url.lastPathComponent,
                    collection: targetCol
                )
                knowledge = res.payload
                kbLoaded = true
                if res.deduplicated { dedupCount += 1 }
                else { importedCount += 1 }
            }
            if !kbCollection.isEmpty || !trimmedKBQuery.isEmpty {
                await loadKnowledge()
            }
            Haptics.selection()
            if importedCount > 0 && dedupCount > 0 {
                showToast("已导入 \(importedCount) 篇文档（另有 \(dedupCount) 篇已存在）")
            } else if importedCount > 0 {
                showToast("已成功导入 \(importedCount) 篇文档到知识库")
            } else {
                showToast("所选文档已在知识库中，已同步刷新")
            }
        } catch is CancellationError { }
        catch {
            kbError = error.localizedDescription
        }
    }

    private func deleteKnowledge(_ doc: KnowledgeDocument) async {
        kbBusy = true
        kbError = nil
        defer { kbBusy = false }
        do {
            let col = kbCollection.isEmpty ? nil : kbCollection
            knowledge = try await store.deleteKnowledgeDocument(id: doc.id, listCollection: col)
            if !trimmedKBQuery.isEmpty {
                await loadKnowledge()
            }
            Haptics.selection()
            showToast("已删除「\(doc.title)」")
        } catch {
            kbError = error.localizedDescription
        }
    }

    private func showToast(_ text: String) {
        kbToast = text
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(3.5))
            if kbToast == text { kbToast = nil }
        }
    }

    private func highlightedSnippet(_ text: String, matches: [[Int]]) -> AttributedString {
        SearchHighlight.styled(text, matches: matches, fallbackQuery: trimmedKBQuery)
    }

    private func formatDocDate(_ seconds: Double) -> String {
        guard seconds > 0 else { return "" }
        let date = Date(timeIntervalSince1970: seconds)
        if Calendar.current.isDateInToday(date) { return "今天 " + date.formatted(date: .omitted, time: .shortened) }
        return date.formatted(.dateTime.month().day())
    }
}

@MainActor
private struct KnowledgeHitDetailSheet: View {
    let hit: KnowledgeSearchHit
    let query: String
    var onAsk: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 8) {
                        Label(hit.collection, systemImage: "books.vertical.fill")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(CompanionPalette.primary)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(CompanionPalette.primary.opacity(0.12), in: Capsule())
                        Text("片段 #\(hit.chunkIndex + 1)")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    Text(hit.title).font(.title3.weight(.semibold))
                    if !hit.heading.isEmpty {
                        Text(hit.heading).font(.subheadline).foregroundStyle(.secondary)
                    }
                    Divider()
                    Text(hit.content.isEmpty ? hit.snippet : hit.content)
                        .font(.body)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(20)
            }
            .background(CompanionPalette.background)
            .navigationTitle("知识片段")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() } }
                ToolbarItemGroup(placement: .primaryAction) {
                    Button {
                        UIPasteboard.general.string = hit.content.isEmpty ? hit.snippet : hit.content
                        Haptics.selection()
                        copied = true
                        Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                    } label: {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                    }
                    .accessibilityLabel("复制片段")
                    Button("去提问") {
                        let base = query.isEmpty ? "请结合《\(hit.title)》的这段内容帮我解读" : "关于「\(query)」，请结合知识库《\(hit.title)》详细说明"
                        onAsk(base)
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
    }
}

@MainActor
private struct LibraryVideoThumbnail: View {
    let path: String
    let baseURL: URL?
    @State private var image: UIImage?
    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: "play.rectangle").font(.largeTitle).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity) }
        }.clipped().task(id: path) {
            guard let url = resolvedURL(path, baseURL: baseURL) else { return }
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 360, height: 360)
            do { let result = try await generator.image(at: CMTime(seconds: 0.1, preferredTimescale: 600)); image = UIImage(cgImage: result.image) }
            catch { }
        }
    }
}
