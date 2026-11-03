import XCTest
@testable import Companion

final class SettingsDecodingTests: XCTestCase {
    private func fixture(_ name: String) throws -> JSONValue {
        let bundle = Bundle(for: SettingsDecodingTests.self)
        let url = bundle.url(forResource: name, withExtension: "json")
            ?? bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
        return try JSONValue.parse(Data(contentsOf: XCTUnwrap(url, "Missing fixture \(name)")))
    }

    func testRealModelsResponseIncludesRoutingAndMedia() throws {
        let payload = try fixture("settings-models")
        let presets = SettingsDecoding.rows(payload, key: "presets", id: "name")
        XCTAssertFalse(presets.isEmpty)
        XCTAssertNotNil(presets.first?["max_tokens"].intValue)
        XCTAssertNotNil(presets.first?["temperature"].doubleValue)
        XCTAssertFalse(payload["settings"].object.isEmpty)
        XCTAssertFalse(payload["media"]["image"]["models"].array.isEmpty)
        XCTAssertNotNil(payload["media"]["video"]["active"].nonemptyString)
    }

    func testRealCatalogNullsAndSubscriptionFieldsDecode() throws {
        let payload = try fixture("settings-model-catalog")
        let vendors = SettingsDecoding.rows(payload, key: "vendors", id: "id")
        XCTAssertFalse(vendors.isEmpty)
        XCTAssertTrue(vendors[0]["kinds"].array.contains(.string("text")))
        XCTAssertNotNil(vendors[0]["login"].nonemptyString)
        XCTAssertFalse(payload["groups"].array.isEmpty)
    }

    func testRealUsageHasEmptyTodayAndPopulatedWeek() throws {
        let payload = try fixture("settings-usage")
        XCTAssertEqual(payload["today"]["cost"], .null)
        XCTAssertEqual(payload["today"]["by_model"].array.count, 0)
        XCTAssertNotNil(payload["week"]["cached_tokens"].doubleValue)
        XCTAssertFalse(payload["week"]["by_source"].object.isEmpty)
        XCTAssertFalse(payload["week"]["media"].array.isEmpty)
    }

    func testRealSkillsGroupsAreIndependent() throws {
        let payload = try fixture("settings-skills")
        XCTAssertFalse(SettingsDecoding.rows(payload, key: "learned", id: "name").isEmpty)
        XCTAssertFalse(SettingsDecoding.rows(payload, key: "bundled", id: "name").isEmpty)
        XCTAssertNotNil(payload["learned"].array.first?["updated_at"].doubleValue)
    }

    func testRealPermissionsOfferServerSpecifiedLevels() throws {
        let payload = try fixture("settings-permissions")
        let rows = SettingsDecoding.rows(payload, key: "kinds", id: "key")
        XCTAssertFalse(rows.isEmpty)
        for row in rows { XCTAssertTrue(row["levels"].array.contains(row["level"])) }
    }

    func testRealVaultEmptyListsAreValid() throws {
        let payload = try fixture("settings-vault")
        XCTAssertEqual(payload["logins"], .array([]))
        XCTAssertEqual(payload["keys"], .array([]))
        XCTAssertTrue(SettingsDecoding.rows(payload, key: "logins", id: "id").isEmpty)
    }

    func testRealDevicesUseMillisecondDatesAndNoCredentialFields() throws {
        let payload = try fixture("settings-devices")
        let rows = SettingsDecoding.rows(payload, key: "devices", id: "id")
        XCTAssertFalse(rows.isEmpty)
        for row in rows {
            XCTAssertNotNil(row["paired_at"].doubleValue)
            XCTAssertNotNil(row["last_seen"].doubleValue)
            XCTAssertEqual(row["token"], .null)
            XCTAssertFalse(SettingsDecoding.date(row["paired_at"]).isEmpty)
        }
    }

    func testRealServicesContainNullableRuntimeFields() throws {
        let payload = try fixture("settings-services")
        let rows = SettingsDecoding.rows(payload, key: "presets", id: "name")
        XCTAssertFalse(rows.isEmpty)
        XCTAssertNotNil(payload["installed_count"].intValue)
        XCTAssertEqual(rows[0]["auth"], .null)
        XCTAssertTrue(rows[0].object.keys.contains("required_fields"))
        XCTAssertTrue(rows[0].object.keys.contains("enabled_tools"))
    }

    func testMalformedSettingsRowsDoNotDiscardValidNeighbors() throws {
        let payload = try JSONValue.parse(Data(#"{"kinds":[null,42,{}, {"key":123,"label":null,"extra":true},{"key":"browse","level":"allow"}]}"#.utf8))
        let rows = SettingsDecoding.rows(payload, key: "kinds", id: "key")
        XCTAssertEqual(rows.count, 2)
        XCTAssertEqual(SettingsDecoding.title(rows[0], "key"), "123")
        XCTAssertEqual(SettingsDecoding.title(rows[0], "label", fallback: "权限"), "权限")
    }
}
