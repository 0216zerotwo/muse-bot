import AVFoundation
import Foundation

/// 朗读: prefers server-side Neural TTS (`POST /api/webui/companion/tts`, cached MP3 played via
/// `AVAudioPlayer`) when `capabilities.tts == true`, seamlessly falling back to on-device
/// `AVSpeechSynthesizer` when offline or unavailable.
@MainActor
final class ReadAloud: NSObject, ObservableObject, AVSpeechSynthesizerDelegate, AVAudioPlayerDelegate {
    static let shared = ReadAloud()
    /// 设置 → 语音输入后朗读回复: a question asked by voice is answered aloud (as Pi does).
    nonisolated static let afterVoiceKey = "companion.read-after-voice"

    /// The message being read (either fetching neural audio or playing).
    @Published private(set) var speakingID: String?
    /// The message currently fetching/synthesizing neural TTS audio.
    @Published private(set) var loadingID: String?

    private let synthesizer = AVSpeechSynthesizer()
    private var audioPlayer: AVAudioPlayer?
    private var activePlayerID: ObjectIdentifier?
    private var neuralTask: Task<Void, Never>?
    private var playbackGeneration = UUID()

    override private init() {
        super.init()
        synthesizer.delegate = self
    }

    func toggle(_ id: String, text: String, store: CompanionStore? = nil) {
        if speakingID == id { stop() } else { speak(id, text: text, store: store) }
    }

    func speak(_ id: String, text: String, store: CompanionStore? = nil) {
        let words = SpokenText.of(text)
        guard !words.isEmpty else { return }
        cancelPlayback(deactivateAudioSession: false)
        let currentGen = UUID()
        playbackGeneration = currentGen

        if let store, store.ttsAvailable, store.connected, let api = store.api {
            speakingID = id
            loadingID = id
            neuralTask = Task { @MainActor [weak self] in
                guard let self else { return }
                await self.playNeuralOrFallback(id: id, words: words, api: api, generation: currentGen)
            }
            return
        }

        speakSystem(id, words: words)
    }

    func stop() {
        playbackGeneration = UUID()
        cancelPlayback(deactivateAudioSession: true)
    }

    private func cancelPlayback(deactivateAudioSession: Bool) {
        neuralTask?.cancel()
        neuralTask = nil
        if let player = audioPlayer {
            player.delegate = nil
            player.stop()
            audioPlayer = nil
            activePlayerID = nil
        }
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        loadingID = nil
        speakingID = nil
        if deactivateAudioSession {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    private func playNeuralOrFallback(id: String, words: String, api: APIClient, generation: UUID) async {
        let cacheURL = LocalStore.url("tts", server: api.baseURL.absoluteString, name: words)
        do {
            var audioData: Data? = await Task.detached(priority: .userInitiated) {
                LocalStore.read(cacheURL)
            }.value
            guard generation == playbackGeneration, !Task.isCancelled else { return }

            if audioData == nil || audioData?.isEmpty == true {
                let response = try await api.post("/api/webui/companion/tts", payload: ["text": .string(words)])
                guard generation == playbackGeneration, !Task.isCancelled else { return }
                guard response["ok"].boolValue, let mediaPath = response["url"].nonemptyString else {
                    throw ServiceError.message("语音生成失败")
                }
                let downloaded = try await api.getData(mediaPath, authenticated: false)
                guard generation == playbackGeneration, !Task.isCancelled else { return }
                guard !downloaded.isEmpty else { throw ServiceError.message("语音文件为空") }
                audioData = downloaded
                Task.detached(priority: .utility) { LocalStore.write(downloaded, to: cacheURL) }
            }

            guard let data = audioData, !data.isEmpty else { throw ServiceError.message("语音文件为空") }
            try activatePlaybackSession()
            let player = try AVAudioPlayer(data: data)
            player.delegate = self
            guard player.prepareToPlay(), player.play() else {
                throw ServiceError.message("语音播放启动失败")
            }
            guard generation == playbackGeneration, !Task.isCancelled else {
                player.delegate = nil
                player.stop()
                return
            }
            audioPlayer = player
            activePlayerID = ObjectIdentifier(player)
            loadingID = nil
            speakingID = id
        } catch is CancellationError {
            return
        } catch {
            guard generation == playbackGeneration, !Task.isCancelled else { return }
            loadingID = nil
            speakSystem(id, words: words)
        }
    }

    private func activatePlaybackSession() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try session.setActive(true)
    }

    private func speakSystem(_ id: String, words: String) {
        if let player = audioPlayer {
            player.delegate = nil
            player.stop()
            audioPlayer = nil
            activePlayerID = nil
        }
        synthesizer.stopSpeaking(at: .immediate)
        try? activatePlaybackSession()
        let utterance = AVSpeechUtterance(string: words)
        utterance.voice = Self.voice(chinese: SpokenText.isChinese(words))
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        loadingID = nil
        speakingID = id
        synthesizer.speak(utterance)
    }

    private func finished() {
        guard !synthesizer.isSpeaking, audioPlayer == nil, loadingID == nil else { return }
        speakingID = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func audioFinished(_ playerID: ObjectIdentifier) {
        guard activePlayerID == playerID else { return }
        audioPlayer?.delegate = nil
        audioPlayer = nil
        activePlayerID = nil
        finished()
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished() }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished() }
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let playerID = ObjectIdentifier(player)
        Task { @MainActor in self.audioFinished(playerID) }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        let playerID = ObjectIdentifier(player)
        Task { @MainActor in self.audioFinished(playerID) }
    }

    /// The best installed voice for the language (premium, then enhanced, then the default one).
    private static func voice(chinese: Bool) -> AVSpeechSynthesisVoice? {
        let language = chinese ? "zh-CN" : "en-US"
        let installed = AVSpeechSynthesisVoice.speechVoices().filter { $0.language == language }
        return installed.max { $0.quality.rawValue < $1.quality.rawValue } ?? AVSpeechSynthesisVoice(language: language)
    }
}
