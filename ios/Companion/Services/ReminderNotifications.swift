import Foundation
import UserNotifications

/// The phone's own notifications for reminders coming up (see ``ReminderPlan``): all of them set
/// again each time, so a reminder paused, moved or deleted never goes off from an old plan.
@MainActor
enum ReminderNotifications {
    /// 设置 → 提醒到点时通知我.
    nonisolated static let enabledKey = "companion.reminder-notifications"
    private static let prefix = "companion-reminder-"

    static var enabled: Bool { UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true }

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
}

extension CompanionStore {
    /// Sets the phone's notifications for what is coming up: when the app opens, when a reply ends
    /// (it may have set a reminder) and when a scheduled task is paused, resumed or deleted here.
    func planReminders() async {
        guard connected else { return }
        let jobs: [JSONValue]
        do { jobs = try await getResource("/api/webui/automations")["jobs"].array } catch { return }
        await ReminderNotifications.plan(ReminderPlan.times(jobs, now: Date()), title: companionName)
    }
}
