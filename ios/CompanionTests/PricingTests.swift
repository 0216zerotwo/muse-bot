import XCTest
@testable import Companion

/// The same words as the web's pricing.ts for each style of price.
final class PricingTests: XCTestCase {
    private func price(_ json: String) throws -> JSONValue { try JSONValue.parse(Data(json.utf8)) }

    func testYuanKeepsTwoSignificantDigitsUnderOne() {
        XCTAssertEqual(Pricing.yuan(14), "14")
        XCTAssertEqual(Pricing.yuan(2.1), "2.1")
        XCTAssertEqual(Pricing.yuan(0.26), "0.26")
        XCTAssertEqual(Pricing.yuan(0.025), "0.025")
        XCTAssertEqual(Pricing.yuan(0), "0")
    }

    func testTokensShowInputOutputAndTiers() throws {
        let one = try price(#"{"unit":"mtok","rows":[{"spec":"","input":2,"output":8,"cached":0.2}]}"#)
        XCTAssertEqual(Pricing.summary(one)?.main, "¥2 / ¥8")
        XCTAssertEqual(Pricing.summary(one)?.rows, ["缓存命中 ¥0.2"])
        XCTAssertEqual(Pricing.line(one), "¥2 / ¥8 每百万 token")
        let tiers = try price(#"{"unit":"mtok","rows":[{"spec":"≤32K","input":0.8,"output":2},{"spec":"","input":1.2,"output":4.8}]}"#)
        XCTAssertEqual(Pricing.summary(tiers)?.rows, ["≤32K ¥0.8 / ¥2", "其他 ¥1.2 / ¥4.8"])
    }

    func testPerSecondVideoNamesTheCheapestWithQi() throws {
        let video = try price(#"{"unit":"second","rows":[{"spec":"768P","price":0.26},{"spec":"480P","price":0.16}]}"#)
        XCTAssertEqual(Pricing.summary(video)?.main, "¥0.16/秒起")
        XCTAssertEqual(Pricing.line(video), "768P ¥0.26/秒 · 480P ¥0.16/秒")
        let image = try price(#"{"unit":"image","rows":[{"spec":"","price":0.12}]}"#)
        XCTAssertEqual(Pricing.line(image), "¥0.12/张")
        XCTAssertNil(Pricing.summary(.null))
    }

    func testUnitHint() throws {
        let tokens = try price(#"{"unit":"mtok","rows":[{"input":1,"output":2}]}"#)
        let picture = try price(#"{"unit":"image","rows":[{"price":0.1}]}"#)
        XCTAssertEqual(Pricing.unitHint([tokens, .null]), "输入 / 输出 每百万 token")
        XCTAssertEqual(Pricing.unitHint([tokens, picture]), "价格按官网，单位见每一行")
        XCTAssertNil(Pricing.unitHint([.null]))
    }
}
