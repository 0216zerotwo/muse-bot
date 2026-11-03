import AVFoundation
import Foundation

/// 朗读: a reply read out with the iPhone's own voice (the best Chinese one installed), offline and at
/// once. One at a time: reading another stops the first; tapping the one being read stops it.
@MainActor
final class ReadAloud: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    static let shared = ReadAloud()
    /// 设置 → 语音输入后朗读回复: a question asked by voice is answered aloud (as Pi does).
    nonisolated static let afterVoiceKey = "companion.read-after-voice"

    /// The message being read.
    @Published private(set) var speakingID: String?
    private let synthesizer = AVSpeechSynthesizer()

    override private init() {
        super.init()
        synthesizer.delegate = self
    }

    func toggle(_ id: String, text: String) {
        if speakingID == id { stop() } else { speak(id, text: text) }
    }

    func speak(_ id: String, text: String) {
        let words = SpokenText.of(text)
        guard !words.isEmpty else { return }
        synthesizer.stopSpeaking(at: .immediate)
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? session.setActive(true)
        let utterance = AVSpeechUtterance(string: words)
        utterance.voice = Self.voice(chinese: SpokenText.isChinese(words))
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        speakingID = id
        synthesizer.speak(utterance)
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        finished()
    }

    private func finished() {
        guard !synthesizer.isSpeaking else { return }
        speakingID = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished() }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finished() }
    }

    /// The best installed voice for the language (premium, then enhanced, then the default one).
    private static func voice(chinese: Bool) -> AVSpeechSynthesisVoice? {
        let language = chinese ? "zh-CN" : "en-US"
        let installed = AVSpeechSynthesisVoice.speechVoices().filter { $0.language == language }
        return installed.max { $0.quality.rawValue < $1.quality.rawValue } ?? AVSpeechSynthesisVoice(language: language)
    }
}
