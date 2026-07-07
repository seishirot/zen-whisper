import Foundation

enum BackendClientError: Error {
    case backendPythonMissing(URL)
    case processAlreadyRunning
    case processNotRunning
    case missingText
    case healthTimeout(String)
    case staleSocketRemovalFailed(String)
    case authTokenWriteFailed(String)
}

final class BackendClient: @unchecked Sendable {
    private let paths: AppPaths
    private var process: Process?
    private let authToken = "\(UUID().uuidString)-\(UUID().uuidString)"

    init(paths: AppPaths) {
        self.paths = paths
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() throws {
        if isRunning {
            throw BackendClientError.processAlreadyRunning
        }
        let python = resolvePython()
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw BackendClientError.backendPythonMissing(python)
        }
        if FileManager.default.fileExists(atPath: paths.socketPath.path) {
            do {
                try FileManager.default.removeItem(at: paths.socketPath)
            } catch {
                throw BackendClientError.staleSocketRemovalFailed(String(describing: error))
            }
        }

        let process = Process()
        process.executableURL = python
        process.arguments = [
            "-P",
            "-m",
            "zen_whisper_mac_backend.server",
            "--socket-path",
            paths.socketPath.path,
            "--log-dir",
            paths.logs.path,
            "--parent-pid",
            "\(ProcessInfo.processInfo.processIdentifier)",
            "--auth-token-stdin"
        ]
        process.currentDirectoryURL = paths.appSupport
        let environment = ProcessEnvironment.backend(
            appSupport: paths.appSupport,
            huggingFaceHome: paths.huggingFaceHome
        )
        process.environment = environment
        let authPipe = Pipe()
        process.standardInput = authPipe
        let startupLog = try FileHandle(forWritingTo: prepareStartupLog())
        process.standardOutput = startupLog
        process.standardError = startupLog
        try process.run()
        do {
            if let token = "\(authToken)\n".data(using: .utf8) {
                try authPipe.fileHandleForWriting.write(contentsOf: token)
            }
            try authPipe.fileHandleForWriting.close()
        } catch {
            process.terminate()
            throw BackendClientError.authTokenWriteFailed(String(describing: error))
        }
        self.process = process
    }

    @discardableResult
    func stop() -> Bool {
        guard let process else {
            return true
        }
        if process.isRunning {
            _ = try? UnixSocketClient(
                socketPath: paths.socketPath.path,
                timeoutSeconds: 2
            ).request(authorized(BackendRequest.shutdown()))
            Thread.sleep(forTimeInterval: 0.2)
            if process.isRunning {
                process.terminate()
                let deadline = Date().addingTimeInterval(2)
                while process.isRunning && Date() < deadline {
                    Thread.sleep(forTimeInterval: 0.05)
                }
            }
        }
        let stopped = !process.isRunning
        if stopped {
            self.process = nil
        }
        return stopped
    }

    func health() throws -> [String: Any] {
        let request = BackendRequest.health()
        let response = try send(request, expectedType: "health_result", timeoutSeconds: 30)
        let protocolVersion = response["protocol_version"] as? Int
        guard protocolVersion == 1 else {
            throw BackendProtocolError.protocolMismatch(expected: 1, actual: protocolVersion)
        }
        return response
    }

    func waitForHealth(timeout: TimeInterval = 30) throws -> [String: Any] {
        let deadline = Date().addingTimeInterval(timeout)
        var lastError: Error?
        while Date() < deadline {
            if process?.isRunning == false {
                throw BackendClientError.healthTimeout(
                    startupFailureDetail(prefix: processExitSummary())
                )
            }
            do {
                return try health()
            } catch let error as BackendProtocolError {
                throw error
            } catch {
                lastError = error
                Thread.sleep(forTimeInterval: 0.1)
            }
        }
        let tail = startupLogTail()
        if tail.isEmpty, let lastError {
            throw BackendClientError.healthTimeout("backend health timed out: \(String(describing: lastError))")
        }
        if tail.isEmpty {
            throw BackendClientError.healthTimeout("backend health timed out with no startup log output")
        }
        throw BackendClientError.healthTimeout(tail)
    }

    func preload(engine: String, model: String, language: String) throws {
        let request = BackendRequest.preload(engine: engine, model: model, language: language)
        _ = try send(request, expectedType: "ready", timeoutSeconds: 300)
    }

    func transcribe(audioURL: URL, engine: String, model: String, language: String) throws -> String {
        let request = BackendRequest.transcribe(
            audioPath: audioURL.path,
            engine: engine,
            model: model,
            language: language
        )
        let response = try send(request, expectedType: "result", timeoutSeconds: 600)
        guard let text = response["text"] as? String else {
            throw BackendClientError.missingText
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func send(
        _ request: [String: Any],
        expectedType: String,
        timeoutSeconds: TimeInterval
    ) throws -> [String: Any] {
        let response = try UnixSocketClient(
            socketPath: paths.socketPath.path,
            timeoutSeconds: timeoutSeconds
        ).request(authorized(request))
        return try validateBackendResponse(
            response,
            request: authorized(request),
            expectedType: expectedType
        )
    }

    private func authorized(_ request: [String: Any]) -> [String: Any] {
        var request = request
        request["auth_token"] = authToken
        return request
    }

    private func prepareStartupLog() throws -> URL {
        let logURL = paths.backendStartupLog
        try FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try Data().write(to: logURL, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: logURL.path)
        return logURL
    }

    private func processExitSummary() -> String {
        guard let process else {
            return "backend process is not running"
        }
        return "backend exited before health check status=\(process.terminationStatus)"
    }

    private func startupFailureDetail(prefix: String) -> String {
        let tail = startupLogTail()
        guard !tail.isEmpty else {
            return prefix
        }
        return "\(prefix)\n\(tail)"
    }

    private func startupLogTail(maxBytes: UInt64 = 8_192) -> String {
        let handle: FileHandle
        do {
            handle = try FileHandle(forReadingFrom: paths.backendStartupLog)
        } catch {
            return "[backend startup log unavailable: \(paths.backendStartupLog.path): \(error.localizedDescription)]"
        }
        defer {
            do {
                try handle.close()
            } catch {
                NSLog("zen-whisper: could not close backend startup log %@: %@", paths.backendStartupLog.path, String(describing: error))
            }
        }
        let size = (try? FileManager.default.attributesOfItem(atPath: paths.backendStartupLog.path)[.size] as? UInt64) ?? 0
        if size > maxBytes {
            do {
                try handle.seek(toOffset: size - maxBytes)
            } catch {
                NSLog("zen-whisper: could not seek backend startup log %@: %@", paths.backendStartupLog.path, String(describing: error))
            }
        }
        let data = handle.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private func resolvePython() -> URL {
        if AppRuntimeIdentity.allowsDevBackendOverride,
           let override = ProcessInfo.processInfo.environment["ZEN_WHISPER_BACKEND_PYTHON"],
           !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        return paths.backendPython
    }
}
