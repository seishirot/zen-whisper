import Foundation

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

    static func transcribe(audioPath: String, engine: String, model: String, language: String) -> [String: Any] {
        [
            "type": "transcribe",
            "request_id": UUID().uuidString,
            "audio_path": audioPath,
            "engine": engine,
            "model": model,
            "language": language
        ]
    }

    static func shutdown() -> [String: Any] {
        ["type": "shutdown", "request_id": UUID().uuidString]
    }
}

enum BackendProtocolError: Error, Equatable {
    case invalidJSON
    case invalidBackendError(String)
    case backendError(code: String, message: String, recoverable: Bool)
    case unexpectedResponse(String)
    case requestIDMismatch(expected: String, actual: String?)
    case protocolMismatch(expected: Int, actual: Int?)
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
