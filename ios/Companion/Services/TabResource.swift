import Foundation
import Combine

@MainActor
final class TabResource: ObservableObject {
    @Published var payload: JSONValue = .null
    @Published var error: String?
    @Published var busy = false
    var hasLoaded: Bool { payload != .null }

    /// Lists shown again at once on the next launch, before the server answers. Only these: triggers,
    /// models and settings can carry addresses or key hints, so they are always asked for afresh.
    static let kept: Set<String> = [
        "/api/webui/companion/feed", "/api/webui/companion/helpers", "/api/webui/companion/creations",
        "/api/webui/companion/ideas", "/api/webui/companion/goals", "/api/webui/companion/routines",
    ]

    func load(_ path: String, store: CompanionStore) async {
        guard !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        let file = Self.kept.contains(path) ? LocalStore.url("lists", server: store.serverAddress, name: path) : nil
        if payload == .null, file != nil,
           let value = await Task.detached(priority: .userInitiated, operation: { LocalStore.read(file).flatMap { try? JSONValue.parse($0) } }).value,
           payload == .null {
            payload = value
        }
        do {
            let value = try await store.getResource(path)
            if value != payload {  // the same data again redraws nothing
                payload = value
                if let file, let data = try? JSONEncoder().encode(value) {
                    Task.detached(priority: .utility) { LocalStore.write(data, to: file) }
                }
            }
        }
        catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }

    @discardableResult
    func change(_ action: String, payload: [String: JSONValue], store: CompanionStore) async -> Bool {
        guard !busy else { return false }
        busy = true; error = nil
        defer { busy = false }
        do { self.payload = try await store.changeResource(action, payload: payload); return true }
        catch { self.error = error.localizedDescription; return false }
    }
}
