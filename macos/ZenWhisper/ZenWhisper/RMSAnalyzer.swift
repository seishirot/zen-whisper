import AVFoundation
import Foundation

struct RMSSettings: Equatable {
    var maxRecordingSeconds: TimeInterval = 300
    var minRecordingSeconds: TimeInterval = 0.5
    var silenceAutoStopSeconds: TimeInterval = 10
    var windowSeconds: TimeInterval = 0.1
    var speechStartRMS: Float = 0.01
    var silenceRMS: Float = 0.002
    var leadingKeepSeconds: TimeInterval = 0.2
    var trailingKeepSeconds: TimeInterval = 0.5
    var emptyRMS: Float = 0.001
    var emptyPeak: Float = 0.01
}

enum RMSAnalyzer {
    static func rms(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else {
            return 0
        }
        let sum = samples.reduce(Float(0)) { $0 + ($1 * $1) }
        return sqrt(sum / Float(samples.count))
    }

    static func peak(_ samples: [Float]) -> Float {
        samples.map { abs($0) }.max() ?? 0
    }

    static func isEmptyAudio(samples: [Float], settings: RMSSettings = RMSSettings()) -> Bool {
        rms(samples) < settings.emptyRMS && peak(samples) < settings.emptyPeak
    }
}
