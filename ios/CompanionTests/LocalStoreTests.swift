import XCTest
@testable import Companion

/// What this iPhone keeps of a server: one folder per server, hashed names, cleared on demand.
final class LocalStoreTests: XCTestCase {
    private let server = "http://test-localstore.invalid:8766"

    override func tearDown() {
        LocalStore.removeServer(server)
        super.tearDown()
    }

    func testAServerAndItsLinksShareOneFolder() {
        let chat = LocalStore.url("chats", server: server, name: "main")
        let picture = LocalStore.url("media", server: "HTTP://Test-LocalStore.invalid:8766/api/media/abc/def?x=1", name: "p")
        XCTAssertEqual(chat?.deletingLastPathComponent().deletingLastPathComponent(), picture?.deletingLastPathComponent().deletingLastPathComponent())
        XCTAssertNil(LocalStore.url("chats", server: "", name: "main"))
    }

    func testFileNamesGiveAwayNoAddressOrTitle() throws {
        let path = try XCTUnwrap(LocalStore.url("chats", server: server, name: "我的日记")).path
        XCTAssertFalse(path.contains("test-localstore"))
        XCTAssertFalse(path.contains("我的日记"))
    }

    func testWriteReadAndForgetAServer() throws {
        let file = LocalStore.url("chats", server: server, name: "main")
        LocalStore.write(Data("[1,2]".utf8), to: file)
        XCTAssertEqual(LocalStore.read(file), Data("[1,2]".utf8))
        XCTAssertGreaterThan(LocalStore.size(), 0)
        LocalStore.removeServer(server)
        XCTAssertNil(LocalStore.read(file))
    }

    func testTrimDropsOldPicturesButKeepsChats() throws {
        let chat = LocalStore.url("chats", server: server, name: "main")
        let picture = LocalStore.url("media", server: server, name: "big")
        LocalStore.write(Data(repeating: 1, count: 10), to: chat)
        LocalStore.write(Data(repeating: 2, count: 4_096), to: picture)
        LocalStore.trim(limit: 0)
        XCTAssertNotNil(LocalStore.read(chat))
        XCTAssertNil(LocalStore.read(picture))
    }
}
