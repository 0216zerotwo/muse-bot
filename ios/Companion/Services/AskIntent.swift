import AppIntents
import Foundation

/// 问伙伴 from Siri, the Shortcuts app or the Action button: what to say is asked (Siri takes it by
/// voice), then the app opens on the chat and sends it — as ChatGPT's and Gemini's shortcuts do.
struct AskCompanionIntent: AppIntent {
    static let title: LocalizedStringResource = "问伙伴"
    static let openAppWhenRun = true

    @Parameter(title: "要说的话", requestValueDialog: "想跟伙伴说什么？")
    var text: String

    @MainActor
    func perform() async throws -> some IntentResult {
        IntentInbox.shared.pending = text
        return .result()
    }
}

struct CompanionShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskCompanionIntent(), phrases: ["用\(.applicationName)问一下", "问\(.applicationName)", "Ask \(.applicationName)"],
                    shortTitle: "问伙伴", systemImageName: "bubble.left.and.text.bubble.right")
    }
}

/// What a shortcut asked to send, until the chat is ready to send it.
@MainActor
final class IntentInbox: ObservableObject {
    static let shared = IntentInbox()
    @Published var pending: String?
}
