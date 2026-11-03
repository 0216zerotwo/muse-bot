import SwiftUI
import UIKit
import AVKit
import ImageIO
import Photos
import QuickLook
import UniformTypeIdentifiers

func resolvedURL(_ path: String, baseURL: URL?) -> URL? {
    guard !path.isEmpty, let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
          let scheme = url.scheme?.lowercased(), ["https", "http", "file"].contains(scheme) else { return nil }
    return url
}

@MainActor
private final class PictureCache {
    static let shared = PictureCache()
    let images = NSCache<NSString, UIImage>()
    private init() { images.totalCostLimit = 96 * 1_024 * 1_024 }
}

/// A server or data: picture, decoded at the size it is shown (`maxPixel`, in pixels) off the main
/// thread and kept. A 28-pt face decoded at 2048 px on every chat row made helper chats crawl.
@MainActor
struct RemotePicture: View {
    let path: String
    let baseURL: URL?
    var maxPixel: CGFloat = 1_400
    /// Draw nothing while loading or when it fails (what is underneath shows instead).
    var quiet = false
    @State private var picture: UIImage?
    @State private var failed = false

    private var key: String { "\(Int(maxPixel))|" + (baseURL?.absoluteString ?? "") + "|" + path }

    var body: some View {
        Group {
            if let picture { Image(uiImage: picture).resizable().scaledToFit() }
            else if quiet { Color.clear }
            // A picture that can't load: a small mark, not a big grey placeholder.
            else if failed { Image(systemName: "photo").font(.system(size: maxPixel < 300 ? 14 : 28)).foregroundStyle(.tertiary).frame(maxWidth: .infinity, minHeight: maxPixel < 300 ? 0 : 60) }
            else if maxPixel < 300 { Color.secondary.opacity(0.12) }
            else { ProgressView().frame(maxWidth: .infinity, minHeight: 80) }
        }
        .task(id: key) { await load() }
    }

    private func load() async {
        let key = self.key as NSString
        if let cached = PictureCache.shared.images.object(forKey: key) { picture = cached; return }
        picture = nil; failed = false
        do {
            let data: Data
            if path.hasPrefix("data:image/"), let comma = path.firstIndex(of: ","), path[..<comma].hasSuffix(";base64") {
                guard let decoded = Data(base64Encoded: String(path[path.index(after: comma)...])) else { failed = true; return }
                data = decoded
            } else if let url = resolvedURL(path, baseURL: baseURL) {
                if url.isFileURL { data = try Data(contentsOf: url) }
                else {
                    // Kept on this iPhone after the first time: chats and helpers open with their pictures.
                    guard let bytes = await LocalStore.download(url) else { try Task.checkCancellation(); failed = true; return }
                    data = bytes
                }
            } else { failed = true; return }
            try Task.checkCancellation()
            let size = maxPixel
            guard let decoded = await Task.detached(priority: .userInitiated, operation: { Self.decode(data, maxPixel: size) }).value else {
                failed = true; return
            }
            try Task.checkCancellation()
            PictureCache.shared.images.setObject(decoded.image, forKey: key, cost: decoded.cost)
            picture = decoded.image
        } catch is CancellationError { }
        catch { failed = true }
    }

    private struct Decoded: @unchecked Sendable { let image: UIImage; let cost: Int }

    nonisolated private static func decode(_ data: Data, maxPixel: CGFloat) -> Decoded? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let frame = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: max(64, maxPixel)
              ] as CFDictionary) else { return nil }
        return Decoded(image: UIImage(cgImage: frame), cost: frame.bytesPerRow * frame.height)
    }
}

struct SafariDestination: Identifiable {
    let id = UUID()
    let url: URL
}

@MainActor
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) { }
}

struct MediaDestination: Identifiable {
    let id = UUID()
    let item: MediaAttachment
    let baseURL: URL?
}

/// A picture, clip, sound or file in the chat, each shown as itself: a file by its first page.
struct AttachmentView: View {
    let item: MediaAttachment
    let baseURL: URL?
    var open: () -> Void
    var body: some View {
        if item.kind == "image" {
            Button(action: open) {
                RemotePicture(path: item.url, baseURL: baseURL, maxPixel: 2_048)
                    .frame(maxWidth: 280, maxHeight: 300).clipShape(RoundedRectangle(cornerRadius: 20))
            }.buttonStyle(.plain).accessibilityLabel("打开\(item.name ?? "图片")")
        } else if item.kind == "video" {
            VideoCard(item: item, baseURL: baseURL, open: open)
        } else if AudioCard.plays(item.name ?? item.url) {
            AudioCard(item: item, baseURL: baseURL, open: open)
        } else {
            FileCard(item: item, baseURL: baseURL, open: open)
        }
    }
}

@MainActor
struct MediaViewer: View {
    let destination: MediaDestination
    @Environment(\.dismiss) private var dismiss
    @State private var localURL: URL?
    @State private var image: UIImage?
    @State private var player: AVPlayer?
    @State private var sharing = false
    @State private var busy = false
    @State private var message: String?

    private var item: MediaAttachment { destination.item }
    private var remoteURL: URL? { resolvedURL(item.url, baseURL: destination.baseURL) }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()
                if item.kind == "image", let image { ZoomablePicture(image: image).accessibilityLabel(item.name ?? "图片") }
                else if item.kind == "video", let player { VideoPlayer(player: player).onAppear { player.play() } }
                else if item.kind != "image" && item.kind != "video", let localURL { DocumentPreview(url: localURL) }
                else { ProgressView("正在载入…").tint(.white).foregroundStyle(.white) }
            }
            .navigationTitle(item.name ?? (item.kind == "image" ? "图片" : item.kind == "video" ? "视频" : "文件"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("关闭") { dismiss() }.tint(.white) }
                ToolbarItemGroup(placement: .primaryAction) {
                    if item.kind == "image" || item.kind == "video" {
                        Button { Task { await save() } } label: { Image(systemName: "square.and.arrow.down").frame(minWidth: 36, minHeight: 44) }
                            .accessibilityLabel("保存到照片").disabled(busy)
                    }
                    Button { Task { await share() } } label: { Image(systemName: "square.and.arrow.up").frame(minWidth: 36, minHeight: 44) }
                        .accessibilityLabel("分享或保存文件").disabled(busy)
                }
            }
            .toolbarBackground(.black, for: .navigationBar).toolbarBackground(.visible, for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .overlay(alignment: .bottom) {
                if busy { ProgressView("正在准备…").padding(16).background(.regularMaterial, in: Capsule()).padding() }
            }
        }
        .task { await load() }
        .onDisappear {
            player?.pause()
            if let localURL, localURL.deletingLastPathComponent().deletingLastPathComponent().lastPathComponent == "CompanionMedia" {
                try? FileManager.default.removeItem(at: localURL.deletingLastPathComponent())
            }
        }
        .sheet(isPresented: $sharing) { if let localURL { ShareSheet(items: [localURL]) } }
        .alert("媒体", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("好", role: .cancel) { message = nil }
        } message: { Text(message ?? "") }
    }

    private func load() async {
        if item.kind == "video", let remoteURL { player = AVPlayer(url: remoteURL); return }
        do {
            let file = try await download()
            if item.kind == "video" { player = AVPlayer(url: file) }
            else if item.kind == "image" {
                let data = try Data(contentsOf: file)
                if let source = CGImageSourceCreateWithData(data as CFData, nil), let frame = CGImageSourceCreateImageAtIndex(source, 0, nil) {
                    image = UIImage(cgImage: frame)
                } else { message = "这张图片暂时无法显示，可以尝试分享或保存文件。" }
            }
        } catch is CancellationError { }
        catch { message = "载入失败，请检查连接后重新打开。" }
    }

    private func download() async throws -> URL {
        if let localURL { return localURL }
        let temporary: URL?
        let inline: Data?
        if item.url.hasPrefix("data:"), let comma = item.url.firstIndex(of: ","), item.url[..<comma].hasSuffix(";base64") {
            guard let bytes = Data(base64Encoded: String(item.url[item.url.index(after: comma)...])) else { throw URLError(.cannotDecodeContentData) }
            inline = bytes; temporary = nil
        } else {
            guard let remoteURL else { throw URLError(.badURL) }
            if remoteURL.isFileURL { localURL = remoteURL; return remoteURL }
            // A file's card already keeps it on this iPhone.
            var kept: Data?
            if item.kind == "file" { kept = await LocalStore.download(remoteURL, kind: "files") }
            if let kept { temporary = nil; inline = kept }
            else {
                let (file, response) = try await URLSession.shared.download(from: remoteURL)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                temporary = file; inline = nil
            }
        }
        let ext = (item.name as NSString?)?.pathExtension ?? remoteURL?.pathExtension ?? ""
        // Its own name (what Quick Look shows, and what a shared copy is called), in a folder of its own.
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("CompanionMedia", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let safeExtension = ext.filter { $0.isLetter || $0.isNumber }.prefix(12)
        let base = item.name.map(FileKinds.safeName) ?? (item.kind == "image" ? "图片" : item.kind == "video" ? "视频" : "文件")
        let filename = (base as NSString).pathExtension.isEmpty && !safeExtension.isEmpty ? base + "." + String(safeExtension) : base
        let result = directory.appendingPathComponent(filename)
        if let temporary { try FileManager.default.moveItem(at: temporary, to: result) }
        else if let inline { try inline.write(to: result, options: .atomic) }
        localURL = result
        return result
    }

    private func share() async {
        guard !busy else { return }; busy = true
        defer { busy = false }
        do { _ = try await download(); sharing = true }
        catch { message = "暂时无法准备文件，请稍后重试。" }
    }
    private func save() async {
        guard !busy else { return }; busy = true
        defer { busy = false }
        do {
            let file = try await download()
            let authorization = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
            guard authorization == .authorized || authorization == .limited else { message = "请在系统设置里允许 Muse Bot 添加照片。"; return }
            let isVideo = item.kind == "video"
            try await PHPhotoLibrary.shared().performChanges {
                if isVideo { _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: file) }
                else { _ = PHAssetChangeRequest.creationRequestForAssetFromImage(atFileURL: file) }
            }
            Haptics.selection(); message = "已保存到照片。"
        } catch { message = "保存失败。也可以用分享按钮保存到文件。" }
    }
}

@MainActor
private struct ZoomablePicture: UIViewRepresentable {
    let image: UIImage
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.minimumZoomScale = 1; scroll.maximumZoomScale = 5
        scroll.backgroundColor = .black; scroll.delegate = context.coordinator
        let picture = context.coordinator.picture
        picture.image = image; picture.contentMode = .scaleAspectFit
        picture.autoresizingMask = [.flexibleWidth, .flexibleHeight]; picture.frame = scroll.bounds
        scroll.addSubview(picture)
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTap(_:)))
        tap.numberOfTapsRequired = 2; scroll.addGestureRecognizer(tap)
        return scroll
    }
    func updateUIView(_ scroll: UIScrollView, context: Context) {
        context.coordinator.picture.image = image
        if scroll.zoomScale == 1 { context.coordinator.picture.frame = scroll.bounds }
    }
    final class Coordinator: NSObject, UIScrollViewDelegate {
        let picture = UIImageView()
        func viewForZooming(in scrollView: UIScrollView) -> UIView? { picture }
        @objc func doubleTap(_ recognizer: UITapGestureRecognizer) {
            guard let scroll = recognizer.view as? UIScrollView else { return }
            scroll.setZoomScale(scroll.zoomScale > 1 ? 1 : 3, animated: !UIAccessibility.isReduceMotionEnabled)
        }
    }
}

@MainActor
private struct DocumentPreview: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let preview = QLPreviewController(); preview.dataSource = context.coordinator; return preview
    }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) { }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}

@MainActor
enum AttachmentEncoder {
    /// ImageIO applies EXIF orientation before scaling, including HEIC and WebP input.
    static func draft(data: Data, name: String, mimeType: String) throws -> DraftUpload {
        guard mimeType.hasPrefix("image/") else { return DraftUpload(data: data, mimeType: mimeType, name: name) }
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let frame = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 2_048
              ] as CFDictionary) else { throw URLError(.cannotDecodeContentData) }
        let bitmap = UIImage(cgImage: frame)
        let renderer = UIGraphicsImageRenderer(size: bitmap.size, format: jpegFormat)
        let flattened = renderer.image { context in
            UIColor.white.setFill(); context.fill(CGRect(origin: .zero, size: bitmap.size))
            bitmap.draw(in: CGRect(origin: .zero, size: bitmap.size))
        }
        guard let encoded = flattened.jpegData(compressionQuality: 0.85) else { throw URLError(.cannotDecodeContentData) }
        let filename = (name as NSString).deletingPathExtension + ".jpg"
        return DraftUpload(data: encoded, mimeType: "image/jpeg", name: filename)
    }
    private static var jpegFormat: UIGraphicsImageRendererFormat {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true; return format
    }
}
