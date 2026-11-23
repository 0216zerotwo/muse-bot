import SwiftUI

@main
@MainActor
struct CompanionApp: App {
    @StateObject private var store = CompanionStore()
    @Environment(\.scenePhase) private var scenePhase
    @State private var wasBackgrounded = false

    init() {
        ReminderNotifications.registerBackgroundRefresh()
    }

    var body: some Scene {
        WindowGroup {
            CompanionRootView()
                .environmentObject(store)
                .task {
                    KeyboardDismisser.shared.installEverywhere()  // a tap outside a text field puts the keyboard away
                    Task.detached(priority: .background) {
                        LocalStore.trim()
                        DraftUpload.trimOrphanedBlobs()
                    }
                    await store.foreground()
                }
                .onOpenURL { url in
                    Task { await store.handleIncomingURL(url) }
                }
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .background:
                        wasBackgrounded = true
                        ReminderNotifications.scheduleBackgroundRefresh()
                    case .active:
                        KeyboardDismisser.shared.installEverywhere()
                        guard wasBackgrounded else { return }
                        wasBackgrounded = false
                        Task {
                            await store.foreground()
                        }
                    case .inactive:
                        break
                    @unknown default:
                        break
                    }
                }
        }
    }
}
