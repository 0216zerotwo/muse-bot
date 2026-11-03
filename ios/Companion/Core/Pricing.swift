import Foundation

/// A model's price in a line, whatever its style (see the server's model_check): per million
/// tokens, per second of video, per clip, per picture — always in 元. Ported from the web's
/// pricing.ts so both show the same words: `main` is the headline (the first, most common tier;
/// "起" when other specs cost less), `rows` the specs underneath when there is more than one.
enum Pricing {
    private static let each = ["second": "秒", "video": "条", "image": "张"]

    /// 2.1, 0.26, 0.025, 14
    static func yuan(_ value: Double) -> String {
        let rounded: Double
        if value >= 10 { rounded = (value * 10).rounded() / 10 }
        else if value >= 1 { rounded = (value * 100).rounded() / 100 }
        else {
            guard value != 0 else { return "0" }
            let digits = 1 - Int(floor(log10(abs(value))))  // two significant digits
            let scale = pow(10, Double(digits))
            rounded = (value * scale).rounded() / scale
        }
        var text = String(format: "%.4f", rounded)
        while text.hasSuffix("0") { text.removeLast() }
        if text.hasSuffix(".") { text.removeLast() }
        return text
    }

    static func summary(_ price: JSONValue) -> (main: String, rows: [String])? {
        let rows = price["rows"].array
        guard !rows.isEmpty else { return nil }
        if price["unit"].stringValue == "mtok" {
            let first = rows[0]
            let main = "¥\(yuan(first["input"].doubleValue ?? 0)) / ¥\(yuan(first["output"].doubleValue ?? 0))"
            let lines: [String]
            if rows.count > 1 {
                lines = rows.map { row in
                    let cached = row["cached"].doubleValue.map { "，缓存 ¥\(yuan($0))" } ?? ""
                    return "\(row["spec"].nonemptyString ?? "其他") ¥\(yuan(row["input"].doubleValue ?? 0)) / ¥\(yuan(row["output"].doubleValue ?? 0))\(cached)"
                }
            } else if let cached = first["cached"].doubleValue {
                lines = ["缓存命中 ¥\(yuan(cached))"]
            } else { lines = [] }
            return (main, lines)
        }
        let unit = each[price["unit"].stringValue] ?? "次"
        let prices = rows.map { $0["price"].doubleValue ?? 0 }
        let lowest = prices.min() ?? 0
        let main = rows.count > 1 && prices.contains(where: { $0 != lowest }) ? "¥\(yuan(lowest))/\(unit)起" : "¥\(yuan(prices[0]))/\(unit)"
        let lines = rows.count > 1 ? rows.map { "\($0["spec"].nonemptyString ?? "标准") ¥\(yuan($0["price"].doubleValue ?? 0))/\(unit)" } : []
        return (main, lines)
    }

    /// All of it in one line, for a model already added: each spec's price.
    static func line(_ price: JSONValue) -> String? {
        guard let summary = summary(price) else { return nil }
        if price["unit"].stringValue == "mtok" { return "\(summary.main) 每百万 token" }
        return summary.rows.isEmpty ? summary.main : summary.rows.joined(separator: " · ")
    }

    /// Under 选择模型: what the prices are per.
    static func unitHint(_ prices: [JSONValue]) -> String? {
        let units = Set(prices.filter { !$0["rows"].array.isEmpty }.map { $0["unit"].stringValue })
        if units.isEmpty { return nil }
        if units == ["mtok"] { return "输入 / 输出 每百万 token" }
        return "价格按官网，单位见每一行"
    }
}
