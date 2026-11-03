import SwiftUI

@main
@MainActor
struct CompanionApp: App {
    @StateObject private var store = CompanionStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            CompanionRootView()
                .environmentObject(store)
                .task {
                    KeyboardDismisser.shared.installEverywhere()  // a tap outside a text field puts the keyboard away
                    Task.detached(priority: .background) { LocalStore.trim() }
                    await store.foreground()
                    await store.planReminders()
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        KeyboardDismisser.shared.installEverywhere()
                        Task {
                            await store.foreground()
                            await store.planReminders()  // the reminders coming up go off on the phone itself
                        }
                    }
                }
        }
    }
}
