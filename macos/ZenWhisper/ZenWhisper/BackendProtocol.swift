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
    case backendError(code: String, message: String, recoverable: Bool)
    case unexpectedResponse(String)
    case requestIDMismatch(expected: String, actual: String?)
    case protocolMismatch(expected: Int, actual: Int?)
}

func decodeBackendResponse(_ data: Data) throws -> [String: Any] {
    let object = try JSONSerialization.jsonObject(with: data)
    guard let dict = object as? [String: Any], let type = dict["type"] as? String else {
        throw BackendProtocolError.invalidJSON
    }
    if type == "error" {
        throw BackendProtocolError.backendError(
            code: dict["code"] as? String ?? "BACKEND_ERROR",
            message: dict["message"] as? String ?? "Unknown backend error",
            recoverable: dict["recoverable"] as? Bool ?? false
        )
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
    guard response["type"] as? String == expectedType else {
        throw BackendProtocolError.unexpectedResponse(response["type"] as? String ?? "<missing>")
    }
    return response
}
