import AudioToolbox
import CoreAudio
import Foundation

enum AudioRecorderError: Error, CustomStringConvertible {
    case microphoneUnavailable
    case outputFormatUnavailable
    case alreadyRecording
    case notRecording
    case audioUnitFailed(step: String, status: OSStatus)

    var description: String {
        switch self {
        case .microphoneUnavailable:
            return "Microphone unavailable"
        case .outputFormatUnavailable:
            return "Microphone audio format unavailable"
        case .alreadyRecording:
            return "Already recording"
        case .notRecording:
            return "Not recording"
        case .audioUnitFailed(let step, let status):
            return "Microphone audio unit failed (\(step)): OSStatus \(status)"
        }
    }
}

final class AudioRecorder {
    private let paths: AppPaths
    private var capture: HALInputCapture?
    private var outputURL: URL?
    private var startDate: Date?
    private let captureQueue = DispatchQueue(label: "com.seishirot.zenwhisper.audio-capture")
    private var recordingID: UUID?
    private var inputSampleRate: Double = 16_000
    private var chunks: [[Float]] = []
    private var recentRMS: Float = 0
    private var lastSpeechDate: Date?

    init(paths: AppPaths) {
        self.paths = paths
    }

    var isRecording: Bool {
        capture != nil
    }

    var elapsed: TimeInterval {
        guard let startDate else {
            return 0
        }
        return Date().timeIntervalSince(startDate)
    }

    func start(deviceUID: String? = nil) throws {
        guard !isRecording else {
            throw AudioRecorderError.alreadyRecording
        }
        try paths.createPrivateDirectory(paths.recordings)
        let url = paths.recordings.appendingPathComponent("zw_tmp_\(UUID().uuidString).wav")
        // Resolve the device first and bind the audio unit to it before it is
        // initialized, so the system default input (e.g. a Bluetooth headset)
        // is never opened when another microphone is selected.
        guard let deviceID = try AudioDeviceManager.resolveInputDeviceID(uid: deviceUID) else {
            throw AudioRecorderError.microphoneUnavailable
        }

        let recordingID = UUID()
        let capture = try HALInputCapture(deviceID: deviceID) { [weak self] samples in
            guard let self else { return }
            self.captureQueue.async {
                if self.recordingID == recordingID {
                    self.chunks.append(samples)
                    self.recentRMS = RMSAnalyzer.rms(samples)
                    if self.recentRMS >= RecordingLevelDefaults.speechRMS {
                        self.lastSpeechDate = Date()
                    }
                }
            }
        }

        captureQueue.sync {
            self.recordingID = recordingID
            self.inputSampleRate = capture.sampleRate
            self.chunks = []
            self.recentRMS = 0
            self.lastSpeechDate = nil
        }

        outputURL = url
        startDate = Date()
        do {
            try capture.start()
        } catch {
            capture.dispose()
            captureQueue.sync { self.recordingID = nil }
            outputURL = nil
            startDate = nil
            throw error
        }
        self.capture = capture
    }

    func stop() throws -> RecordingResult {
        guard let capture, let outputURL else {
            throw AudioRecorderError.notRecording
        }
        capture.dispose()
        self.capture = nil
        let captured = captureQueue.sync {
            let captured = Capture(sampleRate: inputSampleRate, chunks: chunks)
            recordingID = nil
            chunks = []
            recentRMS = 0
            lastSpeechDate = nil
            return captured
        }
        startDate = nil
        self.outputURL = nil
        let samples = captured.flattenedSamples
        let isEmptyAudio = RMSAnalyzer.isEmptyAudio(samples: samples)
        try Self.writeWav16kMonoPCM16(
            samples: samples,
            sourceSampleRate: captured.sampleRate,
            to: outputURL
        )
        return RecordingResult(
            url: outputURL,
            isEmptyAudio: isEmptyAudio,
            rms: RMSAnalyzer.rms(samples),
            peak: RMSAnalyzer.peak(samples)
        )
    }

    @discardableResult
    func cancel() -> String? {
        let url = outputURL
        capture?.dispose()
        capture = nil
        captureQueue.sync {
            recordingID = nil
            chunks = []
            recentRMS = 0
            lastSpeechDate = nil
        }
        startDate = nil
        outputURL = nil
        if let url {
            return paths.removeRecording(url, context: "cancelled recording cleanup")
        }
        return nil
    }

    func levelSnapshot() -> RecordingLevelSnapshot {
        let now = Date()
        let elapsed = startDate.map { now.timeIntervalSince($0) } ?? 0
        let level = captureQueue.sync {
            RecordingLevel(
                recentRMS: recentRMS,
                lastSpeechDate: lastSpeechDate
            )
        }
        let voiceActive = level.lastSpeechDate.map {
            now.timeIntervalSince($0) <= RecordingLevelDefaults.voiceHold
        } ?? false
        let silenceReference = level.lastSpeechDate ?? startDate ?? now
        let silentFor = now.timeIntervalSince(silenceReference)
        let reachedMaxDuration = elapsed >= RecordingLevelDefaults.maxDuration
        let shouldAutoStop =
            elapsed >= RecordingLevelDefaults.minDuration
            && silentFor >= RecordingLevelDefaults.autoStopSilence
        return RecordingLevelSnapshot(
            elapsed: elapsed,
            recentRMS: level.recentRMS,
            voiceActive: voiceActive,
            shouldAutoStop: shouldAutoStop,
            reachedMaxDuration: reachedMaxDuration
        )
    }

    private static func writeWav16kMonoPCM16(
        samples: [Float],
        sourceSampleRate: Double,
        to url: URL
    ) throws {
        let output = resampleLinear(samples, sourceSampleRate: sourceSampleRate, targetSampleRate: 16_000)
        var data = Data()
        let bytesPerSample = 2
        let dataByteCount = output.count * bytesPerSample

        data.appendASCII("RIFF")
        data.appendUInt32LE(UInt32(36 + dataByteCount))
        data.appendASCII("WAVE")
        data.appendASCII("fmt ")
        data.appendUInt32LE(16)
        data.appendUInt16LE(1)
        data.appendUInt16LE(1)
        data.appendUInt32LE(16_000)
        data.appendUInt32LE(UInt32(16_000 * bytesPerSample))
        data.appendUInt16LE(UInt16(bytesPerSample))
        data.appendUInt16LE(16)
        data.appendASCII("data")
        data.appendUInt32LE(UInt32(dataByteCount))

        for sample in output {
            let clamped = max(-1.0, min(1.0, sample))
            let scaled = clamped < 0 ? clamped * 32768.0 : clamped * 32767.0
            data.appendInt16LE(Int16(scaled.rounded()))
        }
        try data.write(to: url, options: .atomic)
    }

    private static func resampleLinear(
        _ samples: [Float],
        sourceSampleRate: Double,
        targetSampleRate: Double
    ) -> [Float] {
        guard !samples.isEmpty else {
            return []
        }
        guard sourceSampleRate > 0, abs(sourceSampleRate - targetSampleRate) > 0.1 else {
            return samples
        }
        let ratio = targetSampleRate / sourceSampleRate
        let outputCount = max(1, Int((Double(samples.count) * ratio).rounded()))
        if outputCount == 1 {
            return [samples[0]]
        }
        return (0..<outputCount).map { index in
            let sourcePosition = Double(index) / ratio
            let lower = min(samples.count - 1, Int(sourcePosition.rounded(.down)))
            let upper = min(samples.count - 1, lower + 1)
            let fraction = Float(sourcePosition - Double(lower))
            return samples[lower] + (samples[upper] - samples[lower]) * fraction
        }
    }
}

struct RecordingLevelSnapshot: Equatable {
    let elapsed: TimeInterval
    let recentRMS: Float
    let voiceActive: Bool
    let shouldAutoStop: Bool
    let reachedMaxDuration: Bool
}

struct RecordingResult: Equatable {
    let url: URL
    let isEmptyAudio: Bool
    let rms: Float
    let peak: Float
}

private struct RecordingLevel {
    let recentRMS: Float
    let lastSpeechDate: Date?
}

private enum RecordingLevelDefaults {
    private static let settings = RMSSettings()
    static let minDuration: TimeInterval = settings.minRecordingSeconds
    static let maxDuration: TimeInterval = settings.maxRecordingSeconds
    static let autoStopSilence: TimeInterval = settings.silenceAutoStopSeconds
    static let voiceHold: TimeInterval = 0.45
    static let speechRMS: Float = settings.speechStartRMS
}

private struct Capture {
    let sampleRate: Double
    let chunks: [[Float]]

    var flattenedSamples: [Float] {
        let count = chunks.reduce(0) { $0 + $1.count }
        var samples: [Float] = []
        samples.reserveCapacity(count)
        for chunk in chunks {
            samples.append(contentsOf: chunk)
        }
        return samples
    }
}

private extension Data {
    mutating func appendASCII(_ value: String) {
        append(value.data(using: .ascii)!)
    }

    mutating func appendUInt16LE(_ value: UInt16) {
        var littleEndian = value.littleEndian
        append(Data(bytes: &littleEndian, count: MemoryLayout<UInt16>.size))
    }

    mutating func appendUInt32LE(_ value: UInt32) {
        var littleEndian = value.littleEndian
        append(Data(bytes: &littleEndian, count: MemoryLayout<UInt32>.size))
    }

    mutating func appendInt16LE(_ value: Int16) {
        var littleEndian = value.littleEndian
        append(Data(bytes: &littleEndian, count: MemoryLayout<Int16>.size))
    }
}
