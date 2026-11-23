import SwiftUI

@main
@MainActor
struct CompanionApp: App {
    @StateObject private var store = CompanionStore()
    @Environment(\.scenePhase) private var scenePhase
    @State private var wasBackgrounded = false

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
                    await store.planReminders()
                }
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .background:
                        wasBackgrounded = true
                    case .active:
                        KeyboardDismisser.shared.installEverywhere()
                        guard wasBackgrounded else { return }
                        wasBackgrounded = false
                        Task {
                            await store.foreground()
                            await store.planReminders()  // the reminders coming up go off on the phone itself
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
