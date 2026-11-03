import Foundation
import Security

/// Device credentials are scoped to one server and never copied into preferences or logs.
enum DeviceKeychain {
    private static let service = "com.personalbot.companion.device"

    static func read(server: String) throws -> String? {
        var query = identity(server: server)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else {
            throw ServiceError.message("无法读取钥匙串，请解锁 iPhone 后重试")
        }
        return value
    }

    static func save(_ value: String, server: String) throws {
        let query = identity(server: server)
        let attributes: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return }
        // Not there yet, or an old item that can't be updated in place: write it afresh.
        if status != errSecItemNotFound { SecItemDelete(query as CFDictionary) }
        var item = query
        attributes.forEach { item[$0.key] = $0.value }
        let added = SecItemAdd(item as CFDictionary, nil)
        guard added == errSecSuccess else {
            throw ServiceError.message("无法保存设备凭证（钥匙串错误 \(added)），请解锁 iPhone 后重试")
        }
    }

    static func remove(server: String) {
        SecItemDelete(identity(server: server) as CFDictionary)
    }

    private static func identity(server: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: server]
    }
}
