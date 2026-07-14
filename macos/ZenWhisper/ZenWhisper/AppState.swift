import Foundation

enum AppState: Equatable {
    case idle
    case inputWaiting
    case pasteUnavailable(String)
    case recording(elapsed: TimeInterval, voiceActive: Bool)
    case preloading(message: String)
    case transcribing
    case copied(pasteDispatched: Bool, reason: String?)
    case copySkipped(String)
    case copyFailed(String)
    case modelUnavailable(String)
    case backendRepairRequired(String)
    case repairingBackend
    case microphoneError(String)
    case hotkeyError(String)
    case appSignatureChanged
    case error(String)

    var title: String {
        switch self {
        case .idle:
            return "ZW"
        case .inputWaiting:
            return ""
        case .pasteUnavailable:
            return "AX"
        case .recording(let elapsed, _):
            return StatusText.elapsed(elapsed)
        case .preloading:
            return "Loading"
        case .transcribing:
            return "Processing"
        case .copied(let pasteDispatched, let reason):
            if pasteDispatched {
                if reason?.lowercased().contains("enter attempted") == true {
                    return "Paste tried + Enter"
                }
                if reason?.lowercased().contains("kept") == true {
                    return "Paste tried"
                }
                return "Paste tried"
            }
            return reason.map { "Copied: \(StatusText.copyOnlyReason($0))" } ?? "Copied"
        case .copySkipped(let reason):
            return "Skipped: \(StatusText.copyOnlyReason(reason))"
        case .copyFailed:
            return "Copy failed"
        case .modelUnavailable:
            return "Model"
        case .backendRepairRequired:
            return "Repair"
        case .repairingBackend:
            return "Repairing"
        case .microphoneError:
            return "Mic"
        case .hotkeyError:
            return "Hotkey"
        case .appSignatureChanged:
            return "Signature"
        case .error:
            return "Error"
        }
    }

    var canStartRecording: Bool {
        switch self {
        case .inputWaiting, .pasteUnavailable, .copied, .copySkipped, .copyFailed, .microphoneError:
            return true
        case .idle, .recording, .preloading, .transcribing, .modelUnavailable,
             .backendRepairRequired, .repairingBackend, .hotkeyError, .appSignatureChanged, .error:
            return false
        }
    }

    var canToggleRecording: Bool {
        if case .recording = self {
            return true
        }
        return canStartRecording
    }
}

enum StatusText {
    static func elapsed(_ elapsed: TimeInterval) -> String {
        let totalSeconds = max(0, Int(elapsed.rounded(.down)))
        return String(format: "%02d:%02d", totalSeconds / 60, totalSeconds % 60)
    }

    static func copyOnlyReason(_ reason: String) -> String {
        let lower = reason.lowercased()
        if lower.contains("accessibility") {
            return "AX"
        }
        if lower.contains("changed") {
            return "Changed"
        }
        if lower.contains("editable") {
            return "Not editable"
        }
        if lower.contains("unsafe") {
            return "Unsafe"
        }
        if lower.contains("empty audio") || lower.contains("silent") {
            return "Silent"
        }
        if lower.contains("start") {
            return "No start"
        }
        if lower.contains("stop") {
            return "No stop"
        }
        if lower.contains("current") {
            return "No current"
        }
        if lower.contains("missing") {
            return "No target"
        }
        if lower.contains("event") {
            return "No paste"
        }
        if lower.contains("output mode") {
            return "Copy only"
        }
        if lower.contains("diagnostics") {
            return "Diagnostics"
        }
        return "Copy only"
    }

    static func visibleErrorSummary(_ message: String) -> String {
        let lower = message.lowercased()
        if lower.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "See logs"
        }
        if lower.contains("permission") || lower.contains("denied") || lower.contains("restricted") {
            return "Permission denied"
        }
        if lower.contains("microphone") || lower.contains("audio") || lower.contains("input device") {
            return "Audio unavailable"
        }
        if lower.contains("model") || lower.contains("download") || lower.contains("offline") {
            return "Model unavailable"
        }
        if lower.contains("pasteboard") {
            return "Pasteboard unavailable"
        }
        if lower.contains("hotkey") {
            return "Hotkey unavailable"
        }
        if lower.contains("backend") || lower.contains("protocol") || lower.contains("socket")
            || lower.contains("python") || lower.contains("manifest") || lower.contains("venv")
            || lower.contains("repair") {
            return "Backend unavailable"
        }
        return "See logs"
    }
}
