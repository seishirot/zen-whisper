import Darwin
import Foundation

enum UnixSocketError: Error, Equatable {
    case pathTooLong
    case socketFailed(Int32)
    case connectFailed(Int32)
    case timeoutSetupFailed(Int32)
    case writeFailed(Int32)
    case readFailed(Int32)
    case emptyResponse
    case requestTooLarge
    case responseTooLarge
    case tooManyProgressMessages
}

struct UnixSocketClient {
    static let maxRequestBytes = 524_288
    static let maxResponseBytes = 1_048_576
    static let maxProgressMessages = 8

    let socketPath: String
    var timeoutSeconds: TimeInterval = 30

    func request(
        _ message: [String: Any],
        onProgress: (([String: Any]) throws -> Void)? = nil
    ) throws -> [String: Any] {
        let payload = try Self.encodeRequest(message)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw UnixSocketError.socketFailed(errno)
        }
        defer { close(fd) }
        var timeout = timeval(tv_sec: max(1, Int(timeoutSeconds.rounded(.up))), tv_usec: 0)
        guard setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
            throw UnixSocketError.timeoutSetupFailed(errno)
        }
        guard setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
            throw UnixSocketError.timeoutSetupFailed(errno)
        }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8CString)
        let sunPathSize = MemoryLayout.size(ofValue: address.sun_path)
        guard pathBytes.count <= sunPathSize else {
            throw UnixSocketError.pathTooLong
        }
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: sunPathSize) { dest in
                for index in 0..<pathBytes.count {
                    dest[index] = pathBytes[index]
                }
            }
        }

        let pathOffset = MemoryLayout<UInt8>.size + MemoryLayout<sa_family_t>.size
        let length = socklen_t(pathOffset + pathBytes.count)
        address.sun_len = UInt8(length)
        let connectResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, length)
            }
        }
        guard connectResult == 0 else {
            throw UnixSocketError.connectFailed(errno)
        }

        try payload.withUnsafeBytes { bytes in
            var written = 0
            while written < payload.count {
                let result = Darwin.write(
                    fd,
                    bytes.baseAddress!.advanced(by: written),
                    payload.count - written
                )
                guard result > 0 else {
                    throw UnixSocketError.writeFailed(errno)
                }
                written += result
            }
        }

        var buffer = [UInt8](repeating: 0, count: 4096)
        return try Self.readResponse(
            readChunk: {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count < 0 {
                throw UnixSocketError.readFailed(errno)
            }
            if count == 0 {
                    return nil
            }
                return Data(buffer.prefix(count))
            },
            onProgress: onProgress
        )
    }

    static func readResponse(
        readChunk: () throws -> Data?,
        onProgress: (([String: Any]) throws -> Void)? = nil
    ) throws -> [String: Any] {
        var pending = Data()
        var progressCount = 0

        while let chunk = try readChunk() {
            pending.append(chunk)
            while let newlineIndex = pending.firstIndex(of: 0x0A) {
                let frame = Data(pending[..<newlineIndex])
                pending.removeSubrange(...newlineIndex)
                if let response = try decodeFrame(
                    frame,
                    progressCount: &progressCount,
                    onProgress: onProgress
                ) {
                    return response
                }
            }
            guard pending.count <= maxResponseBytes else {
                throw UnixSocketError.responseTooLarge
            }
        }

        if !pending.isEmpty,
           let response = try decodeFrame(
               pending,
               progressCount: &progressCount,
               onProgress: onProgress
           ) {
            return response
        }
        throw UnixSocketError.emptyResponse
    }

    private static func decodeFrame(
        _ frame: Data,
        progressCount: inout Int,
        onProgress: (([String: Any]) throws -> Void)?
    ) throws -> [String: Any]? {
        guard !frame.isEmpty else {
            throw UnixSocketError.emptyResponse
        }
        guard frame.count <= maxResponseBytes else {
            throw UnixSocketError.responseTooLarge
        }
        let response = try decodeBackendResponseObject(frame)
        guard response["type"] as? String == "progress" else {
            return response
        }
        progressCount += 1
        guard progressCount <= maxProgressMessages else {
            throw UnixSocketError.tooManyProgressMessages
        }
        try onProgress?(response)
        return nil
    }

    static func encodeRequest(_ message: [String: Any]) throws -> Data {
        var payload = try JSONSerialization.data(withJSONObject: message)
        payload.append(0x0A)
        guard payload.count <= maxRequestBytes else {
            throw UnixSocketError.requestTooLarge
        }
        return payload
    }
}
