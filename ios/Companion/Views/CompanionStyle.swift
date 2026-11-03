import SwiftUI
import UIKit

/// The same OKLCH lightness/chroma steps as companion/theme.ts.
/// The last sampled avatar accent also colors the very first frame on launch.
enum CompanionPalette {
    static var background: Color { token(light: (0.968, 0.06, 0.0075, 12), dark: (0.165, 0.08, 0.01, 0)) }
    static var surface: Color { token(light: (0.997, 0.03, 0.004, 0), dark: (0.215, 0.08, 0.011, 0)) }
    static var elevated: Color { token(light: (0.935, 0.12, 0.013, 12), dark: (0.275, 0.10, 0.014, 0)) }
    static var border: Color { Color.primary.opacity(0.10) }
    static var userBubble: Color { token(light: (0.285, 0.12, 0.02, 0), dark: (0.42, 0.55, 0.09, 0)) }
    static var primary: Color { token(light: (0.56, 1, 0.16, 0), dark: (0.72, 1, 0.15, 0)) }
    static var primaryText: Color {
        Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.10, alpha: 1) : .white })
    }

    private typealias Step = (Double, Double, Double, Double)
    // Every view reads these on every redraw: the colours are worked out once per accent and kept.
    private static var made: [String: Color] = [:]
    private static var madeFor: [Double] = []
    private static func token(light: Step, dark: Step) -> Color {
        let rgb = UserDefaults.standard.array(forKey: "companion.palette.accent") as? [Double] ?? [0.8784, 0.4784, 0.2078]
        if rgb != madeFor { made = [:]; madeFor = rgb }
        let key = "\(light)|\(dark)"
        if let kept = made[key] { return kept }
        let color = makeToken(rgb: rgb, light: light, dark: dark)
        made[key] = color
        return color
    }
    private static func makeToken(rgb: [Double], light: Step, dark: Step) -> Color {
        let accent = components(rgb)
        func make(_ step: Step) -> UIColor {
            mapped(lightness: step.0, chroma: min(accent.0 * step.1, step.2), hue: accent.1 + step.3)
        }
        let day = make(light), night = make(dark)
        return Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? night : day })
    }

    private static func components(_ rgb: [Double]) -> (Double, Double) {
        let safe = rgb.count == 3 ? rgb : [0.8784, 0.4784, 0.2078]
        let v = safe.map { $0 <= 0.04045 ? $0 / 12.92 : pow(($0 + 0.055) / 1.055, 2.4) }
        let l = cbrt(0.4122214708 * v[0] + 0.5363325363 * v[1] + 0.0514459929 * v[2])
        let m = cbrt(0.2119034982 * v[0] + 0.6806995451 * v[1] + 0.1073969566 * v[2])
        let s = cbrt(0.0883024619 * v[0] + 0.2817188376 * v[1] + 0.6299787005 * v[2])
        let a = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s
        let b = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
        return (hypot(a, b), atan2(b, a) * 180 / .pi)
    }

    private static func mapped(lightness: Double, chroma: Double, hue: Double) -> UIColor {
        var c = chroma
        for _ in 0..<24 {
            let a = c * cos(hue * .pi / 180), b = c * sin(hue * .pi / 180)
            let l = pow(lightness + 0.3963377774 * a + 0.2158037573 * b, 3)
            let m = pow(lightness - 0.1055613458 * a - 0.0638541728 * b, 3)
            let s = pow(lightness - 0.0894841775 * a - 1.291485548 * b, 3)
            let linear = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                          -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                          -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s]
            let rgb = linear.map { $0 <= 0.0031308 ? 12.92 * $0 : 1.055 * pow($0, 1 / 2.4) - 0.055 }
            if rgb.allSatisfy({ $0 >= -0.001 && $0 <= 1.001 }) {
                return UIColor(red: CGFloat(min(1, max(0, rgb[0]))), green: CGFloat(min(1, max(0, rgb[1]))),
                               blue: CGFloat(min(1, max(0, rgb[2]))), alpha: 1)
            }
            c *= 0.85
        }
        return UIColor(white: CGFloat(lightness), alpha: 1)
    }
}

struct FloatingPanel: ViewModifier {
    var radius: CGFloat = 24
    func body(content: Content) -> some View {
        content
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: radius))
            .overlay(RoundedRectangle(cornerRadius: radius).fill(
                LinearGradient(colors: [.white.opacity(0.09), .clear], startPoint: .top, endPoint: .bottom)).allowsHitTesting(false))
            .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(CompanionPalette.border).allowsHitTesting(false))
    }
}

/// Liquid Glass on iOS 26, the frosted panel before it.
struct GlassCard: ViewModifier {
    var radius: CGFloat = 24
    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.glassEffect(.regular, in: RoundedRectangle(cornerRadius: radius))
        } else {
            content.modifier(FloatingPanel(radius: radius))
        }
    }
}

/// A chat bubble's outline, as the web's: round (20) with one tight corner (6) for its tail —
/// bottom-left on a reply (top-left too where it joins the bubble above), bottom-right on the user's;
/// chat-app style the tail is at the top, by the face.
enum Bubble {
    static func reply(messenger: Bool, joined: Bool) -> UnevenRoundedRectangle {
        if messenger { return UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 20, bottomTrailingRadius: 20, topTrailingRadius: 20) }
        return UnevenRoundedRectangle(topLeadingRadius: joined ? 6 : 20, bottomLeadingRadius: 6, bottomTrailingRadius: 20, topTrailingRadius: 20)
    }
    static func user(messenger: Bool) -> UnevenRoundedRectangle {
        if messenger { return UnevenRoundedRectangle(topLeadingRadius: 20, bottomLeadingRadius: 20, bottomTrailingRadius: 20, topTrailingRadius: 6) }
        return UnevenRoundedRectangle(topLeadingRadius: 20, bottomLeadingRadius: 20, bottomTrailingRadius: 6, topTrailingRadius: 20)
    }
}

/// A reply bubble's look: the floating panel's colours without a live blur. The chat's background
/// is a plain colour, so a blur shows nothing more — and a screen of blurred bubbles was slow to scroll.
struct BubbleFill<S: Shape>: ViewModifier {
    var shape: S
    func body(content: Content) -> some View {
        content
            .background(CompanionPalette.surface.opacity(0.92), in: shape)
            .overlay(shape.fill(LinearGradient(colors: [.white.opacity(0.06), .clear], startPoint: .top, endPoint: .bottom)).allowsHitTesting(false))
            .overlay(shape.stroke(CompanionPalette.border, lineWidth: 1).allowsHitTesting(false))
    }
}

struct ScreenHeading: View {
    var title: String
    var action: (() -> Void)? = nil
    var icon: String = "plus"
    var body: some View {
        HStack {
            Text(title).font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            Spacer()
            if let action {
                Button(action: action) { Image(systemName: icon).font(.title2).frame(width: 44, height: 44).background(CompanionPalette.elevated, in: Circle()) }
                    .accessibilityLabel(icon == "arrow.clockwise" ? "刷新\(title)" : "添加\(title)")
            }
        }.padding(.horizontal, 20).padding(.vertical, 12)
    }
}

struct ResourceErrorView: View {
    let message: String
    let retry: () -> Void
    var body: some View {
        VStack(spacing: 12) {
            Label(message, systemImage: "exclamationmark.circle").font(.subheadline).multilineTextAlignment(.center)
            Button("重试", action: retry).buttonStyle(.bordered).frame(minHeight: 44)
        }.padding(24).frame(maxWidth: .infinity).accessibilityElement(children: .contain)
    }
}
