import AVFoundation
import Combine
import Foundation

@MainActor
final class VoiceRecorder: ObservableObject {
    @Published private(set) var recording = false
    @Published private(set) var transcribing = false
    @Published private(set) var requestingPermission = false
    @Published var error: String?
    private var recorder: AVAudioRecorder?
    private var file: URL?
    private var started = Date()
    private var limit: Task<Void, Never>?
    private var generation = UUID()

    func start() async {
        guard !recording, !transcribing, !requestingPermission else { return }
        let attempt = UUID(); generation = attempt
        requestingPermission = true
        defer { requestingPermission = false }
        error = nil
        let allowed = await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { continuation.resume(returning: $0) }
        }
        guard generation == attempt else { return }
        guard allowed else { error = "请在系统设置中允许 Muse Bot 使用麦克风。"; return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .default)
            try session.setActive(true)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("companion-voice-\(UUID().uuidString).m4a")
            file = url
            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16_000,
                                           AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32_000,
                                           AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue]
            let next = try AVAudioRecorder(url: url, settings: settings)
            guard next.record() else { throw URLError(.cannotCreateFile) }
            recorder = next; file = url; recording = true; started = Date()
            limit = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(120)) } catch { return }
                self?.cancel(); self?.error = "录音最长 2 分钟，请重新录制短一些的内容。"
            }
        } catch { self.error = "无法开始录音，请稍后重试。"; cancel() }
    }

    func finish(using store: CompanionStore) async -> String? {
        guard recording, let url = file else { return nil }
        let attempt = generation
        let milliseconds = Date().timeIntervalSince(started) * 1_000
        stopRecording(); transcribing = true
        defer {
            if generation == attempt { transcribing = false; file = nil }
            try? FileManager.default.removeItem(at: url)
        }
        do {
            let data = try Data(contentsOf: url)
            guard !data.isEmpty else { throw URLError(.zeroByteResource) }
            let transcript = try await store.transcribeAudio(dataURL: "data:audio/mp4;base64," + data.base64EncodedString(), durationMS: milliseconds)
            return generation == attempt ? transcript : nil
        } catch {
            if generation == attempt {
                self.error = (error as? ServiceError)?.localizedDescription ?? "语音识别失败，请检查连接后重新录制。"
            }
            return nil
        }
    }

    func cancel() {
        generation = UUID()
        transcribing = false
        stopRecording()
        if let file { try? FileManager.default.removeItem(at: file) }; file = nil
    }
    private func stopRecording() {
        limit?.cancel(); limit = nil; recorder?.stop(); recorder = nil; recording = false
        try? AVAudioSession.sharedInstance().setActive(false)
    }
}
