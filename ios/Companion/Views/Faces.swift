import SwiftUI
import UIKit

/// The app's own drawings for the emoji it shows (helper faces, feed topics), as the web's icons.ts:
/// bundled from engine/webui/public/icons. An emoji stays the stored value; where there is a drawing
/// for it, the drawing is shown.
enum DrawnIcons {
    private static let names: [String: String] = [
        "🤖": "robot", "🔎": "search", "🔍": "search", "💰": "money", "✍️": "write", "✍": "write", "🧭": "compass",
        "🛒": "cart", "🌐": "globe", "📚": "books", "🗂️": "folders", "🗂": "folders", "📁": "folders", "📈": "chart",
        "🍳": "cooking", "🏃": "run", "🎨": "palette", "🧑‍💻": "coder", "📮": "mailbox", "🗓️": "calendar", "🗓": "calendar",
        "📅": "calendar", "🙂": "smile", "😊": "smile", "💻": "laptop", "📰": "news", "🗞️": "news", "🗞": "news",
        "🌿": "leaf", "✈️": "plane", "✈": "plane", "🎬": "clapper", "⚽": "ball", "🏠": "home", "👥": "people",
        "🎯": "target", "📝": "memo",
    ]
    /// What the helper face picker offers, in order.
    static let faceChoices = ["🤖", "🔎", "💰", "✍️", "🧭", "🛒", "🌐", "📚", "🗂️", "📈", "🍳", "🏃", "🎨", "🧑‍💻", "📮", "🗓️"]

    private static let images = NSCache<NSString, UIImage>()

    static func image(for emoji: String) -> UIImage? {
        guard let name = names[emoji.trimmingCharacters(in: .whitespaces)] else { return nil }
        if let kept = images.object(forKey: name as NSString) { return kept }
        guard let path = Bundle.main.path(forResource: name, ofType: "webp"), let image = UIImage(contentsOfFile: path) else { return nil }
        images.setObject(image, forKey: name as NSString)
        return image
    }
}

/// An emoji as the app draws it: its drawing when there is one, else the emoji.
struct EmojiIcon: View {
    let emoji: String
    let size: CGFloat
    var body: some View {
        if let image = DrawnIcons.image(for: emoji) {
            Image(uiImage: image).resizable().scaledToFit().frame(width: size, height: size).accessibilityHidden(true)
        } else {
            Text(emoji).font(.system(size: size * 0.9)).frame(width: size, height: size).accessibilityHidden(true)
        }
    }
}

/// A helper's face: its photo, or its emoji drawn, on a round fill.
struct HelperAvatar: View {
    let emoji: String
    var avatar: String = ""
    var size: CGFloat = 44
    let baseURL: URL?
    var body: some View {
        ZStack {
            CompanionPalette.elevated
            // The drawn emoji stays underneath: a photo that can't load (or is still loading) leaves it showing.
            EmojiIcon(emoji: emoji.isEmpty ? "🤖" : emoji, size: (size * 0.72).rounded())
            if !avatar.isEmpty {
                RemotePicture(path: avatar, baseURL: baseURL, maxPixel: size * 3, quiet: true).scaledToFill()
            }
        }
        .frame(width: size, height: size).clipShape(Circle()).accessibilityHidden(true)
    }
}

/// A group's face, as in a chat app: its members' faces in a small grid (up to four).
struct GroupFace: View {
    let members: [JSONValue]
    var size: CGFloat = 48
    let baseURL: URL?
    var body: some View {
        let shown = Array(members.prefix(4))
        let cell = shown.count <= 1 ? size : (size - 6) / 2
        ZStack {
            CompanionPalette.surface
            if shown.count <= 1 {
                HelperAvatar(emoji: shown.first?["emoji"].stringValue ?? "🤖", avatar: shown.first?["avatar"].stringValue ?? "", size: cell, baseURL: baseURL)
            } else {
                LazyVGrid(columns: [GridItem(.fixed(cell), spacing: 2), GridItem(.fixed(cell), spacing: 2)], spacing: 2) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { _, member in
                        HelperAvatar(emoji: member["emoji"].stringValue, avatar: member["avatar"].stringValue, size: cell, baseURL: baseURL)
                    }
                }
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.3))
        .overlay(RoundedRectangle(cornerRadius: size * 0.3).strokeBorder(CompanionPalette.border, lineWidth: 0.5))
        .accessibilityHidden(true)
    }
}

/// The user's face in a conversation: "我" on the theme colour.
struct MeFace: View {
    var size: CGFloat = 36
    var body: some View {
        Text("我").font(.system(size: size * 0.4, weight: .semibold)).foregroundStyle(CompanionPalette.primaryText)
            .frame(width: size, height: size).background(CompanionPalette.primary, in: Circle()).accessibilityHidden(true)
    }
}

/// Who said a reply in a chat-app style conversation: a helper (its face) or the companion hosting a
/// group, and the name shown over the message in a group.
struct Speaker: Equatable {
    var emoji = "🤖"
    var avatar = ""
    /// The companion's still picture (a group's host); nil for a helper.
    var companionPicture: String?
    var companion = false
    var name: String?
}

struct SpeakerFace: View {
    let speaker: Speaker
    var size: CGFloat = 36
    let baseURL: URL?
    var body: some View {
        if speaker.companion {
            ZStack {
                CompanionPalette.elevated
                if let picture = speaker.companionPicture, !picture.isEmpty {
                    RemotePicture(path: picture, baseURL: baseURL, maxPixel: size * 3).scaledToFit()
                } else {
                    Image("BrandMark").resizable().scaledToFit()
                }
            }.frame(width: size, height: size).clipShape(Circle()).accessibilityHidden(true)
        } else {
            HelperAvatar(emoji: speaker.emoji, avatar: speaker.avatar, size: size, baseURL: baseURL)
        }
    }
}

/// A chat list's time, as the web's chatTime: 21:09 today, 昨天, 周三 this week, else 10/23.
func chatTime(_ milliseconds: Double, now: Date = Date()) -> String {
    let date = Date(timeIntervalSince1970: milliseconds / 1000)
    let calendar = Calendar.current
    let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 0
    if days <= 0 {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }
    if days == 1 { return "昨天" }
    if days < 7 { return "周" + ["日", "一", "二", "三", "四", "五", "六"][(calendar.component(.weekday, from: date) - 1) % 7] }
    let parts = calendar.dateComponents([.month, .day], from: date)
    return "\(parts.month ?? 0)/\(parts.day ?? 0)"
}

/// 「今天 14:05」「昨天 09:12」「明天 08:00」「10月3日 08:00」, as the web's dayLabel.
func dayLabel(_ milliseconds: Double) -> String {
    let date = Date(timeIntervalSince1970: milliseconds / 1000)
    let calendar = Calendar.current
    let parts = calendar.dateComponents([.month, .day, .hour, .minute], from: date)
    let time = String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    if calendar.isDateInToday(date) { return "今天 \(time)" }
    if calendar.isDateInYesterday(date) { return "昨天 \(time)" }
    if calendar.isDateInTomorrow(date) { return "明天 \(time)" }
    return "\(parts.month ?? 0)月\(parts.day ?? 0)日 \(time)"
}
