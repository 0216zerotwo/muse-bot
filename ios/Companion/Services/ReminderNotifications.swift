import BackgroundTasks
import Foundation
import UserNotifications

private final class BackgroundRefreshBox: @unchecked Sendable {
    private let task: BGTask
    private let lock = NSLock()
    private var completed = false

    init(_ task: BGTask) {
        self.task = task
    }

    func complete(success: Bool) {
        lock.lock()
        let shouldComplete = !completed
        completed = true
        lock.unlock()
        if shouldComplete {
            task.setTaskCompleted(success: success)
        }
    }
}

private final class RegistrationGate: @unchecked Sendable {
    private let lock = NSLock()
    private var registered = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if registered { return false }
        registered = true
        return true
    }
}

/// The phone's own notifications for reminders coming up (see ``ReminderPlan``): all of them set
/// again each time, so a reminder paused, moved or deleted never goes off from an old plan.
@MainActor
enum ReminderNotifications {
    /// 设置 → 提醒到点时通知我.
    nonisolated static let enabledKey = "companion.reminder-notifications"
    nonisolated static let backgroundTaskIdentifier = "com.personalbot.companion.refresh"
    private static let prefix = "companion-reminder-"
    nonisolated private static let registrationGate = RegistrationGate()

    static var enabled: Bool { UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true }

    static func clearAll() async {
        let center = UNUserNotificationCenter.current()
        let pending = await center.pendingNotificationRequests()
        let ids = pending.map(\.identifier).filter { $0.hasPrefix(prefix) }
        if !ids.isEmpty {
            center.removePendingNotificationRequests(withIdentifiers: ids)
        }
    }

    static func plan(_ times: [ReminderTime], title: String) async {
        let center = UNUserNotificationCenter.current()
        let pending = await center.pendingNotificationRequests()
        center.removePendingNotificationRequests(withIdentifiers: pending.map(\.identifier).filter { $0.hasPrefix(prefix) })
        guard enabled, !times.isEmpty else { return }
        // Asked once, the first time there is a reminder to set (not at launch).
        let asked = await center.notificationSettings()
        if asked.authorizationStatus == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .sound])
        }
        let status = await center.notificationSettings().authorizationStatus
        guard status == .authorized || status == .provisional || status == .ephemeral else { return }
        for time in times {
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = time.body
            content.sound = .default
            content.threadIdentifier = "companion-reminders"
            let when = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: time.at)
            let request = UNNotificationRequest(identifier: prefix + time.id, content: content,
                                                trigger: UNCalendarNotificationTrigger(dateMatching: when, repeats: false))
            try? await center.add(request)
        }
    }

    nonisolated static func registerBackgroundRefresh() {
        guard registrationGate.claim() else { return }
        _ = BGTaskScheduler.shared.register(forTaskWithIdentifier: backgroundTaskIdentifier, using: DispatchQueue.main) { task in
            let box = BackgroundRefreshBox(task)
            let work = Task { @MainActor in
                ReminderNotifications.scheduleBackgroundRefresh()
                let ok = await ReminderNotifications.performBackgroundRefresh()
                box.complete(success: ok)
            }
            task.expirationHandler = {
                work.cancel()
                box.complete(success: false)
            }
        }
    }

    static func scheduleBackgroundRefresh() {
        guard enabled, !(UserDefaults.standard.string(forKey: "companion.server") ?? "").isEmpty else { return }
        let request = BGAppRefreshTaskRequest(identifier: backgroundTaskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private static func performBackgroundRefresh() async -> Bool {
        guard enabled else { return true }
        let serverAddress = UserDefaults.standard.string(forKey: "companion.server") ?? ""
        guard !serverAddress.isEmpty, let base = try? APIClient.normalizedServer(serverAddress) else { return false }
        let client = APIClient(baseURL: base)
        defer { client.invalidate() }
        do {
            let credential = try DeviceKeychain.read(server: serverAddress)
            let boot = try await client.bootstrap(deviceToken: credential)
            client.apiToken = boot["api_token"].stringValue
            guard !client.apiToken.isEmpty else { return false }
            let jobs = try await client.get("/api/webui/automations")["jobs"].array
            var title = "伙伴"
            if let cached = UserDefaults.standard.data(forKey: "companion.state." + serverAddress),
               let state = try? JSONValue.parse(cached),
               let name = state["name"].nonemptyString {
                title = name
            }
            await plan(ReminderPlan.times(jobs, now: Date()), title: title)
            return true
        } catch {
            return false
        }
    }
}

extension CompanionStore {
    /// Sets the phone's notifications for what is coming up: when the session has loaded `companionName`,
    /// when a reply ends (it may have set a reminder) and when a scheduled task is changed here.
    func planReminders() async {
        guard connected else { return }
        let jobs: [JSONValue]
        do { jobs = try await getResource("/api/webui/automations")["jobs"].array } catch { return }
        await ReminderNotifications.plan(ReminderPlan.times(jobs, now: Date()), title: companionName)
    }
}
