import CryptoKit
import Foundation
import UIKit

/// What this iPhone keeps of a server's data so it shows at once: chats, pictures and clips, card
/// pictures, each tab's last lists. Kept under Caches — never in iCloud backups, and iOS may clear it
/// when space runs short (then it is simply fetched again) — one folder per server, each file
/// readable only while the phone is unlocked. Keys, tokens and sign-ins are never kept here.
enum LocalStore {
    static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    static var root: URL? {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appendingPathComponent("CompanionLocal", isDirectory: true)
    }

    /// `http://host:port` of an address or link: a server's chats and its pictures share one folder.
    static func origin(_ address: String) -> String {
        guard var parts = URLComponents(string: address), parts.host?.isEmpty == false else { return address }
        parts.scheme = parts.scheme?.lowercased()
        parts.host = parts.host?.lowercased()
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.url?.absoluteString ?? address
    }

    /// `<root>/<server>/<kind>/<name>` (server and name hashed: no addresses or titles in file names).
    static func url(_ kind: String, server: String, name: String) -> URL? {
        guard let root, !server.isEmpty, !name.isEmpty else { return nil }
        return root.appendingPathComponent(digest(origin(server)), isDirectory: true)
            .appendingPathComponent(kind, isDirectory: true)
            .appendingPathComponent(digest(name))
    }

    static func read(_ url: URL?) -> Data? {
        guard let url else { return nil }
        return try? Data(contentsOf: url)
    }

    static func write(_ data: Data, to url: URL?) {
        guard let url else { return }
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch { /* only a head start for next time */ }
    }

    static func remove(_ url: URL?) {
        guard let url else { return }
        try? FileManager.default.removeItem(at: url)
    }

    /// A server file (picture, clip) from this iPhone when kept, otherwise downloaded and kept. Signed
    /// links name one file for good (a changed picture gets a new link), so the link is the key.
    static func download(_ url: URL, kind: String = "media") async -> Data? {
        let file = Self.url(kind, server: url.absoluteString, name: url.absoluteString)
        if let kept = await Task.detached(priority: .userInitiated, operation: { LocalStore.read(file) }).value { return kept }
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? false else { return nil }
        Task.detached(priority: .utility) { LocalStore.write(data, to: file) }
        return data
    }

    private struct Picture: @unchecked Sendable { let image: UIImage }

    /// Keeps a picture (a card's last look) as PNG, encoded off the main thread.
    static func keep(_ image: UIImage, to url: URL?) {
        guard let url else { return }
        let picture = Picture(image: image)
        Task.detached(priority: .utility) {
            if let data = picture.image.pngData() { LocalStore.write(data, to: url) }
        }
    }

    /// A kept picture at the screen's scale (read and decoded off the main thread).
    static func picture(_ url: URL?, scale: CGFloat) async -> UIImage? {
        guard let url else { return nil }
        return await Task.detached(priority: .userInitiated) { () -> Picture? in
            guard let data = LocalStore.read(url), let image = UIImage(data: data, scale: scale), let ready = image.preparingForDisplay() else { return nil }
            return Picture(image: ready)
        }.value?.image
    }

    /// Everything kept for one server (this iPhone was removed from it, so its copy goes too).
    static func removeServer(_ server: String) {
        guard let root, !server.isEmpty else { return }
        try? FileManager.default.removeItem(at: root.appendingPathComponent(digest(origin(server)), isDirectory: true))
    }

    /// Keeps pictures and clips under `limit` bytes, dropping the oldest first (chats are small and
    /// stay). Run off the main thread at launch.
    static func trim(limit: Int64 = 400 * 1_024 * 1_024) {
        guard let root, let files = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey]) else { return }
        var media: [(url: URL, size: Int64, date: Date)] = []
        var total: Int64 = 0
        for case let file as URL in files where file.deletingLastPathComponent().lastPathComponent != "chats" {
            guard let values = try? file.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey]),
                  let size = values.fileSize else { continue }
            media.append((file, Int64(size), values.contentModificationDate ?? .distantPast))
            total += Int64(size)
        }
        guard total > limit else { return }
        for item in media.sorted(by: { $0.date < $1.date }) where total > limit {
            try? FileManager.default.removeItem(at: item.url)
            total -= item.size
        }
    }

    /// 清除这台 iPhone 上的缓存: everything kept for every server.
    static func clearAll() {
        guard let root else { return }
        try? FileManager.default.removeItem(at: root)
    }

    /// How much is kept, for the settings row.
    static func size() -> Int64 {
        guard let root, let files = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let file as URL in files {
            total += Int64((try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        return total
    }
}
