import SwiftUI
import UIKit
import ImageIO

/// The same look and per-state clips as Pet.tsx, drawn entirely with native images.
@MainActor
struct AvatarView: View {
    let value: String?
    var library: [JSONValue]? = nil
    var animation: String = "idle"
    let size: CGFloat
    let baseURL: URL?
    var onAccent: ((Color) -> Void)? = nil

    @Environment(\.accessibilityReduceMotion) private var reducedMotion
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var player = AvatarPlayer()

    private var scope: String { baseURL?.absoluteString ?? "unconfigured" }
    private var remembered: JSONValue? {
        guard let data = UserDefaults.standard.data(forKey: "avatar.look.\(scope)") else { return nil }
        return try? JSONDecoder().decode(JSONValue.self, from: data)
    }
    private var lookValue: String? {
        value ?? UserDefaults.standard.string(forKey: "avatar.value.\(scope)")
    }
    private var source: JSONValue? {
        guard let look = lookValue else { return nil }
        let id = String(look.split(separator: ":", maxSplits: 1).last ?? "")
        if look.hasPrefix("image:") || look.hasPrefix("custom:") {
            return library?.first(where: { $0["id"].stringValue == id })
                ?? (remembered?["id"].stringValue == id ? remembered : nil)
        }
        return nil
    }
    private var isBuiltIn: Bool {
        guard let look = lookValue else { return false }
        return look.isEmpty || look == "🐱" || look == "pet:orange-cat"
            || (look.hasPrefix("pet:") && look != "none")
    }
    /// What the picture depends on. (Links carry the file's version, so a remade clip still counts.)
    private var loadKey: String {
        let files = [source?["image_url"].stringValue ?? ""] + (source?["motions"].object.values.compactMap(\.string).sorted() ?? [])
        return "\(scope)|\(lookValue ?? "none")|\(animation)|\(reducedMotion)|\(files.joined(separator: ","))"
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            if let look = lookValue, look.hasPrefix("mochi:") {
                MochiFace(variant: String(look.dropFirst(6)))
                    .frame(width: size, height: size)
            } else if !player.frames.isEmpty {
                // A still (one frame) needs no clock ticking ~16 times a second.
                TimelineView(.animation(minimumInterval: 0.06, paused: reducedMotion || scenePhase != .active || player.frames.count < 2)) { context in
                    Image(uiImage: player.frame(at: context.date, reducedMotion: reducedMotion))
                        .resizable()
                        .interpolation(player.isSprite ? .none : .high)
                        .aspectRatio(contentMode: .fit)
                        .frame(width: size, height: size * 208 / 192)
                }
            } else if let look = lookValue,
                      !look.contains(":"), !isBuiltIn, look != "none" {
                Text(look).font(.system(size: size * 0.55))
                    .frame(width: size, height: size)
            } else {
                Color.clear.frame(width: size, height: size * 208 / 192)
            }
            if player.needsBadge, let symbol = Self.badges[animation] {
                Image(systemName: symbol)
                    .font(.system(size: max(10, size * 0.18), weight: .semibold))
                    .padding(5).background(.regularMaterial, in: Circle())
            }
        }
        .frame(width: size, height: size * 208 / 192)
        .accessibilityHidden(true)
        .task(id: loadKey) {
            if !player.frames.isEmpty {
                // Tool steps flip the state many times a minute: only a state that lasts gets its clip.
                try? await Task.sleep(for: .milliseconds(800))
                if Task.isCancelled { return }
            }
            if let components = UserDefaults.standard.array(forKey: "avatar.accent.\(scope)") as? [Double],
               components.count == 3 {
                onAccent?(Color(red: components[0], green: components[1], blue: components[2]))
            }
            if let look = value {
                UserDefaults.standard.set(look, forKey: "avatar.value.\(scope)")
            }
            if let source, let encoded = try? JSONEncoder().encode(source) {
                UserDefaults.standard.set(encoded, forKey: "avatar.look.\(scope)")
            }
            if isBuiltIn {
                publishAccent(UIColor(red: 224 / 255, green: 122 / 255, blue: 53 / 255, alpha: 1))
                await player.loadBuiltIn(animation: reducedMotion ? "idle" : animation)
            } else if let source {
                await player.load(source: source, animation: reducedMotion ? "idle" : animation, baseURL: baseURL)
                if !Task.isCancelled, let accent = player.accent { publishAccent(accent) }
            } else if let look = lookValue, look.hasPrefix("mochi:") {
                publishAccent(MochiFace.palette(String(look.dropFirst(6))).1)
                player.clear()
            } else {
                player.clear()
            }
        }
    }

    private func publishAccent(_ color: UIColor) {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        if color.getRed(&red, green: &green, blue: &blue, alpha: &alpha) {
            UserDefaults.standard.set([Double(red), Double(green), Double(blue)], forKey: "avatar.accent.\(scope)")
        }
        onAccent?(Color(uiColor: color))
    }

    private static let badges: [String: String] = [
        "typing": "laptopcomputer", "reading": "book", "drawing": "paintpalette",
        "writing": "pencil", "sleeping": "moon.zzz", "searching": "magnifyingglass"
    ]
}

@MainActor
private final class AvatarPlayer: ObservableObject {
    struct Frames: @unchecked Sendable {  // immutable images, handed over from the decoding task
        let images: [UIImage]
        let durations: [TimeInterval]
        let accent: UIColor?
    }
    @Published private(set) var frames: [UIImage] = []
    @Published private(set) var needsBadge = false
    private(set) var isSprite = false
    private(set) var accent: UIColor?
    private var durations: [TimeInterval] = []
    private var started = Date()
    private var identity = ""
    private static var cache: [String: Frames] = [:]
    /// Downloaded files (a clip is ~1.5 MB), so a mood seen before is never fetched again.
    private static var bytes: [String: Data] = [:]

    func clear() { frames = []; durations = []; needsBadge = false }

    func frame(at date: Date, reducedMotion: Bool) -> UIImage {
        guard !frames.isEmpty else { return UIImage() }
        guard !reducedMotion, frames.count > 1 else { return frames[0] }
        let total = durations.reduce(0, +)
        guard total > 0 else { return frames[0] }
        var time = max(0, date.timeIntervalSince(started)).truncatingRemainder(dividingBy: total)
        for index in frames.indices {
            let duration = index < durations.count ? durations[index] : 0.1
            if time < duration { return frames[index] }
            time -= duration
        }
        return frames[0]
    }

    func loadBuiltIn(animation: String) async {
        let base = Bundle.main.url(forResource: "orange-cat-spritesheet", withExtension: "webp")
        let actions = Bundle.main.url(forResource: "orange-cat-actions", withExtension: "webp")
        await loadSprite(base: base, actions: actions, animation: animation)
    }

    func load(source: JSONValue, animation: String, baseURL: URL?) async {
        if source["kind"].stringValue == "pet" {
            await loadSprite(base: Self.url(source["sheet_url"].string, base: baseURL),
                             actions: Self.url(source["actions_url"].string, base: baseURL), animation: animation)
            return
        }
        isSprite = false
        needsBadge = false
        let character = source["character"].boolValue
        let chosen = AvatarChoice.picture(source: source, animation: animation)
        let still = character ? chosen.still : source["image_url"].stringValue
        let clip = character ? chosen.clip : nil
        let currentIdentity = "\(still)|\(clip ?? "")"
        identity = currentIdentity
        // The app's colours come from the look's own picture, never from the clip playing: each mood's
        // clip has a slightly different colour, and a changing accent repainted every screen mid-reply.
        if let main = Self.url(source["image_url"].string, base: baseURL), let picture = await Self.fetch(main) {
            accent = picture.accent
        }
        // Display a still first; retain it until the new clip has downloaded and decoded.
        if let stillURL = Self.url(still, base: baseURL), let image = await Self.fetch(stillURL) {
            guard identity == currentIdentity, !Task.isCancelled else { return }
            install(image)
        }
        if let clipURL = Self.url(clip, base: baseURL), let motion = await Self.fetch(clipURL) {
            guard identity == currentIdentity, !Task.isCancelled else { return }
            install(motion)
            // Prefetch the other moods once a clip is playing.
            for path in source["motions"].object.values.compactMap(\.string) {
                guard !Task.isCancelled else { return }
                if let url = Self.url(path, base: baseURL) { _ = await Self.data(url) }  // the file only; decoded when shown
            }
        }
    }

    private func loadSprite(base: URL?, actions: URL?, animation: String) async {
        let currentIdentity = "\(base?.absoluteString ?? "")|\(animation)"
        identity = currentIdentity
        isSprite = true
        // A sprite's colour: its sheet's first (idle) frame, whichever row is playing.
        if let base, let idle = await Self.sprite(base, row: Self.rows["idle"]!, rows: 9) { accent = idle.accent }
        let row = Self.rows[animation] ?? Self.rows["idle"]!
        if row.action, let actions, let decoded = await Self.sprite(actions, row: row, rows: 6) {
            guard identity == currentIdentity, !Task.isCancelled else { return }
            needsBadge = false
            install(decoded)
            return
        }
        let fallback = row.action ? (Self.fallbacks[animation] ?? "idle") : animation
        let baseRow = Self.rows[fallback] ?? Self.rows["idle"]!
        if let base, let decoded = await Self.sprite(base, row: baseRow, rows: 9) {
            guard identity == currentIdentity, !Task.isCancelled else { return }
            needsBadge = row.action
            install(decoded)
        }
    }

    private func install(_ decoded: Frames) {
        durations = decoded.durations
        started = Date()
        frames = decoded.images
    }

    private static func url(_ path: String?, base: URL?) -> URL? {
        guard let path, !path.isEmpty else { return nil }
        return URL(string: path, relativeTo: base)?.absoluteURL
    }

    private static func data(_ url: URL) async -> Data? {
        if url.isFileURL { return try? Data(contentsOf: url) }
        let key = url.absoluteString
        if let kept = bytes[key] { return kept }
        // Clips and stills are kept on this iPhone too: the face shows at once on the next launch.
        guard let data = await LocalStore.download(url) else { return nil }
        if bytes.count >= 16 { bytes.removeAll(keepingCapacity: true) }
        bytes[key] = data
        return data
    }

    private static func fetch(_ url: URL) async -> Frames? {
        let key = url.absoluteString
        if let cached = cache[key] { return cached }
        guard let data = await data(url), !Task.isCancelled else { return nil }
        // Decoding a clip is real work: off the main thread, so the chat stays smooth.
        guard let decoded = await Task.detached(priority: .userInitiated, operation: { AvatarPlayer.decode(data) }).value,
              !Task.isCancelled else { return nil }
        store(decoded, key: key)
        return decoded
    }

    /// The clip's frames at most 256 px. A long clip keeps every other frame (each shown twice as
    /// long): it looks the same at this size and needs half the memory, so all moods stay cached.
    nonisolated private static func decode(_ data: Data) -> Frames? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let count = min(CGImageSourceGetCount(source), 180)
        let step = count > 48 ? 2 : 1
        var images: [UIImage] = []
        var delays: [TimeInterval] = []
        let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                        kCGImageSourceThumbnailMaxPixelSize: 256,
                                        kCGImageSourceCreateThumbnailWithTransform: true]
        for index in stride(from: 0, to: count, by: step) {
            guard let cg = CGImageSourceCreateThumbnailAtIndex(source, index, options as CFDictionary) else { continue }
            images.append(UIImage(cgImage: cg))
            var delay: TimeInterval = 0
            for skipped in index..<min(index + step, count) {
                let properties = CGImageSourceCopyPropertiesAtIndex(source, skipped, nil) as? [String: Any] ?? [:]
                let motion = (properties["{WebP}"] ?? properties["{GIF}"] ?? properties["{PNG}"]) as? [String: Any] ?? [:]
                delay += max(0.02, (motion["UnclampedDelayTime"] as? NSNumber)?.doubleValue
                             ?? (motion["DelayTime"] as? NSNumber)?.doubleValue ?? 0.1)
            }
            delays.append(delay)
        }
        guard let first = images.first else { return nil }
        return Frames(images: images, durations: delays, accent: dominantColor(first.cgImage))
    }

    private static func sprite(_ url: URL, row: SpriteRow, rows: Int) async -> Frames? {
        let key = "\(url.absoluteString)#row\(row.index)"
        if let cached = cache[key] { return cached }
        guard let data = await data(url), !Task.isCancelled,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              let sheet = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }
        let width = sheet.width / 8
        let height = sheet.height / rows
        guard width > 0, height > 0 else { return nil }
        let images: [UIImage] = row.durations.indices.compactMap { column in
            let rect = CGRect(x: CGFloat(column * width), y: CGFloat(row.index * height),
                              width: CGFloat(width), height: CGFloat(height))
            return sheet.cropping(to: rect).map { UIImage(cgImage: $0) }
        }
        guard !images.isEmpty else { return nil }
        let idle = sheet.cropping(to: CGRect(x: 0, y: 0, width: CGFloat(width), height: CGFloat(height)))
        let decoded = Frames(images: images, durations: row.durations, accent: dominantColor(idle))
        store(decoded, key: key)
        return decoded
    }

    private static func store(_ value: Frames, key: String) {
        let cost: (Frames) -> Int = { item in
            item.images.reduce(0) { $0 + ($1.cgImage?.bytesPerRow ?? 0) * ($1.cgImage?.height ?? 0) }
        }
        let total = cache.values.reduce(0) { $0 + cost($1) }
        // Room for the four moods of a drawn look (~12 MB each) plus the built-in sprite.
        if cache.count >= 24 || total + cost(value) > 72 * 1024 * 1024 {
            cache.removeAll(keepingCapacity: true)
        }
        cache[key] = value
    }

    private struct SpriteRow {
        let index: Int
        let durations: [TimeInterval]
        var action = false
    }
    private static func timing(_ count: Int, _ frameMS: Double, _ lastMS: Double) -> [TimeInterval] {
        Array(repeating: frameMS / 1000, count: count - 1) + [lastMS / 1000]
    }
    private static let rows: [String: SpriteRow] = [
        "idle": SpriteRow(index: 0, durations: [0.28, 0.11, 0.11, 0.14, 0.14, 0.32]),
        "running-right": SpriteRow(index: 1, durations: timing(8, 120, 220)),
        "running-left": SpriteRow(index: 2, durations: timing(8, 120, 220)),
        "waving": SpriteRow(index: 3, durations: timing(4, 140, 280)),
        "jumping": SpriteRow(index: 4, durations: timing(5, 140, 280)),
        "failed": SpriteRow(index: 5, durations: timing(8, 140, 240)),
        "waiting": SpriteRow(index: 6, durations: timing(6, 150, 260)),
        "running": SpriteRow(index: 7, durations: timing(6, 120, 220)),
        "review": SpriteRow(index: 8, durations: timing(6, 150, 280)),
        "typing": SpriteRow(index: 0, durations: timing(6, 120, 200), action: true),
        "reading": SpriteRow(index: 1, durations: timing(6, 180, 320), action: true),
        "drawing": SpriteRow(index: 2, durations: timing(6, 150, 260), action: true),
        "writing": SpriteRow(index: 3, durations: timing(6, 150, 260), action: true),
        "sleeping": SpriteRow(index: 4, durations: timing(6, 320, 480), action: true),
        "searching": SpriteRow(index: 5, durations: timing(6, 160, 300), action: true)
    ]
    private static let fallbacks: [String: String] = [
        "typing": "running", "reading": "review", "drawing": "running",
        "writing": "running", "sleeping": "idle", "searching": "review"
    ]

    /// Hue buckets weighted by saturation, ignoring transparent, nearly white and black pixels.
    nonisolated private static func dominantColor(_ image: CGImage?) -> UIColor? {
        guard let image else { return nil }
        let side = 48
        var pixels = [UInt8](repeating: 0, count: side * side * 4)
        let drew: Bool = pixels.withUnsafeMutableBytes { bytes in
            guard let context = CGContext(data: bytes.baseAddress, width: side, height: side,
                                          bitsPerComponent: 8, bytesPerRow: side * 4,
                                          space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: CGFloat(side), height: CGFloat(side)))
            return true
        }
        guard drew else { return nil }
        var buckets = Array(repeating: (weight: Double(0), r: Double(0), g: Double(0), b: Double(0)), count: 24)
        for index in stride(from: 0, to: pixels.count, by: 4) where pixels[index + 3] >= 200 {
            let r = Double(pixels[index]) / 255
            let g = Double(pixels[index + 1]) / 255
            let b = Double(pixels[index + 2]) / 255
            let high = max(r, max(g, b)), low = min(r, min(g, b)), delta = high - low
            guard delta >= 0.08, high > 0.25, low < 0.92 else { continue }
            var hue: Double
            if high == r { hue = ((g - b) / delta).truncatingRemainder(dividingBy: 6) }
            else if high == g { hue = (b - r) / delta + 2 }
            else { hue = (r - g) / delta + 4 }
            hue = (hue * 60 + 360).truncatingRemainder(dividingBy: 360)
            let bucket = Int((hue / 15).rounded()) % 24
            buckets[bucket].weight += delta
            buckets[bucket].r += r * delta
            buckets[bucket].g += g * delta
            buckets[bucket].b += b * delta
        }
        guard let best = buckets.max(by: { $0.weight < $1.weight }), best.weight > 0.5 else { return nil }
        return UIColor(red: CGFloat(best.r / best.weight), green: CGFloat(best.g / best.weight),
                       blue: CGFloat(best.b / best.weight), alpha: 1)
    }
}

private struct MochiFace: View {
    let variant: String
    static func palette(_ variant: String) -> (UIColor, UIColor) {
        let values: [String: (UInt32, UInt32)] = [
            "cream": (0xfff7ec, 0xe6cdb0), "peach": (0xffe6d8, 0xf2a184),
            "mint": (0xe4f8ee, 0x93d3b5), "sky": (0xe5f0ff, 0x93b8ef),
            "lilac": (0xf1e8ff, 0xb79de9), "cocoa": (0xf2e2d5, 0xb28465)
        ]
        let selected = values[variant] ?? values["cream"]!
        func color(_ hex: UInt32) -> UIColor {
            UIColor(red: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
                    blue: CGFloat(hex & 255) / 255, alpha: 1)
        }
        return (color(selected.0), color(selected.1))
    }
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let palette = Self.palette(variant)
            ZStack {
                Ellipse().fill(Color(uiColor: palette.1)).opacity(0.15)
                    .frame(width: width * 0.5, height: width * 0.08).offset(y: width * 0.41)
                Ellipse().fill(RadialGradient(colors: [Color(uiColor: palette.0), Color(uiColor: palette.1)],
                                             center: .topLeading, startRadius: 0, endRadius: width))
                    .frame(width: width * 0.72, height: width * 0.8)
                HStack(spacing: width * 0.13) {
                    Ellipse().frame(width: width * 0.075, height: width * 0.1)
                    Ellipse().frame(width: width * 0.075, height: width * 0.1)
                }.foregroundStyle(Color.brown.opacity(0.85)).offset(y: width * 0.02)
                Text("⌣").font(.system(size: width * 0.16)).foregroundStyle(.brown).offset(y: width * 0.12)
            }.frame(width: width, height: width)
        }
    }
}
