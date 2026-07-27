import CoreFoundation
import Foundation

enum BackendPostprocessorRequest: Equatable, Sendable {
    case off
    case dictionary
    case preset(EnhancementPostprocessorPreset)

    static let maximumPresetTimeoutSeconds: TimeInterval =
        EnhancementCatalogLimits.maximumPresetTimeoutSeconds

    var requestsStructuredEnhancementResponse: Bool {
        self != .off
    }

    var additionalClientTimeoutSeconds: TimeInterval {
        guard case .preset(let preset) = self else {
            return 0
        }
        let boundedPresetTimeout = min(
            max(0, preset.timeoutSeconds),
            Self.maximumPresetTimeoutSeconds
        )
        let preflightTimeout: TimeInterval = preset.preflightExecutable.isEmpty ? 0 : 10
        return preflightTimeout + boundedPresetTimeout
    }

    var logIdentifier: String {
        switch self {
        case .off:
            return "off"
        case .dictionary:
            return "dictionary"
        case .preset(let preset):
            return preset.id
        }
    }

    fileprivate var backendPayload: [String: Any]? {
        switch self {
        case .off:
            return nil
        case .dictionary:
            return ["mode": "dictionary"]
        case .preset(let preset):
            return [
                "mode": "preset",
                "preset": preset.backendPayload
            ]
        }
    }
}

struct BackendRequest {
    static func health() -> [String: Any] {
        ["type": "health", "request_id": UUID().uuidString]
    }

    static func preload(engine: String, model: String, language: String) -> [String: Any] {
        [
            "type": "preload",
            "request_id": UUID().uuidString,
            "engine": engine,
            "model": model,
            "language": language
        ]
    }

    static func transcribe(
        audioPath: String,
        engine: String,
        model: String,
        language: String,
        profile: EnhancementProfile? = nil,
        postprocessor: BackendPostprocessorRequest = .off
    ) -> [String: Any] {
        var request: [String: Any] = [
            "type": "transcribe",
            "request_id": UUID().uuidString,
            "audio_path": audioPath,
            "engine": engine,
            "model": model,
            "language": language
        ]
        if let profile {
            request["profile"] = profile.backendPayload
        }
        if let postprocessorPayload = postprocessor.backendPayload {
            request["postprocessor"] = postprocessorPayload
        }
        return request
    }

    static func shutdown() -> [String: Any] {
        ["type": "shutdown", "request_id": UUID().uuidString]
    }
}

enum BackendProtocolError: Error, Equatable, Sendable {
    case invalidJSON
    case invalidBackendError(String)
    case invalidProgress(String)
    case invalidTranscriptionResult(String)
    case backendError(code: String, message: String, recoverable: Bool)
    case unexpectedResponse(String)
    case requestIDMismatch(expected: String, actual: String?)
    case protocolMismatch(expected: Int, actual: Int?)
}

enum BackendEnhancementOutcome: String, Equatable, Sendable {
    case raw
    case dictionary
    case cli
    case cliFallback = "cli_fallback"
}

enum BackendProgressStage: String, Equatable, Sendable {
    case postprocessing
}

struct BackendEnhancementResult: Equatable, Sendable {
    let outcome: BackendEnhancementOutcome
    let cliSelected: Bool
    let succeeded: Bool
    let applied: Bool
    let warningCode: String?
    let error: String?
    let elapsedSeconds: TimeInterval?

    static let legacyRaw = BackendEnhancementResult(
        outcome: .raw,
        cliSelected: false,
        succeeded: true,
        applied: false,
        warningCode: nil,
        error: nil,
        elapsedSeconds: nil
    )
}

struct BackendTranscriptionResult: Equatable, Sendable {
    let text: String
    let hintsApplied: Bool
    let enhancement: BackendEnhancementResult
}

private let nonRecoverableBackendErrorCodes: Set<String> = [
    "AUTH_FAILED",
    "BACKEND_ERROR",
    "BACKEND_SHUTTING_DOWN",
    "INVALID_REQUEST",
    "PROTOCOL_ERROR",
    "UNKNOWN_REQUEST"
]

func decodeBackendResponse(_ data: Data) throws -> [String: Any] {
    let dict = try decodeBackendResponseObject(data)
    if dict["type"] as? String == "error" {
        throw try backendError(from: dict)
    }
    return dict
}

func decodeBackendResponseObject(_ data: Data) throws -> [String: Any] {
    let object: Any
    do {
        object = try JSONSerialization.jsonObject(with: data)
    } catch {
        throw BackendProtocolError.invalidJSON
    }
    guard let dict = object as? [String: Any],
          (dict["type"] as? String)?.isEmpty == false else {
        throw BackendProtocolError.invalidJSON
    }
    return dict
}

func validateBackendResponse(
    _ response: [String: Any],
    request: [String: Any],
    expectedType: String
) throws -> [String: Any] {
    let expectedRequestID = request["request_id"] as? String
    let actualRequestID = response["request_id"] as? String
    guard expectedRequestID == actualRequestID else {
        throw BackendProtocolError.requestIDMismatch(
            expected: expectedRequestID ?? "",
            actual: actualRequestID
        )
    }
    guard let type = response["type"] as? String, !type.isEmpty else {
        throw BackendProtocolError.unexpectedResponse("<missing>")
    }
    if type == "error" {
        throw try backendError(from: response)
    }
    guard type == expectedType else {
        throw BackendProtocolError.unexpectedResponse(type)
    }
    return response
}

func decodeBackendProgress(
    _ response: [String: Any],
    request: [String: Any]
) throws -> BackendProgressStage {
    guard request["type"] as? String == "transcribe" else {
        throw BackendProtocolError.invalidProgress(
            "unexpected request type"
        )
    }
    let validated = try validateBackendResponse(
        response,
        request: request,
        expectedType: "progress"
    )
    guard let rawStage = validated["stage"] as? String,
          let stage = BackendProgressStage(rawValue: rawStage) else {
        throw BackendProtocolError.invalidProgress("invalid stage")
    }
    return stage
}

func decodeBackendTranscriptionResult(
    _ response: [String: Any],
    requireEnhancementMetadata: Bool = false
) throws -> BackendTranscriptionResult {
    guard let text = response["text"] as? String else {
        throw BackendProtocolError.invalidTranscriptionResult("missing text")
    }

    if requireEnhancementMetadata,
       response["hints_applied"] == nil || response["enhancement"] == nil {
        throw BackendProtocolError.invalidTranscriptionResult(
            "missing enhancement metadata"
        )
    }

    let hintsApplied: Bool
    if let value = response["hints_applied"] {
        guard let decoded = value as? Bool else {
            throw BackendProtocolError.invalidTranscriptionResult("invalid hints_applied")
        }
        hintsApplied = decoded
    } else {
        hintsApplied = false
    }

    let enhancement: BackendEnhancementResult
    if let value = response["enhancement"] {
        guard let object = value as? [String: Any] else {
            throw BackendProtocolError.invalidTranscriptionResult("invalid enhancement")
        }
        enhancement = try decodeBackendEnhancementResult(
            object,
            requireElapsedSeconds: requireEnhancementMetadata
        )
    } else {
        enhancement = .legacyRaw
    }

    return BackendTranscriptionResult(
        text: text.trimmingCharacters(in: .whitespacesAndNewlines),
        hintsApplied: hintsApplied,
        enhancement: enhancement
    )
}

private func decodeBackendEnhancementResult(
    _ object: [String: Any],
    requireElapsedSeconds: Bool
) throws -> BackendEnhancementResult {
    guard let outcomeValue = object["outcome"] as? String,
          let outcome = BackendEnhancementOutcome(rawValue: outcomeValue) else {
        throw BackendProtocolError.invalidTranscriptionResult("invalid enhancement outcome")
    }
    guard let cliSelected = object["cli_selected"] as? Bool else {
        throw BackendProtocolError.invalidTranscriptionResult("invalid enhancement cli_selected")
    }
    guard let succeeded = object["succeeded"] as? Bool else {
        throw BackendProtocolError.invalidTranscriptionResult("invalid enhancement succeeded")
    }
    guard let applied = object["applied"] as? Bool else {
        throw BackendProtocolError.invalidTranscriptionResult("invalid enhancement applied")
    }

    let warningCode = try optionalNonemptyString(
        object["warning_code"],
        field: "enhancement warning_code"
    )
    let error = try optionalNonemptyString(
        object["error"],
        field: "enhancement error"
    )
    let elapsedSeconds: TimeInterval?
    if let value = object["elapsed_sec"] {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite,
              number.doubleValue >= 0 else {
            throw BackendProtocolError.invalidTranscriptionResult(
                "invalid enhancement elapsed_sec"
            )
        }
        elapsedSeconds = number.doubleValue
    } else if requireElapsedSeconds {
        throw BackendProtocolError.invalidTranscriptionResult(
            "missing enhancement elapsed_sec"
        )
    } else {
        elapsedSeconds = nil
    }

    let outcomeSelectsCLI = outcome == .cli || outcome == .cliFallback
    guard cliSelected == outcomeSelectsCLI else {
        throw BackendProtocolError.invalidTranscriptionResult(
            "contradictory enhancement cli_selected"
        )
    }
    switch outcome {
    case .raw:
        guard succeeded, !applied, warningCode == nil, error == nil else {
            throw BackendProtocolError.invalidTranscriptionResult(
                "contradictory raw enhancement"
            )
        }
    case .dictionary:
        guard succeeded, warningCode == nil, error == nil else {
            throw BackendProtocolError.invalidTranscriptionResult(
                "contradictory dictionary enhancement"
            )
        }
    case .cli:
        guard succeeded, applied, warningCode == nil, error == nil else {
            throw BackendProtocolError.invalidTranscriptionResult(
                "contradictory cli enhancement"
            )
        }
    case .cliFallback:
        guard !succeeded, applied, warningCode != nil, error != nil else {
            throw BackendProtocolError.invalidTranscriptionResult(
                "contradictory cli fallback enhancement"
            )
        }
    }

    return BackendEnhancementResult(
        outcome: outcome,
        cliSelected: cliSelected,
        succeeded: succeeded,
        applied: applied,
        warningCode: warningCode,
        error: error,
        elapsedSeconds: elapsedSeconds
    )
}

private func optionalNonemptyString(
    _ value: Any?,
    field: String
) throws -> String? {
    guard let value, !(value is NSNull) else {
        return nil
    }
    guard let string = value as? String, !string.isEmpty else {
        throw BackendProtocolError.invalidTranscriptionResult("invalid \(field)")
    }
    return string
}

private func backendError(from response: [String: Any]) throws -> BackendProtocolError {
    guard let code = response["code"] as? String, !code.isEmpty else {
        throw BackendProtocolError.invalidBackendError("missing code")
    }
    guard let message = response["message"] as? String, !message.isEmpty else {
        throw BackendProtocolError.invalidBackendError("missing message")
    }
    guard let recoverable = response["recoverable"] as? Bool else {
        throw BackendProtocolError.invalidBackendError("missing recoverable")
    }
    if recoverable, nonRecoverableBackendErrorCodes.contains(code.uppercased()) {
        throw BackendProtocolError.invalidBackendError("contradictory recoverable")
    }
    return .backendError(code: code, message: message, recoverable: recoverable)
}
