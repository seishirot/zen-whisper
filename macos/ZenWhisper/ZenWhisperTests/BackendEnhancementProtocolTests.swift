import Foundation
import XCTest
@testable import ZenWhisper

private final class CallbackOrderRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []

    func append(_ value: String) {
        lock.lock()
        defer { lock.unlock() }
        values.append(value)
    }

    func snapshot() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return values
    }
}

final class BackendEnhancementProtocolTests: XCTestCase {
    func testLegacyTranscribeRequestOmitsEnhancementPayloads() {
        let request = BackendRequest.transcribe(
            audioPath: "/tmp/audio.wav",
            engine: "mlx-whisper",
            model: "model",
            language: "ja"
        )

        XCTAssertNil(request["profile"])
        XCTAssertNil(request["postprocessor"])
    }

    func testTranscribeRequestEncodesProfileAndDictionary() throws {
        let profile = EnhancementProfile(
            id: "work",
            name: "Work",
            context: "Software review",
            terms: [
                EnhancementTerm(
                    canonical: "ZenWhisper",
                    spoken: ["zen whisper"],
                    replaceFrom: ["Zen Whisper"],
                    description: "Application name"
                )
            ]
        )

        let request = BackendRequest.transcribe(
            audioPath: "/tmp/audio.wav",
            engine: "mlx-whisper",
            model: "model",
            language: "ja",
            profile: profile,
            postprocessor: .dictionary
        )

        let encodedProfile = try XCTUnwrap(request["profile"] as? [String: Any])
        XCTAssertEqual(encodedProfile["id"] as? String, "work")
        XCTAssertEqual(encodedProfile["name"] as? String, "Work")
        XCTAssertNil(encodedProfile["profile_id"])
        let terms = try XCTUnwrap(encodedProfile["terms"] as? [[String: Any]])
        XCTAssertEqual(terms.first?["canonical"] as? String, "ZenWhisper")
        XCTAssertEqual(terms.first?["spoken"] as? [String], ["zen whisper"])
        XCTAssertEqual(terms.first?["replace_from"] as? [String], ["Zen Whisper"])

        let postprocessor = try XCTUnwrap(request["postprocessor"] as? [String: Any])
        XCTAssertEqual(postprocessor["mode"] as? String, "dictionary")
        XCTAssertNil(postprocessor["preset"])
    }

    func testTranscribeRequestEncodesPresetWithoutShellCommand() throws {
        let preset = EnhancementPostprocessorPreset(
            id: "local-tool",
            displayName: "Local Tool",
            executable: "/usr/local/bin/local-tool",
            arguments: ["--format", "{{prompt}}"],
            preflightExecutable: "/usr/bin/test",
            preflightArguments: ["-x", "/usr/local/bin/local-tool"],
            inputMode: .argument,
            destination: .local,
            timeoutSeconds: 12,
            promptTemplate: "{{transcript}}",
            environment: ["LOCAL_TOOL_MODE": "safe"]
        )

        let request = BackendRequest.transcribe(
            audioPath: "/tmp/audio.wav",
            engine: "mlx-whisper",
            model: "model",
            language: "ja",
            postprocessor: .preset(preset)
        )

        let postprocessor = try XCTUnwrap(request["postprocessor"] as? [String: Any])
        XCTAssertEqual(postprocessor["mode"] as? String, "preset")
        let encodedPreset = try XCTUnwrap(postprocessor["preset"] as? [String: Any])
        XCTAssertEqual(encodedPreset["id"] as? String, "local-tool")
        XCTAssertNil(encodedPreset["preset_id"])
        XCTAssertEqual(encodedPreset["executable"] as? String, "/usr/local/bin/local-tool")
        XCTAssertEqual(encodedPreset["arguments"] as? [String], ["--format", "{{prompt}}"])
        XCTAssertNil(encodedPreset["command"])

        let noPreflightRequest = BackendRequest.transcribe(
            audioPath: "/tmp/audio.wav",
            engine: "mlx-whisper",
            model: "model",
            language: "ja",
            postprocessor: .preset(
                EnhancementPostprocessorPreset(
                    id: "stdin-tool",
                    displayName: "Stdin Tool",
                    executable: "stdin-tool"
                )
            )
        )
        let noPreflightWrapper = try XCTUnwrap(
            noPreflightRequest["postprocessor"] as? [String: Any]
        )
        let noPreflightPreset = try XCTUnwrap(
            noPreflightWrapper["preset"] as? [String: Any]
        )
        XCTAssertNil(noPreflightPreset["preflight_executable"])
    }

    func testLegacyTranscriptionResultUsesSafeDefaults() throws {
        let result = try decodeBackendTranscriptionResult([
            "type": "result",
            "text": "  hello  "
        ])

        XCTAssertEqual(result.text, "hello")
        XCTAssertFalse(result.hintsApplied)
        XCTAssertEqual(result.enhancement, .legacyRaw)
        XCTAssertNil(result.enhancement.elapsedSeconds)
    }

    func testEnhancedRequestRequiresStructuredResponseMetadata() {
        XCTAssertThrowsError(
            try decodeBackendTranscriptionResult(
                [
                    "type": "result",
                    "text": "hello"
                ],
                requireEnhancementMetadata: true
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("missing enhancement metadata")
            )
        }

        XCTAssertThrowsError(
            try decodeBackendTranscriptionResult(
                [
                    "type": "result",
                    "text": "hello",
                    "hints_applied": false,
                    "enhancement": [
                        "outcome": "raw",
                        "cli_selected": false,
                        "succeeded": true,
                        "applied": false
                    ]
                ],
                requireEnhancementMetadata: true
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult(
                    "missing enhancement elapsed_sec"
                )
            )
        }

        XCTAssertThrowsError(
            try decodeBackendTranscriptionResult(
                [
                    "type": "result",
                    "text": "hello",
                    "hints_applied": true
                ],
                requireEnhancementMetadata: true
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("missing enhancement metadata")
            )
        }
    }

    func testStructuredTranscriptionResultDecodes() throws {
        let result = try decodeBackendTranscriptionResult([
            "type": "result",
            "text": "hello",
            "hints_applied": true,
            "enhancement": [
                "outcome": "cli_fallback",
                "cli_selected": true,
                "succeeded": false,
                "applied": true,
                "warning_code": "CLI_TIMEOUT",
                "error": "postprocessor timed out",
                "elapsed_sec": 1.234
            ]
        ])

        XCTAssertEqual(result.text, "hello")
        XCTAssertTrue(result.hintsApplied)
        XCTAssertEqual(result.enhancement.outcome, .cliFallback)
        XCTAssertTrue(result.enhancement.cliSelected)
        XCTAssertFalse(result.enhancement.succeeded)
        XCTAssertTrue(result.enhancement.applied)
        XCTAssertEqual(result.enhancement.warningCode, "CLI_TIMEOUT")
        XCTAssertEqual(result.enhancement.error, "postprocessor timed out")
        XCTAssertEqual(result.enhancement.elapsedSeconds, 1.234)
    }

    func testStructuredTranscriptionResultRejectsMalformedEnhancement() {
        XCTAssertThrowsError(try decodeBackendTranscriptionResult([
            "text": "hello",
            "hints_applied": "yes"
        ])) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("invalid hints_applied")
            )
        }

        XCTAssertThrowsError(try decodeBackendTranscriptionResult([
            "text": "hello",
            "enhancement": [
                "outcome": "cli",
                "cli_selected": false,
                "succeeded": true,
                "applied": true
            ]
        ])) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("contradictory enhancement cli_selected")
            )
        }

        XCTAssertThrowsError(try decodeBackendTranscriptionResult([
            "text": "hello",
            "enhancement": [
                "outcome": "future_mode",
                "cli_selected": false,
                "succeeded": true,
                "applied": false
            ]
        ])) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("invalid enhancement outcome")
            )
        }

        XCTAssertThrowsError(try decodeBackendTranscriptionResult([
            "text": "hello",
            "enhancement": [
                "outcome": "cli_fallback",
                "cli_selected": true,
                "succeeded": true,
                "applied": true,
                "warning_code": "CLI_TIMEOUT",
                "error": "timed out"
            ]
        ])) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult(
                    "contradictory cli fallback enhancement"
                )
            )
        }

        XCTAssertThrowsError(try decodeBackendTranscriptionResult([
            "text": "hello",
            "enhancement": [
                "outcome": "raw",
                "cli_selected": false,
                "succeeded": true,
                "applied": true
            ]
        ])) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidTranscriptionResult("contradictory raw enhancement")
            )
        }

        for invalidElapsed: Any in [
            -0.1,
            "fast",
            true,
            Double.nan,
            Double.infinity
        ] {
            XCTAssertThrowsError(try decodeBackendTranscriptionResult([
                "text": "hello",
                "enhancement": [
                    "outcome": "raw",
                    "cli_selected": false,
                    "succeeded": true,
                    "applied": false,
                    "elapsed_sec": invalidElapsed
                ]
            ])) { error in
                XCTAssertEqual(
                    error as? BackendProtocolError,
                    .invalidTranscriptionResult(
                        "invalid enhancement elapsed_sec"
                    )
                )
            }
        }
    }

    func testPostprocessingProgressRequiresMatchingRequestAndKnownStage() throws {
        let request: [String: Any] = [
            "type": "transcribe",
            "request_id": "request-1"
        ]
        XCTAssertEqual(
            try decodeBackendProgress(
                [
                    "type": "progress",
                    "request_id": "request-1",
                    "stage": "postprocessing"
                ],
                request: request
            ),
            .postprocessing
        )

        XCTAssertThrowsError(
            try decodeBackendProgress(
                [
                    "type": "progress",
                    "request_id": "wrong-request",
                    "stage": "postprocessing"
                ],
                request: request
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .requestIDMismatch(
                    expected: "request-1",
                    actual: "wrong-request"
                )
            )
        }

        XCTAssertThrowsError(
            try decodeBackendProgress(
                [
                    "type": "progress",
                    "request_id": "request-1",
                    "stage": "future-stage"
                ],
                request: request
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidProgress("invalid stage")
            )
        }

        XCTAssertThrowsError(
            try decodeBackendProgress(
                [
                    "type": "progress",
                    "request_id": "health-1",
                    "stage": "postprocessing"
                ],
                request: [
                    "type": "health",
                    "request_id": "health-1"
                ]
            )
        ) { error in
            XCTAssertEqual(
                error as? BackendProtocolError,
                .invalidProgress("unexpected request type")
            )
        }
    }

    func testSocketReaderHandlesFragmentedProgressBeforeFinalResult() throws {
        var chunks = [
            Data(
                """
                {"type":"progress","request_id":"request-1","stage":"post
                """.utf8
            ),
            Data(
                """
                processing"}
                {"type":"progress","request_id":"request-1","stage":"postprocessing"}
                {"type":"result","request_id":"request-1","text":"hello"}

                """.utf8
            )
        ]
        var progressResponses: [[String: Any]] = []

        let result = try UnixSocketClient.readResponse(
            readChunk: {
                chunks.isEmpty ? nil : chunks.removeFirst()
            },
            onProgress: { progressResponses.append($0) }
        )

        XCTAssertEqual(progressResponses.count, 2)
        XCTAssertEqual(progressResponses[0]["type"] as? String, "progress")
        XCTAssertEqual(
            progressResponses[0]["stage"] as? String,
            "postprocessing"
        )
        XCTAssertEqual(
            progressResponses[1]["stage"] as? String,
            "postprocessing"
        )
        XCTAssertEqual(result["type"] as? String, "result")
        XCTAssertEqual(result["text"] as? String, "hello")
    }

    func testSocketReaderBoundsProgressAndResponseFrames() {
        let progressFrame = Data(
            """
            {"type":"progress","request_id":"request-1","stage":"postprocessing"}

            """.utf8
        )
        var excessiveProgress = Array(
            repeating: progressFrame,
            count: UnixSocketClient.maxProgressMessages + 1
        )

        XCTAssertThrowsError(
            try UnixSocketClient.readResponse(
                readChunk: {
                    excessiveProgress.isEmpty
                        ? nil
                        : excessiveProgress.removeFirst()
                }
            )
        ) { error in
            XCTAssertEqual(
                error as? UnixSocketError,
                .tooManyProgressMessages
            )
        }

        var progressOnly = [progressFrame]
        XCTAssertThrowsError(
            try UnixSocketClient.readResponse(
                readChunk: {
                    progressOnly.isEmpty ? nil : progressOnly.removeFirst()
                }
            )
        ) { error in
            XCTAssertEqual(error as? UnixSocketError, .emptyResponse)
        }

        var oversizedPending = [
            Data(
                repeating: 0x78,
                count: UnixSocketClient.maxResponseBytes + 1
            )
        ]
        XCTAssertThrowsError(
            try UnixSocketClient.readResponse(
                readChunk: {
                    oversizedPending.isEmpty
                        ? nil
                        : oversizedPending.removeFirst()
                }
            )
        ) { error in
            XCTAssertEqual(error as? UnixSocketError, .responseTooLarge)
        }

        var oversizedTerminatedFrame = Data(
            repeating: 0x78,
            count: UnixSocketClient.maxResponseBytes + 1
        )
        oversizedTerminatedFrame.append(0x0A)
        var terminatedChunks = [oversizedTerminatedFrame]
        XCTAssertThrowsError(
            try UnixSocketClient.readResponse(
                readChunk: {
                    terminatedChunks.isEmpty
                        ? nil
                        : terminatedChunks.removeFirst()
                }
            )
        ) { error in
            XCTAssertEqual(error as? UnixSocketError, .responseTooLarge)
        }
    }

    func testSocketReaderReturnsTerminalErrorAfterProgress() throws {
        var chunks = [
            Data(
                """
                {"type":"progress","request_id":"request-1","stage":"postprocessing"}
                {"type":"error","request_id":"request-1","code":"BACKEND_ERROR","message":"failed","recoverable":false}

                """.utf8
            )
        ]
        var progressCount = 0

        let response = try UnixSocketClient.readResponse(
            readChunk: {
                chunks.isEmpty ? nil : chunks.removeFirst()
            },
            onProgress: { _ in progressCount += 1 }
        )

        XCTAssertEqual(progressCount, 1)
        XCTAssertEqual(response["type"] as? String, "error")
        XCTAssertEqual(response["code"] as? String, "BACKEND_ERROR")
    }

    func testEnhancementLogContainsOnlySafeOutcomeAndTimingMetadata() {
        let privateContent = "PRIVATE TRANSCRIPT AND CLI ERROR"
        let preset = EnhancementPostprocessorPreset(
            id: "codex",
            displayName: "Codex",
            executable: "codex"
        )
        let result = BackendEnhancementResult(
            outcome: .cliFallback,
            cliSelected: true,
            succeeded: false,
            applied: true,
            warningCode: "CLI_TIMEOUT",
            error: privateContent,
            elapsedSeconds: 2.3456
        )

        let message = AppDelegate.enhancementLogMessage(
            result,
            postprocessor: .preset(preset)
        )

        XCTAssertEqual(
            message,
            "enhancement result postprocessor=codex "
                + "outcome=cli_fallback cli_selected=true "
                + "succeeded=false applied=true "
                + "warning_code=CLI_TIMEOUT elapsed_sec=2.346"
        )
        XCTAssertFalse(message.contains(privateContent))
    }

    func testPostprocessingProgressOnlyAdvancesActiveTranscription() {
        XCTAssertEqual(
            AppDelegate.state(
                for: .postprocessing,
                currentState: .transcribing
            ),
            .postprocessing
        )
        XCTAssertNil(
            AppDelegate.state(
                for: .postprocessing,
                currentState: .inputWaiting
            )
        )
    }

    func testBackendClientForwardsValidatedProgressBeforeResult() throws {
        let root = URL(
            fileURLWithPath: NSTemporaryDirectory(),
            isDirectory: true
        ).appendingPathComponent(
            "zw-progress-client-\(UUID().uuidString)",
            isDirectory: true
        )
        let paths = AppPaths(
            appSupport: root.appendingPathComponent(
                "support",
                isDirectory: true
            ),
            logs: root.appendingPathComponent("logs", isDirectory: true),
            runtimeDirectoryOverride: root.appendingPathComponent(
                "runtime",
                isDirectory: true
            )
        )
        let callbackOrder = CallbackOrderRecorder()
        var currentState: AppState = .transcribing
        let client = BackendClient(
            paths: paths,
            requestTransport: { request, _, onProgress in
                let requestID = try XCTUnwrap(
                    request["request_id"] as? String
                )
                try onProgress?([
                    "type": "progress",
                    "request_id": requestID,
                    "stage": "postprocessing"
                ])
                callbackOrder.append("result")
                return [
                    "type": "result",
                    "request_id": requestID,
                    "text": "corrected",
                    "hints_applied": false,
                    "enhancement": [
                        "outcome": "dictionary",
                        "cli_selected": false,
                        "succeeded": true,
                        "applied": true,
                        "elapsed_sec": 0.125
                    ]
                ]
            }
        )

        let result = try client.transcribe(
            audioURL: root.appendingPathComponent("audio.wav"),
            engine: "mlx-whisper",
            model: "model",
            language: "ja",
            postprocessor: .dictionary,
            onProgress: { progress in
                XCTAssertEqual(progress, .postprocessing)
                currentState = AppDelegate.state(
                    for: progress,
                    currentState: currentState
                ) ?? currentState
                callbackOrder.append("progress")
            }
        )

        XCTAssertEqual(callbackOrder.snapshot(), ["progress", "result"])
        XCTAssertEqual(result.text, "corrected")
        XCTAssertEqual(result.enhancement.elapsedSeconds, 0.125)
        XCTAssertEqual(currentState, .postprocessing)
        XCTAssertEqual(AppState.postprocessing.title, "Post-processing")
        XCTAssertEqual(
            StatusIconFactory.kind(for: .postprocessing),
            .postprocessing
        )
    }

    func testBackendEnvironmentKeepsRestrictedParentPathAndAddsDedicatedCLIPath() {
        let appSupport = URL(fileURLWithPath: "/tmp/zen-whisper-support")
        let huggingFaceHome = appSupport.appendingPathComponent("models")
        let environment = ProcessEnvironment.backend(
            appSupport: appSupport,
            huggingFaceHome: huggingFaceHome
        )

        XCTAssertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        XCTAssertEqual(
            ProcessEnvironment.cliExecutablePath(homeDirectory: "/Users/example"),
            [
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                "/opt/homebrew/bin",
                "/opt/homebrew/sbin",
                "/usr/local/bin",
                "/usr/local/sbin",
                "/Users/example/.local/bin",
                "/Users/example/bin",
                "/Users/example/.local/share/mise/shims",
                "/Users/example/.local/share/mise/bin"
            ].joined(separator: ":")
        )
        XCTAssertEqual(
            environment["ZEN_WHISPER_CLI_PATH"],
            ProcessEnvironment.cliExecutablePath()
        )
        XCTAssertFalse(environment["PATH"]?.contains("/opt/homebrew/bin") == true)
    }

    func testClientTimeoutIncludesBoundedPostprocessorBudget() {
        XCTAssertEqual(
            BackendClient.transcriptionTimeout(postprocessor: .off),
            615
        )
        XCTAssertEqual(
            BackendClient.transcriptionTimeout(
                postprocessor: .preset(
                    EnhancementPostprocessorPreset(
                        id: "bounded",
                        displayName: "Bounded",
                        executable: "tool",
                        preflightExecutable: "test",
                        timeoutSeconds: 12
                    )
                )
            ),
            637
        )
        XCTAssertEqual(
            BackendClient.transcriptionTimeout(
                postprocessor: .preset(
                    EnhancementPostprocessorPreset(
                        id: "untrusted",
                        displayName: "Untrusted",
                        executable: "tool",
                        preflightExecutable: "test",
                        timeoutSeconds: 10_000
                    )
                )
            ),
            925,
            "Untrusted configuration must not create an unbounded socket timeout"
        )
    }

    func testSocketRequestEncodingRejectsOversizedEnhancementPayload() throws {
        let small = try UnixSocketClient.encodeRequest(["value": "ok"])
        XCTAssertLessThan(small.count, UnixSocketClient.maxRequestBytes)

        XCTAssertThrowsError(
            try UnixSocketClient.encodeRequest([
                "profile": String(
                    repeating: "x",
                    count: UnixSocketClient.maxRequestBytes
                )
            ])
        ) { error in
            XCTAssertEqual(error as? UnixSocketError, .requestTooLarge)
        }
    }

    func testCLIIntentSuppressesAutomaticSubmitEvenWhenFallbackIsUsed() {
        XCTAssertTrue(
            AppDelegate.shouldSubmitAfterPaste(
                requested: true,
                cliWasSelected: false
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldSubmitAfterPaste(
                requested: true,
                cliWasSelected: true
            )
        )
        XCTAssertFalse(
            AppDelegate.shouldSubmitAfterPaste(
                requested: false,
                cliWasSelected: false
            )
        )
    }

    func testPostprocessorApprovalMustMatchAnAvailableNonlocalPresetRevision() {
        let remotePreset = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote",
            executable: "remote-tool",
            destination: .remote
        )
        let approvedSelection = EnhancementSelection(
            postprocessing: .preset(remotePreset.id),
            approvedPostprocessorRevision: remotePreset.reviewRevision
        )
        let availableCatalog = EnhancementCatalogSnapshot(
            postprocessors: [remotePreset.id: remotePreset]
        )

        XCTAssertTrue(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: approvedSelection,
                catalog: availableCatalog
            )
        )
        XCTAssertFalse(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: EnhancementSelection(
                    postprocessing: .preset(remotePreset.id),
                    approvedPostprocessorRevision: "stale"
                ),
                catalog: availableCatalog
            )
        )
        XCTAssertFalse(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: approvedSelection,
                catalog: EnhancementCatalogSnapshot()
            )
        )
        XCTAssertFalse(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: approvedSelection,
                catalog: EnhancementCatalogSnapshot(
                    postprocessors: [remotePreset.id: remotePreset],
                    blockedPostprocessorIDs: [remotePreset.id]
                )
            )
        )
        XCTAssertFalse(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: approvedSelection,
                catalog: EnhancementCatalogSnapshot(
                    postprocessors: [remotePreset.id: remotePreset],
                    blocksAllPostprocessors: true
                )
            )
        )

        var localPreset = remotePreset
        localPreset.destination = .local
        let localSelection = EnhancementSelection(
            postprocessing: .preset(localPreset.id),
            approvedPostprocessorRevision: localPreset.reviewRevision
        )
        XCTAssertFalse(
            AppDelegate.postprocessorApprovalIsCurrent(
                selection: localSelection,
                catalog: EnhancementCatalogSnapshot(
                    postprocessors: [localPreset.id: localPreset]
                )
            ),
            "Local presets never retain remote-disclosure approval state"
        )
    }

    func testEnhancementWarningsUseAllowlistedUserVisibleMessages() {
        XCTAssertEqual(
            AppDelegate.preferredEnhancementWarningCode(
                requestWarningCode: "PROFILE_UNAVAILABLE",
                backendWarningCode: "CLI_TIMEOUT",
                engine: "mlx-whisper",
                profileHadRecognitionHints: true,
                hintsApplied: false
            ),
            "CLI_TIMEOUT",
            "The concrete backend fallback should take precedence"
        )
        XCTAssertEqual(
            AppDelegate.preferredEnhancementWarningCode(
                requestWarningCode: nil,
                backendWarningCode: nil,
                engine: "mlx-whisper",
                profileHadRecognitionHints: true,
                hintsApplied: false
            ),
            "HINTS_NOT_APPLIED"
        )

        let untrustedCode = "secret transcript: do not render"
        let message = AppDelegate.visibleEnhancementWarningMessage(code: untrustedCode)
        XCTAssertFalse(message.contains(untrustedCode))
        XCTAssertTrue(message.contains("safe fallback"))
        let consentMessage = AppDelegate.visibleEnhancementWarningMessage(
            code: "POSTPROCESSOR_CONSENT_REQUIRED"
        )
        XCTAssertTrue(consentMessage.contains("inactive"))
        XCTAssertTrue(consentMessage.contains("Settings"))
        XCTAssertTrue(consentMessage.contains("dictionary fallback"))
    }
}
