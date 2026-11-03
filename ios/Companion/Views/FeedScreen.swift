import SwiftUI
import MarkdownUI

@MainActor
struct FeedScreen: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = TabResource()
    @State private var settings = false
    @State private var info: TabRecord?
    @State private var instructions = ""
    @State private var daily = true
    @State private var time = "08:00"
    @State private var writing = false
    @State private var beforeWrite: Double = 0
    var onChat: () -> Void = {}
    private var posts: [TabRecord] { TabRecords.items(resource.payload, key: "posts") }
    private var liked: Set<String> { Set(resource.payload["liked"].array.compactMap(\.nonemptyString)) }

    var body: some View {
        ScrollView {
            let list = posts
            let likes = liked
            LazyVStack(alignment: .leading, spacing: 0) {  // posts are laid out as they scroll in
                ScreenHeading(title: "动态", action: edit, icon: "slider.horizontal.3")
                Button(action: edit) {
                    Text(resource.payload["instructions"].nonemptyString ?? "设置动态版块说明")
                        .lineLimit(1).font(.system(size: 16)).foregroundStyle(.secondary)
                        .padding(.horizontal, 20).padding(.vertical, 13).frame(maxWidth: .infinity, alignment: .leading)
                        .overlay(Capsule().stroke(CompanionPalette.border, lineWidth: 1))
                }.buttonStyle(.plain).padding(.horizontal, 20).padding(.bottom, 12)
                if let error = resource.error { ResourceErrorView(message: error, retry: { Task { await load() } }) }
                // Only the first load shows it: a refresh must not shift the page.
                if writing || (resource.busy && !resource.hasLoaded) { ProgressView(writing ? "正在写…" : "正在读取…").padding(20).frame(maxWidth: .infinity) }
                if resource.hasLoaded && list.isEmpty {
                    VStack(spacing: 12) {
                        EmojiIcon(emoji: "🗞️", size: 72)
                        Text("还没有动态").font(.headline)
                        Text("每天 \(resource.payload["time"].nonemptyString ?? "08:00") 更新").foregroundStyle(.secondary)
                        Button("现在就写一期") { Task { await writeNow() } }
                            .font(.system(size: 15, weight: .semibold)).padding(.horizontal, 24).frame(minHeight: 44)
                            .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText).buttonStyle(.plain)
                    }.frame(maxWidth: .infinity).padding(30)
                }
                ForEach(list) { post in
                    HStack(alignment: .top, spacing: 13) {
                        EmojiIcon(emoji: Self.areaEmoji[post["area"].stringValue] ?? "📝", size: 40).padding(.top, 2)
                        VStack(alignment: .leading, spacing: 8) {
                            Text(post.title).font(.system(size: 18, weight: .bold))
                            Markdown(post["body"].stringValue).markdownTextStyle { FontSize(16) }
                            if let image = post["image"].nonemptyString {
                                RemotePicture(path: image, baseURL: store.baseURL, maxPixel: 1_100).frame(maxHeight: 220).clipShape(RoundedRectangle(cornerRadius: 16))
                            }
                            HStack(spacing: 4) {
                                Button { Task { await react(post, action: "like", on: !likes.contains(post.id)) } } label: {
                                    Image(systemName: likes.contains(post.id) ? "heart.fill" : "heart").font(.system(size: 22)).frame(width: 44, height: 44)
                                        .foregroundStyle(likes.contains(post.id) ? CompanionPalette.primary : Color.primary)
                                }.accessibilityLabel(likes.contains(post.id) ? "取消喜欢" : "喜欢")
                                Button { discuss(post) } label: { Label("讨论", systemImage: "bubble.left").font(.system(size: 15)).frame(minHeight: 44) }
                                Spacer(minLength: 0)
                                Text(TabRecords.ago(post["ts"])).font(.system(size: 13)).foregroundStyle(.secondary)
                                Button { info = post } label: { Image(systemName: "info.circle").font(.system(size: 20)).frame(width: 44, height: 44) }.accessibilityLabel("关于这条动态")
                            }.buttonStyle(.plain)
                        }
                    }.padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 7)
                    Divider().overlay(CompanionPalette.border)
                }
            }.padding(.top, 174).padding(.bottom, 24)
        }
        .refreshable { await load() }
        // The server says when a post is written; the minute's look is for when that word was missed.
        .onChange(of: store.feedRevision) { _, _ in Task { await load() } }
        .task {
            await load()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 60_000_000_000) } catch { return }
                await load()
            }
        }
        .sheet(isPresented: $settings) { settingsSheet }
        .sheet(item: $info) { post in
            NavigationStack {
                Form {
                    Section {
                        Text(post.title).font(.headline)
                        LabeledContent("话题") {
                            HStack(spacing: 6) {
                                EmojiIcon(emoji: Self.areaEmoji[post["area"].stringValue] ?? "📝", size: 22)
                                Text(Self.areaLabel[post["area"].stringValue] ?? "其他")
                            }
                        }
                        LabeledContent("写于", value: post["ts"].doubleValue.map { Date(timeIntervalSince1970: $0 / 1000).formatted(date: .abbreviated, time: .shortened) } ?? "")
                        Text("按你们聊过的事和你的目标挑话题。想换方向，改顶部那句说明。").foregroundStyle(.secondary)
                    }
                    Button("不想看这条，删掉", role: .destructive) { Task { await react(post, action: "delete"); info = nil } }
                }.navigationTitle("关于这条动态").navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { info = nil } } }
            }.presentationDetents([.medium, .large])
        }
    }

    private var settingsSheet: some View {
        NavigationStack {
            Form {
                Section("动态版块说明") {
                    Text("你的编辑将应用于今后的动态帖子。").foregroundStyle(.secondary)
                    TextEditor(text: $instructions).frame(minHeight: 130).accessibilityLabel("动态版块说明")
                }
                Section {
                    Toggle("每天自动更新", isOn: $daily)
                    TextField("更新时间（HH:MM）", text: $time).keyboardType(.numbersAndPunctuation).disabled(!daily)
                    Button("现在就写一期") { Task { await writeNow(); settings = false } }.disabled(resource.busy)
                }
                if let error = resource.error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("动态版块说明").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("取消") { settings = false } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("保存") { Task {
                            if await resource.change("companion.feed.change", payload: ["action": .string("update"), "changes": .object(["instructions": .string(String(instructions.prefix(500))), "daily": .bool(daily), "time": .string(time)])], store: store) { settings = false }
                        } }.disabled(instructions.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || resource.busy)
                    }
                }
        }.presentationDetents([.medium, .large])
    }

    private func load() async {
        await resource.load("/api/webui/companion/feed", store: store)
        if writing && (resource.payload["generated_at"].doubleValue ?? 0) > beforeWrite { writing = false }
        let newest = posts.compactMap { $0["ts"].doubleValue }.max() ?? 0
        if newest > (store.companionState["feed_seen_at"].doubleValue ?? 0) {
            do { _ = try await store.changeResource("companion.update", payload: ["changes": .object(["feed_seen_at": .number(newest)])]) }
            catch { resource.error = error.localizedDescription }
        }
    }
    private func edit() {
        instructions = resource.payload["instructions"].stringValue
        daily = resource.payload["daily"].boolValue
        time = resource.payload["time"].nonemptyString ?? "08:00"
        settings = true
    }
    private func writeNow() async {
        beforeWrite = resource.payload["generated_at"].doubleValue ?? 0
        writing = await resource.change("companion.feed.change", payload: ["action": .string("write_now")], store: store)
    }
    private func react(_ post: TabRecord, action: String, on: Bool = true) async {
        await resource.change("companion.feed.change", payload: ["action": .string(action), "id": .string(post.id), "on": .bool(on)], store: store)
    }
    private func discuss(_ post: TabRecord) {
        Task {
            await store.changeChat(store.mainChatID)
            store.composeDraft(post["prompt"].nonemptyString ?? "关于「\(post.title)」，")
            onChat()
            await react(post, action: "discussed")
        }
    }
    // Each post's topic, drawn as the web draws it.
    static let areaEmoji = ["tech": "💻", "ai": "🤖", "finance": "📈", "news": "📰", "health": "🌿", "food": "🍳", "travel": "✈️", "learning": "📚",
                            "entertainment": "🎬", "sports": "⚽", "home": "🏠", "people": "👥", "goals": "🎯", "other": "📝"]
    static let areaLabel = ["tech": "科技", "ai": "AI", "finance": "财经", "news": "新闻", "health": "健康", "food": "美食", "travel": "出行", "learning": "学习",
                            "entertainment": "娱乐", "sports": "体育", "home": "家居", "people": "人物", "goals": "目标", "other": "其他"]
}
