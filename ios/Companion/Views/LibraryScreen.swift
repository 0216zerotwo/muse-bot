import SwiftUI
import AVFoundation
import UIKit

@MainActor
struct LibraryScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = TabResource()
    @State private var filter = "all"
    @State private var viewing: MediaDestination?
    private let filters = [("all", "全部"), ("image", "图片"), ("video", "视频"), ("document", "文档"), ("audio", "音频")]
    private var items: [TabRecord] { TabRecords.items(resource.payload, key: "items").filter { $0["url"].nonemptyString != nil } }
    private var shown: [TabRecord] { items.filter { filter == "all" || $0["kind"].stringValue == filter } }
    /// The shown items by day, newest day first (worked out once per redraw).
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

    var body: some View {
        ScrollView {
            let all = items
            let days = byDay
            LazyVStack(alignment: .leading, spacing: 16) {  // thumbnails are made as they scroll in
                ScreenHeading(title: "资源库")
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
                if let error = resource.error { ResourceErrorView(message: error, retry: { Task { await load() } }) }
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
                    VStack(alignment: .leading, spacing: 8) {
                        Text(day).font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary).padding(.horizontal, 20)
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 3), spacing: 4) {
                            ForEach(group.filter { ["image", "video"].contains($0["kind"].stringValue) }) { item in
                                Button { open(item) } label: {
                                    GeometryReader { geometry in
                                        ZStack(alignment: .bottomLeading) {
                                            CompanionPalette.surface
                                            if item["kind"].stringValue == "image" {
                                                RemotePicture(path: item["url"].stringValue + "?thumb=1", baseURL: store.baseURL, maxPixel: 450).frame(width: geometry.size.width, height: geometry.size.height).clipped()
                                            } else {
                                                LibraryVideoThumbnail(path: item["url"].stringValue, baseURL: store.baseURL)
                                                Label(item.title, systemImage: "play.fill").font(.system(size: 10)).lineLimit(2).foregroundStyle(.white).padding(6).frame(maxWidth: .infinity, alignment: .leading).background(.black.opacity(0.5))
                                            }
                                        }.clipShape(RoundedRectangle(cornerRadius: 7))
                                    }.aspectRatio(1, contentMode: .fit)
                                }.buttonStyle(.plain).accessibilityLabel("打开\(item.title)").contextMenu { hideButton(item) }
                            }
                        }.padding(.horizontal, 16)
                        ForEach(group.filter { !["image", "video"].contains($0["kind"].stringValue) }) { item in
                            Button { open(item) } label: {
                                HStack(spacing: 12) {
                                    let name = item["name"].nonemptyString ?? item.title
                                    FileBadge(name: name, kind: FileKind.of(name), size: 34)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(item.title).font(.system(size: 15, weight: .medium)).lineLimit(2)
                                        Text(FileKinds.subtitle(FileKind.of(name), pages: nil, bytes: item["size"].intValue)).font(.caption).foregroundStyle(.secondary)
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                }.padding(12).background(CompanionPalette.surface, in: RoundedRectangle(cornerRadius: 16))
                            }.buttonStyle(.plain).contextMenu { hideButton(item) }.padding(.horizontal, 16)
                        }
                    }
                }
            }.padding(.top, 174).padding(.bottom, 24)
        }.refreshable { await load() }.task { await load() }
            .fullScreenCover(item: $viewing) { MediaViewer(destination: $0) }
    }
    private func load() async { await resource.load("/api/webui/companion/creations", store: store) }
    private func open(_ item: TabRecord) {
        viewing = MediaDestination(item: MediaAttachment(url: item["url"].stringValue, name: item["name"].nonemptyString ?? item.title, kind: item["kind"].stringValue), baseURL: store.baseURL)
    }
    private func hideButton(_ item: TabRecord) -> some View {
        Button("从资源库移除", role: .destructive) { Task { await resource.change("companion.creations.hide", payload: ["id": .string(item.id)], store: store) } }
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
