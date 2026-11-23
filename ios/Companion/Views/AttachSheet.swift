import SwiftUI
import Photos
import AVFoundation
import UIKit
import UniformTypeIdentifiers

/// ＋ in the composer, as Muse has it: a strip with the camera and the phone's latest photos and
/// videos (a tap attaches one), 查看全部 for the full picker, then 添加文件 and 添加视频.
@MainActor
struct AttachSheet: View {
    enum Choice { case camera, allPhotos, files, videos }

    var onChoice: (Choice) -> Void
    var onAsset: (PHAsset) -> Void
    @StateObject private var library = RecentPhotos()
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("资源库").font(.headline).foregroundStyle(.secondary)
                Spacer()
                Button("查看全部") { onChoice(.allPhotos) }.foregroundStyle(CompanionPalette.primary)
            }.padding(.horizontal, 24)

            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 10) {
                    Button { onChoice(.camera) } label: {
                        Image(systemName: "camera").font(.system(size: 26))
                            .frame(width: 96, height: 96)
                            .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 22))
                    }.buttonStyle(.plain).accessibilityLabel("拍照或录像")
                    if library.denied {
                        Button {
                            if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                        } label: {
                            Text("允许访问照片后，\n最近的照片会出现在这里").font(.caption).foregroundStyle(.secondary)
                                .multilineTextAlignment(.center).padding(.horizontal, 14).frame(height: 96)
                                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 22))
                        }.buttonStyle(.plain)
                    }
                    ForEach(library.items) { item in
                        Button { onAsset(item.asset) } label: { tile(item) }
                            .buttonStyle(.plain)
                            .accessibilityLabel(item.asset.mediaType == .video ? "最近的视频" : "最近的照片")
                    }
                }.padding(.horizontal, 16)
            }.frame(height: 96)

            VStack(spacing: 0) {
                row("添加文件", icon: "paperclip") { onChoice(.files) }
                Divider().padding(.leading, 60)
                row("添加视频", icon: "play.circle") { onChoice(.videos) }
            }
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 24))
            .padding(.horizontal, 16)
        }
        .padding(.top, 26).padding(.bottom, 10)
        .task { await library.load() }
    }

    private func tile(_ item: RecentPhotos.Item) -> some View {
        ZStack(alignment: .bottomTrailing) {
            Group {
                if let image = item.image { Image(uiImage: image).resizable().scaledToFill() }
                else { Color.primary.opacity(0.07) }
            }.frame(width: 96, height: 96).clipShape(RoundedRectangle(cornerRadius: 22))
            if item.asset.mediaType == .video {
                Text(Self.duration(item.asset.duration)).font(.caption2.weight(.semibold).monospacedDigit())
                    .foregroundStyle(.white).padding(.horizontal, 6).padding(.vertical, 3)
                    .background(.black.opacity(0.45), in: Capsule()).padding(6)
            }
        }
    }

    private func row(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 18) {
                Image(systemName: icon).font(.system(size: 22)).frame(width: 26)
                Text(title).font(.body)
                Spacer()
            }.padding(.horizontal, 18).frame(minHeight: 60).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private static func duration(_ seconds: TimeInterval) -> String {
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

private final class OnceBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Never>?

    init(_ continuation: CheckedContinuation<Value, Never>) {
        self.continuation = continuation
    }

    func resume(returning value: Value) {
        lock.lock()
        let target = continuation
        continuation = nil
        lock.unlock()
        target?.resume(returning: value)
    }
}

/// The phone's newest photos and videos, with small thumbnails for the strip.
@MainActor
final class RecentPhotos: ObservableObject {
    struct Item: Identifiable {
        let id: String
        let asset: PHAsset
        var image: UIImage?
    }

    @Published var items: [Item] = []
    @Published var denied = false
    private let manager = PHCachingImageManager()

    func load() async {
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined { status = await PHPhotoLibrary.requestAuthorization(for: .readWrite) }
        guard status == .authorized || status == .limited else { denied = true; return }
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        options.fetchLimit = 30
        let result = PHAsset.fetchAssets(with: options)
        var list: [Item] = []
        result.enumerateObjects { asset, _, _ in
            if asset.mediaType == .image || asset.mediaType == .video { list.append(Item(id: asset.localIdentifier, asset: asset)) }
        }
        items = list
        let request = PHImageRequestOptions()
        request.deliveryMode = .opportunistic
        request.isNetworkAccessAllowed = true
        for item in list {
            let id = item.id
            manager.requestImage(for: item.asset, targetSize: CGSize(width: 288, height: 288),
                                 contentMode: .aspectFill, options: request) { [weak self] image, _ in
                guard let image else { return }
                Task { @MainActor in
                    guard let self, let index = self.items.firstIndex(where: { $0.id == id }) else { return }
                    self.items[index].image = image
                }
            }
        }
    }

    /// The asset's own bytes, a file name and its type: a photo as stored (HEIC/JPEG…), a video as a file.
    static func file(for asset: PHAsset) async -> (data: Data, name: String, mimeType: String)? {
        if asset.mediaType == .video {
            let options = PHVideoRequestOptions()
            options.isNetworkAccessAllowed = true
            options.version = .current
            options.deliveryMode = .highQualityFormat
            let url: URL? = await withCheckedContinuation { continuation in
                let box = OnceBox(continuation)
                PHImageManager.default().requestAVAsset(forVideo: asset, options: options) { video, _, info in
                    if (info?[PHImageResultIsDegradedKey] as? Bool) == true { return }
                    box.resume(returning: (video as? AVURLAsset)?.url)
                }
            }
            if let url, let data = try? Data(contentsOf: url) {
                let type = UTType(filenameExtension: url.pathExtension) ?? .quickTimeMovie
                return (data, "视频.\(url.pathExtension.isEmpty ? "mov" : url.pathExtension)", type.preferredMIMEType ?? "video/quicktime")
            }
            return await exportVideoResource(for: asset)
        }
        let options = PHImageRequestOptions()
        options.isNetworkAccessAllowed = true
        options.version = .current
        options.deliveryMode = .highQualityFormat
        return await withCheckedContinuation { continuation in
            let box = OnceBox(continuation)
            PHImageManager.default().requestImageDataAndOrientation(for: asset, options: options) { data, uti, _, info in
                if (info?[PHImageResultIsDegradedKey] as? Bool) == true { return }
                guard let data else { box.resume(returning: nil); return }
                let type = uti.flatMap { UTType($0) } ?? .jpeg
                box.resume(returning: (data, "照片.\(type.preferredFilenameExtension ?? "jpg")", type.preferredMIMEType ?? "image/jpeg"))
            }
        }
    }

    /// Exports edited, slow-motion, or Cinematic videos (`AVComposition`) via `PHAssetResourceManager`.
    private static func exportVideoResource(for asset: PHAsset) async -> (data: Data, name: String, mimeType: String)? {
        let resources = PHAssetResource.assetResources(for: asset)
        guard let resource = resources.first(where: { $0.type == .fullSizeVideo })
            ?? resources.first(where: { $0.type == .video })
            ?? resources.first(where: { $0.type == .pairedVideo }) else {
            return nil
        }
        let rawExt = (resource.originalFilename as NSString).pathExtension
        let safeExt = rawExt.filter { $0.isASCII && ($0.isLetter || $0.isNumber) }
        let ext = safeExt.isEmpty ? "mov" : String(safeExt.prefix(10))
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("CompanionPhoto-\(UUID().uuidString).\(ext)")
        defer { try? FileManager.default.removeItem(at: tempURL) }
        let requestOptions = PHAssetResourceRequestOptions()
        requestOptions.isNetworkAccessAllowed = true
        let ok: Bool = await withCheckedContinuation { continuation in
            let box = OnceBox(continuation)
            PHAssetResourceManager.default().writeData(for: resource, toFile: tempURL, options: requestOptions) { error in
                box.resume(returning: error == nil)
            }
        }
        guard ok, let data = try? Data(contentsOf: tempURL) else { return nil }
        let type = UTType(resource.uniformTypeIdentifier) ?? UTType(filenameExtension: ext) ?? .quickTimeMovie
        return (data, "视频.\(ext)", type.preferredMIMEType ?? "video/quicktime")
    }
}
