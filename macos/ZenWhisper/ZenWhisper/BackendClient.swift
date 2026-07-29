import Foundation

typealias BackendRequestTransport = @Sendable (
    [String: Any],
    TimeInterval,
    (([String: Any]) throws -> Void)?
) throws -> [String: Any]

enum BackendClientError: Error {
    case backendPythonMissing(URL)
    case processAlreadyRunning
    case processNotRunning
    case healthTimeout(String)
    case staleSocketRemovalFailed(String)
    case authTokenWriteFailed(String)
}

struct BackendStopResult: Equatable {
    let stopped: Bool
    let cleanExit: Bool
    let terminationStatus: Int32?

    var summary: String {
        guard stopped else {
            return "backend did not stop"
        }
        guard let terminationStatus else {
            return "backend stopped without process status"
        }
        return cleanExit
            ? "backend stopped cleanly status=\(terminationStatus)"
            : "backend stopped with nonzero status=\(terminationStatus)"
    }
}

final class BackendClient: @unchecked Sendable {
    static let supportedProtocolVersion = 2
    static let recognitionTimeoutSeconds: TimeInterval = 600
    static let responseGraceSeconds: TimeInterval = 15

    private let paths: AppPaths
    private var process: Process?
    private let authToken = "\(UUID().uuidString)-\(UUID().uuidString)"
    private let requestTransport: BackendRequestTransport

    init(
        paths: AppPaths,
        process: Process? = nil,
        requestTransport: BackendRequestTransport? = nil
    ) {
        self.paths = paths
        self.process = process
        self.requestTransport = requestTransport ?? {
            request,
            timeoutSeconds,
            onProgress in
            try UnixSocketClient(
                socketPath: paths.socketPath.path,
                timeoutSeconds: timeoutSeconds
            ).request(request, onProgress: onProgress)
        }
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
        self.process = process
        do {
            if let token = "\(authToken)\n".data(using: .utf8) {
                try authPipe.fileHandleForWriting.write(contentsOf: token)
            }
            try authPipe.fileHandleForWriting.close()
        } catch {
            let stopResult = stopDetailed()
            throw BackendClientError.authTokenWriteFailed("\(String(describing: error)); \(stopResult.summary)")
        }
    }

    @discardableResult
    func stop() -> Bool {
        stopDetailed().stopped
    }

    @discardableResult
    func stopDetailed() -> BackendStopResult {
        guard let process else {
            return BackendStopResult(stopped: true, cleanExit: true, terminationStatus: nil)
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
        let status = stopped ? process.terminationStatus : nil
        let cleanExit = stopped && status == 0
        if stopped {
            if !cleanExit {
                NSLog("zen-whisper: backend stopped with nonzero status %d", status ?? -1)
            }
            self.process = nil
        }
        return BackendStopResult(stopped: stopped, cleanExit: cleanExit, terminationStatus: status)
    }

    func health() throws -> [String: Any] {
        let request = BackendRequest.health()
        let response = try send(request, expectedType: "health_result", timeoutSeconds: 30)
        let protocolVersion = response["protocol_version"] as? Int
        guard protocolVersion == Self.supportedProtocolVersion else {
            throw BackendProtocolError.protocolMismatch(
                expected: Self.supportedProtocolVersion,
                actual: protocolVersion
            )
        }
        return response
    }

    func waitForHealth(timeout: TimeInterval = 30) throws -> [String: Any] {
        let deadline = Date().addingTimeInterval(timeout)
        var lastError: Error?
        while Date() < deadline {
            if process?.isRunning == false {
                let tail = startupLogTail()
                throw BackendClientError.healthTimeout(
                    healthFailureDetail(
                        prefix: processExitSummary(),
                        startupLogTail: tail,
                        lastError: lastError
                    )
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
        throw BackendClientError.healthTimeout(
            healthFailureDetail(
                prefix: "backend health timed out",
                startupLogTail: tail,
                lastError: lastError
            )
        )
    }

    func preload(engine: String, model: String, language: String) throws {
        let request = BackendRequest.preload(engine: engine, model: model, language: language)
        _ = try send(request, expectedType: "ready", timeoutSeconds: 300)
    }

    func transcribe(
        audioURL: URL,
        engine: String,
        model: String,
        language: String,
        profile: EnhancementProfile? = nil,
        postprocessor: BackendPostprocessorRequest = .off,
        onProgress: ((BackendProgressStage) throws -> Void)? = nil
    ) throws -> BackendTranscriptionResult {
        let request = BackendRequest.transcribe(
            audioPath: audioURL.path,
            engine: engine,
            model: model,
            language: language,
            profile: profile,
            postprocessor: postprocessor
        )
        let response = try send(
            request,
            expectedType: "result",
            timeoutSeconds: Self.transcriptionTimeout(postprocessor: postprocessor),
            onProgress: onProgress
        )
        return try decodeBackendTranscriptionResult(
            response,
            requireEnhancementMetadata:
                profile != nil || postprocessor.requestsStructuredEnhancementResponse
        )
    }

    static func transcriptionTimeout(
        postprocessor: BackendPostprocessorRequest
    ) -> TimeInterval {
        recognitionTimeoutSeconds
            + postprocessor.additionalClientTimeoutSeconds
            + responseGraceSeconds
    }

    private func send(
        _ request: [String: Any],
        expectedType: String,
        timeoutSeconds: TimeInterval,
        onProgress: ((BackendProgressStage) throws -> Void)? = nil
    ) throws -> [String: Any] {
        let authorizedRequest = authorized(request)
        let response = try requestTransport(
            authorizedRequest,
            timeoutSeconds
        ) { progressResponse in
            let stage = try decodeBackendProgress(
                progressResponse,
                request: authorizedRequest
            )
            try onProgress?(stage)
        }
        return try validateBackendResponse(
            response,
            request: authorizedRequest,
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

    private func healthFailureDetail(prefix: String, startupLogTail tail: String, lastError: Error?) -> String {
        var parts = [prefix]
        if let lastError {
            parts.append("last error: \(String(describing: lastError))")
        }
        if tail.isEmpty {
            parts.append("startup log: <empty>")
        } else {
            parts.append("startup log:\n\(tail)")
        }
        return parts.joined(separator: "\n")
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
