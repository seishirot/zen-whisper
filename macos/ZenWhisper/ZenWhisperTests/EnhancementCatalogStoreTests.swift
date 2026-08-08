import Darwin
import Foundation
import XCTest
@testable import ZenWhisper

final class EnhancementCatalogStoreTests: XCTestCase {
    func testSelectionStorageAndBackendPayload() {
        XCTAssertEqual(PostprocessingSelection(storageValue: nil), .off)
        XCTAssertEqual(PostprocessingSelection(storageValue: ""), .off)
        XCTAssertEqual(PostprocessingSelection(storageValue: "dictionary"), .dictionary)
        XCTAssertEqual(
            PostprocessingSelection(storageValue: "claude"),
            .preset("claude")
        )
        XCTAssertFalse(PostprocessingSelection.off.isCLI)
        XCTAssertFalse(PostprocessingSelection.dictionary.isCLI)
        XCTAssertTrue(PostprocessingSelection.preset("claude").isCLI)
        XCTAssertEqual(PostprocessingSelection.preset("claude").presetID, "claude")
        XCTAssertEqual(PostprocessingSelection.dictionary.storageValue, "dictionary")

        let off = EnhancementSelection.off
        XCTAssertNil(off.profileID)
        XCTAssertEqual(off.postprocessing, .off)

        let selection = EnhancementSelection(
            profileID: "  project  ",
            postprocessing: .preset("ollama")
        )
        XCTAssertEqual(selection.profileID, "project")
        XCTAssertEqual(selection.postprocessing.storageValue, "ollama")
    }

    func testBundledPresetsAreSecureAndEquivalentToDesktopDefaults() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }

        let snapshot = EnhancementCatalogStore(paths: fixture.paths).load()
        let codex = try XCTUnwrap(snapshot.postprocessors["codex"])
        XCTAssertEqual(codex.executable, "codex")
        XCTAssertEqual(codex.destination, .remote)
        XCTAssertEqual(codex.inputMode, .stdin)
        XCTAssertEqual(codex.timeoutSeconds, 30)
        XCTAssertEqual(
            codex.arguments,
            [
                "--ask-for-approval",
                "never",
                "--disable",
                "apps",
                "--disable",
                "hooks",
                "--disable",
                "multi_agent",
                "--disable",
                "plugins",
                "--disable",
                "shell_tool",
                "--disable",
                "unified_exec",
                "exec",
                "--model",
                "gpt-5.6-luna",
                "-c",
                "model_reasoning_effort=low",
                "-c",
                "model_instructions_file={{system_prompt_file}}",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                "web_search=disabled",
                "-"
            ]
        )
        XCTAssertFalse(
            codex.arguments.joined(separator: " ").contains("{{transcript}}")
        )

        let claude = try XCTUnwrap(snapshot.postprocessors["claude"])
        XCTAssertEqual(claude.executable, "claude")
        XCTAssertEqual(claude.destination, .remote)
        XCTAssertEqual(claude.inputMode, .stdin)
        XCTAssertEqual(claude.preflightExecutable, "claude")
        XCTAssertEqual(claude.preflightArguments, ["auth", "status"])
        XCTAssertEqual(
            claude.arguments,
            [
                "--print",
                "--model",
                "haiku",
                "--safe-mode",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--system-prompt-file",
                "{{system_prompt_file}}",
                "--output-format",
                "text"
            ]
        )
        XCTAssertFalse(claude.arguments.joined(separator: " ").contains("{{transcript}}"))

        let kiro = try XCTUnwrap(snapshot.postprocessors["kiro"])
        XCTAssertEqual(kiro.executable, "kiro-cli")
        XCTAssertEqual(kiro.destination, .remote)
        XCTAssertEqual(kiro.inputMode, .stdin)
        XCTAssertEqual(kiro.adapter, .kiro)
        XCTAssertEqual(kiro.model, "gpt-5.6-luna")
        XCTAssertEqual(kiro.preflightExecutable, "")
        XCTAssertEqual(kiro.preflightArguments, [])
        XCTAssertTrue(kiro.arguments.contains("{{agent}}"))
        XCTAssertTrue(kiro.arguments.contains("--no-interactive"))
        XCTAssertTrue(kiro.arguments.contains("--effort"))

        let ollama = try XCTUnwrap(snapshot.postprocessors["ollama"])
        XCTAssertEqual(ollama.executable, "ollama")
        XCTAssertEqual(ollama.destination, .local)
        XCTAssertEqual(ollama.inputMode, .stdin)
        XCTAssertEqual(ollama.preflightArguments, ["show", "qwen3.5:4b"])
        XCTAssertEqual(
            ollama.arguments,
            [
                "run",
                "qwen3.5:4b",
                "--think=false",
                "--hidethinking",
                "--nowordwrap"
            ]
        )
        XCTAssertEqual(ollama.environment["OLLAMA_HOST"], "127.0.0.1:11434")
        XCTAssertEqual(ollama.environment["OLLAMA_NOHISTORY"], "1")
        XCTAssertFalse(ollama.arguments.contains("pull"))

        XCTAssertEqual(codex.systemPrompt, claude.systemPrompt)
        XCTAssertEqual(codex.systemPrompt, kiro.systemPrompt)
        XCTAssertFalse(codex.systemPrompt.isEmpty)
        for preset in [codex, claude, kiro] {
            XCTAssertTrue(preset.systemPrompt.contains("音声認識結果の校正器"))
            XCTAssertTrue(preset.systemPrompt.contains("未信頼の参照データ"))
            XCTAssertTrue(
                preset.systemPrompt.contains("ツールやコマンドを使用しないでください")
            )
            XCTAssertTrue(
                preset.systemPrompt.contains("Markdown、コードフェンス、説明を付けず")
            )
            XCTAssertFalse(preset.promptTemplate.contains("音声認識結果の校正器"))
        }
        XCTAssertEqual(codex.promptTemplate, claude.promptTemplate)
        XCTAssertEqual(codex.promptTemplate, kiro.promptTemplate)
        XCTAssertTrue(ollama.systemPrompt.isEmpty)
        XCTAssertTrue(ollama.promptTemplate.contains("音声認識結果の校正器"))
        for preset in [codex, claude, kiro, ollama] {
            XCTAssertTrue(
                preset.promptTemplate.contains(
                    "<transcript_{{boundary}}>\n{{transcript}}\n"
                        + "</transcript_{{boundary}}>"
                )
            )
        }
    }

    func testProfileRoundTripPreservesUnknownFieldsAndUsesPrivatePermissions() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        let profileURL = fixture.paths.profilesDirectory
            .appendingPathComponent("project.json")
        try """
        {
          "version": 1,
          "profile_id": "project",
          "name": "Project",
          "context": "context",
          "future": {"format": 2},
          "terms": [{
            "canonical": "ZenWhisper",
            "spoken": ["Zen Whisper"],
            "replace_from": ["Zen Whisper"],
            "description": "app",
            "future_term": true
          }]
        }
        """.write(to: profileURL, atomically: true, encoding: .utf8)

        let loaded = store.load()
        var profile = try XCTUnwrap(loaded.profiles["project"])
        XCTAssertEqual(profile.extraFields["future"], .object(["format": .number(2)]))
        XCTAssertEqual(profile.terms[0].extraFields["future_term"], .bool(true))
        profile.name = "Renamed"

        let fingerprint = try XCTUnwrap(loaded.fingerprints.profiles["project"])
        _ = try store.saveProfile(profile, expectedFingerprint: fingerprint)

        let reloaded = store.load()
        XCTAssertEqual(reloaded.profiles["project"]?.name, "Renamed")
        XCTAssertEqual(
            reloaded.profiles["project"]?.extraFields["future"],
            .object(["format": .number(2)])
        )
        XCTAssertEqual(
            reloaded.profiles["project"]?.terms[0].extraFields["future_term"],
            .bool(true)
        )
        XCTAssertEqual(fileMode(profileURL), 0o600)
        XCTAssertEqual(fileMode(fixture.paths.profilesDirectory), 0o700)
    }

    func testInvalidProfileIsSkippedWithoutLeakingValuesAndValidProfilesRemain() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let validURL = fixture.paths.profilesDirectory.appendingPathComponent("valid.json")
        let invalidURL = fixture.paths.profilesDirectory.appendingPathComponent("invalid.json")
        try profileJSON(id: "valid", name: "Valid")
            .write(to: validURL, atomically: true, encoding: .utf8)
        try """
        {
          "version": 1,
          "profile_id": "invalid",
          "name": "Contains super-secret-token",
          "terms": [{"canonical": 42}]
        }
        """.write(to: invalidURL, atomically: true, encoding: .utf8)

        let snapshot = fixture.store().load()
        XCTAssertNotNil(snapshot.profiles["valid"])
        XCTAssertNil(snapshot.profiles["invalid"])
        let issue = try XCTUnwrap(
            snapshot.issues.first {
                $0.source == .profileFile("invalid.json")
            }
        )
        XCTAssertEqual(issue.reason, .invalidField)
        XCTAssertEqual(issue.field, "terms[0].canonical")
        XCTAssertFalse(String(describing: issue).contains("super-secret-token"))
    }

    func testProfileSaveRejectsDuplicateReplacementMalformedOverwriteAndConflict() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        let conflicting = EnhancementProfile(
            id: "project",
            name: "Project",
            terms: [
                EnhancementTerm(canonical: "One", replaceFrom: ["same"]),
                EnhancementTerm(canonical: "Two", replaceFrom: ["same"])
            ]
        )
        XCTAssertThrowsError(try store.saveProfile(conflicting)) { error in
            guard case EnhancementCatalogError.invalidProfile(let field) = error else {
                return XCTFail("Unexpected error: \(error)")
            }
            XCTAssertEqual(field, "terms[1].replace_from[0]")
        }

        let profileURL = fixture.paths.profilesDirectory
            .appendingPathComponent("project.json")
        let malformed = Data(#"{"version":1,"name":"secret","terms":["#.utf8)
        try malformed.write(to: profileURL)
        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(id: "project", name: "Replacement")
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .malformedExistingFile("project.json")
            )
        }
        XCTAssertEqual(try Data(contentsOf: profileURL), malformed)

        try profileJSON(id: "fresh", name: "Fresh")
            .write(
                to: fixture.paths.profilesDirectory
                    .appendingPathComponent("fresh.json"),
                atomically: true,
                encoding: .utf8
            )
        let initial = store.load()
        let staleFingerprint = try XCTUnwrap(initial.fingerprints.profiles["fresh"])
        try profileJSON(id: "fresh", name: "External")
            .write(
                to: fixture.paths.profilesDirectory
                    .appendingPathComponent("fresh.json"),
                atomically: true,
                encoding: .utf8
            )
        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(id: "fresh", name: "Editor"),
                expectedFingerprint: staleFingerprint
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .fingerprintConflict("fresh.json")
            )
        }
    }

    func testLocalOverridesMergeAndChangedExecutionResetsDestination() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try """
        {
          "version": 1,
          "top_level_future": true,
          "postprocessors": {
            "remote": {
              "executable": "replacement-cli",
              "future_entry": {"keep": true}
            },
            "disabled": {
              "enabled": false
            }
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )

        let snapshot = fixture.store().load()
        let remote = try XCTUnwrap(snapshot.postprocessors["remote"])
        XCTAssertEqual(remote.executable, "replacement-cli")
        XCTAssertEqual(remote.arguments, [])
        XCTAssertEqual(remote.preflightExecutable, "")
        XCTAssertEqual(remote.environment, [:])
        XCTAssertEqual(remote.destination, .unknown)
        XCTAssertEqual(
            remote.extraFields["future_entry"],
            .object(["keep": .bool(true)])
        )
        XCTAssertNil(snapshot.postprocessors["disabled"])
        XCTAssertEqual(snapshot.localPostprocessorIDs, ["remote", "disabled"])
    }

    func testMalformedLocalCatalogBlocksAllBundledPostprocessors() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try Data(#"{"version":1,"postprocessors":{"remote":"#.utf8)
            .write(to: fixture.paths.postprocessorsFile)

        let snapshot = fixture.store().load()

        XCTAssertTrue(snapshot.blocksAllPostprocessors)
        XCTAssertTrue(snapshot.postprocessors.isEmpty)
        XCTAssertTrue(
            snapshot.issues.contains {
                $0.source == .localPostprocessors
                    && $0.reason == .malformedJSON
            }
        )
    }

    func testUnreadableLocalCatalogPathBlocksAllBundledPostprocessors() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try FileManager.default.createDirectory(
            at: fixture.paths.postprocessorsFile,
            withIntermediateDirectories: false
        )

        let snapshot = fixture.store().load()

        XCTAssertTrue(snapshot.blocksAllPostprocessors)
        XCTAssertTrue(snapshot.postprocessors.isEmpty)
        XCTAssertTrue(
            snapshot.issues.contains {
                $0.source == .localPostprocessors
                    && $0.reason == .unreadable
            }
        )
    }

    func testDanglingLocalCatalogSymlinkBlocksAllBundledPostprocessors() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try FileManager.default.createSymbolicLink(
            at: fixture.paths.postprocessorsFile,
            withDestinationURL: fixture.root.appendingPathComponent("missing.json")
        )

        let snapshot = fixture.store().load()

        XCTAssertTrue(snapshot.blocksAllPostprocessors)
        XCTAssertTrue(snapshot.postprocessors.isEmpty)
    }

    func testInvalidLocalEntryShadowsBundledPresetWithoutBlockingOthers() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try """
        {
          "version": 1,
          "postprocessors": {
            "remote": "invalid local override"
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )

        let snapshot = fixture.store().load()

        XCTAssertFalse(snapshot.blocksAllPostprocessors)
        XCTAssertEqual(snapshot.blockedPostprocessorIDs, ["remote"])
        XCTAssertTrue(snapshot.localPostprocessorIDs.contains("remote"))
        XCTAssertNil(snapshot.postprocessors["remote"])
        XCTAssertNotNil(snapshot.postprocessors["disabled"])
    }

    func testCustomPresetMissingDestinationDefaultsUnknownAndInvalidTypeIsBlocked() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try """
        {
          "version": 1,
          "postprocessors": {
            "custom": {
              "display_name": "Custom",
              "executable": "custom-cli",
              "arguments": [],
              "input_mode": "stdin",
              "timeout_sec": 30,
              "prompt_template": "{{transcript}}",
              "environment": {}
            },
            "invalid": {
              "display_name": "Invalid",
              "executable": "invalid-cli",
              "arguments": [],
              "input_mode": "stdin",
              "data_destination": 7,
              "timeout_sec": 30,
              "prompt_template": "{{transcript}}",
              "environment": {}
            }
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )

        let snapshot = fixture.store().load()

        XCTAssertEqual(snapshot.postprocessors["custom"]?.destination, .unknown)
        XCTAssertNil(snapshot.postprocessors["invalid"])
        XCTAssertTrue(snapshot.blockedPostprocessorIDs.contains("invalid"))
    }

    func testGenericPresetNormalizesWhitespaceOnlyModel() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try """
        {
          "version": 1,
          "postprocessors": {
            "custom": {
              "display_name": "Custom",
              "executable": "custom-cli",
              "arguments": [],
              "input_mode": "stdin",
              "model": "   ",
              "timeout_sec": 30,
              "prompt_template": "{{transcript}}",
              "environment": {}
            }
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )

        let snapshot = fixture.store().load()

        XCTAssertEqual(snapshot.postprocessors["custom"]?.model, "")
    }

    func testEveryCommandTransportEditResetsInheritedLocalDestination() throws {
        let overrideFragments = [
            #""executable": "different-cli""#,
            #""arguments": ["--different"]"#,
            #""environment": {"TOKEN": "different"}"#,
            #""preflight_executable": "different-cli""#,
            #""preflight_arguments": ["--different"]"#,
            #""input_mode": "argument", "arguments": ["{{prompt}}"]"#,
            #""adapter": "kiro", "model": "gpt-5.6-luna", "executable": "kiro-cli", "arguments": ["chat", "--agent", "{{agent}}"], "system_prompt": "Dedicated""#
        ]

        for fragment in overrideFragments {
            let fixture = try Fixture()
            do {
                try """
                {
                  "version": 1,
                  "postprocessors": {
                    "disabled": {
                      "data_destination": "local",
                      \(fragment)
                    }
                  }
                }
                """.write(
                    to: fixture.paths.postprocessorsFile,
                    atomically: true,
                    encoding: .utf8
                )

                let snapshot = fixture.store().load()
                XCTAssertEqual(
                    snapshot.postprocessors["disabled"]?.destination,
                    .unknown,
                    "Expected trust reset for override: \(fragment)"
                )
            }
            fixture.cleanup()
        }
    }

    func testRemoteBundledCommandCannotBeRelabeledLocalWithoutReview() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        try """
        {
          "version": 1,
          "postprocessors": {
            "remote": {
              "data_destination": "local"
            },
            "disabled": {
              "display_name": "Still trusted local"
            }
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )

        let snapshot = fixture.store().load()

        XCTAssertEqual(snapshot.postprocessors["remote"]?.destination, .unknown)
        XCTAssertEqual(snapshot.postprocessors["disabled"]?.destination, .local)
    }

    func testExplicitLocalReclassificationIsBoundToExactCommandRevision() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        var snapshot = store.load()
        var preset = try XCTUnwrap(snapshot.postprocessors["remote"])
        preset.executable = "verified-local-cli"
        preset.arguments = []
        preset.preflightExecutable = ""
        preset.preflightArguments = []
        preset.environment = [:]
        preset.destination = .local

        let classified = try store.savePostprocessor(
            preset,
            expectedFingerprint: snapshot.fingerprints.postprocessors,
            destinationWasExplicitlyReclassified: true
        )
        XCTAssertEqual(classified.preset.destination, .local)
        XCTAssertEqual(
            classified.preset.destinationReviewRevision,
            classified.preset.commandRevision
        )

        snapshot = store.load()
        var edited = try XCTUnwrap(snapshot.postprocessors["remote"])
        XCTAssertEqual(edited.destination, .local)
        edited.environment["REMOTE_HOST"] = "example.invalid"
        let reset = try store.savePostprocessor(
            edited,
            expectedFingerprint: snapshot.fingerprints.postprocessors
        )

        XCTAssertEqual(reset.preset.destination, .unknown)
        XCTAssertNil(reset.preset.destinationReviewRevision)
        XCTAssertEqual(store.load().postprocessors["remote"]?.destination, .unknown)
    }

    func testPostprocessorSavePreservesUnknownFieldsAndRequiresExplicitReclassification() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        try """
        {
          "version": 1,
          "top_level_future": "keep",
          "postprocessors": {
            "remote": {
              "display_name": "Local override",
              "future_entry": 7
            }
          }
        }
        """.write(
            to: fixture.paths.postprocessorsFile,
            atomically: true,
            encoding: .utf8
        )
        var snapshot = store.load()
        var preset = try XCTUnwrap(snapshot.postprocessors["remote"])
        preset.executable = "other"
        preset.destination = .remote

        let forcedUnknown = try store.savePostprocessor(
            preset,
            expectedFingerprint: snapshot.fingerprints.postprocessors
        )
        XCTAssertEqual(forcedUnknown.preset.destination, .unknown)
        snapshot = store.load()
        XCTAssertEqual(snapshot.postprocessors["remote"]?.destination, .unknown)
        XCTAssertEqual(
            snapshot.postprocessors["remote"]?.extraFields["future_entry"],
            .number(7)
        )

        let rawAfterFirstSave = try JSONDecoder().decode(
            JSONValue.self,
            from: Data(contentsOf: fixture.paths.postprocessorsFile)
        )
        XCTAssertEqual(
            rawAfterFirstSave.objectValue?["top_level_future"],
            .string("keep")
        )
        XCTAssertEqual(fileMode(fixture.paths.postprocessorsFile), 0o600)

        var explicitlyRemote = try XCTUnwrap(snapshot.postprocessors["remote"])
        explicitlyRemote.executable = "third"
        explicitlyRemote.destination = .remote
        let explicit = try store.savePostprocessor(
            explicitlyRemote,
            expectedFingerprint: snapshot.fingerprints.postprocessors,
            destinationWasExplicitlyReclassified: true
        )
        XCTAssertEqual(explicit.preset.destination, .remote)
        XCTAssertEqual(store.load().postprocessors["remote"]?.destination, .remote)
    }

    func testPostprocessorSaveRejectsUnsafeStdinMalformedOverwriteAndConflict() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        let unsafe = EnhancementPostprocessorPreset(
            id: "unsafe",
            displayName: "Unsafe",
            executable: "tool",
            arguments: ["--text", "{{transcript}}"],
            inputMode: .stdin,
            destination: .unknown
        )
        XCTAssertThrowsError(try store.savePostprocessor(unsafe)) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidPostprocessor(field: "arguments")
            )
            XCTAssertFalse(String(describing: error).contains("{{transcript}}"))
        }
        let unpairedSystemPrompt = EnhancementPostprocessorPreset(
            id: "unpaired-system-prompt",
            displayName: "Unpaired system prompt",
            executable: "tool",
            inputMode: .stdin,
            destination: .unknown,
            systemPrompt: "Dedicated"
        )
        XCTAssertThrowsError(
            try store.savePostprocessor(unpairedSystemPrompt)
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidPostprocessor(field: "system_prompt")
            )
        }
        let placeholderInSystemPrompt = EnhancementPostprocessorPreset(
            id: "dynamic-system-prompt",
            displayName: "Dynamic system prompt",
            executable: "tool",
            arguments: ["{{system_prompt_file}}"],
            inputMode: .stdin,
            destination: .unknown,
            systemPrompt: "Dedicated {{language}}"
        )
        XCTAssertThrowsError(
            try store.savePostprocessor(placeholderInSystemPrompt)
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidPostprocessor(field: "system_prompt")
            )
        }

        let malformed = Data(#"{"version":1,"postprocessors":{"x":"secret-value"}}"#.utf8)
        try malformed.write(to: fixture.paths.postprocessorsFile)
        XCTAssertThrowsError(
            try store.savePostprocessor(validPreset(id: "new"))
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .malformedExistingFile("postprocessors.json")
            )
            XCTAssertFalse(String(describing: error).contains("secret-value"))
        }
        XCTAssertEqual(try Data(contentsOf: fixture.paths.postprocessorsFile), malformed)

        try #"{"version":1,"postprocessors":{}}"#
            .write(
                to: fixture.paths.postprocessorsFile,
                atomically: true,
                encoding: .utf8
            )
        let stale = store.load().fingerprints.postprocessors
        try #"{"version":1,"postprocessors":{},"external":true}"#
            .write(
                to: fixture.paths.postprocessorsFile,
                atomically: true,
                encoding: .utf8
            )
        XCTAssertThrowsError(
            try store.savePostprocessor(
                validPreset(id: "new"),
                expectedFingerprint: stale
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .fingerprintConflict("postprocessors.json")
            )
        }
    }

    func testArgumentModePayloadUsesStructuredExecutableAndArguments() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let preset = EnhancementPostprocessorPreset(
            id: "argument",
            displayName: "Argument",
            executable: "/usr/bin/tool",
            arguments: [
                "--prompt",
                "{{prompt}}",
                "--system-prompt-file",
                "{{system_prompt_file}}"
            ],
            preflightExecutable: "/usr/bin/tool",
            preflightArguments: ["--version"],
            inputMode: .argument,
            destination: .unknown,
            timeoutSeconds: 12,
            systemPrompt: "Dedicated proofreader",
            promptTemplate: "Fix {{transcript}}"
        )
        let result = try fixture.store().savePostprocessor(
            preset,
            expectedFingerprint: .missing
        )
        XCTAssertEqual(result.preset.destination, .unknown)
        XCTAssertEqual(result.preset.backendPayload["executable"] as? String, "/usr/bin/tool")
        XCTAssertEqual(
            result.preset.backendPayload["arguments"] as? [String],
            [
                "--prompt",
                "{{prompt}}",
                "--system-prompt-file",
                "{{system_prompt_file}}"
            ]
        )
        XCTAssertEqual(
            result.preset.backendPayload["system_prompt"] as? String,
            "Dedicated proofreader"
        )
        XCTAssertEqual(result.preset.backendPayload["id"] as? String, "argument")
    }

    func testCatalogLimitsRejectOversizedProfilesAndPresets() throws {
        let fixture = try Fixture()
        defer { fixture.cleanup() }
        let store = fixture.store()
        let tooManyTerms = (0...EnhancementCatalogLimits.maximumProfileTermCount)
            .map { EnhancementTerm(canonical: "term-\($0)") }
        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(
                    id: "too-many",
                    name: "Too Many",
                    terms: tooManyTerms
                )
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidProfile(field: "terms")
            )
        }

        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(
                    id: "context",
                    name: "Context",
                    context: String(
                        repeating: "x",
                        count: EnhancementCatalogLimits.maximumContextUTF8Bytes + 1
                    )
                )
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidProfile(field: "context")
            )
        }

        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(
                    id: "string",
                    name: "String",
                    terms: [
                        EnhancementTerm(
                            canonical: String(
                                repeating: "x",
                                count: EnhancementCatalogLimits.maximumStringUTF8Bytes + 1
                            )
                        )
                    ]
                )
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidProfile(field: "terms[0].canonical")
            )
        }

        let payloadAliases = (0..<EnhancementCatalogLimits.maximumAliasesPerTerm)
            .map { _ in String(repeating: "x", count: 3_000) }
        XCTAssertThrowsError(
            try store.saveProfile(
                EnhancementProfile(
                    id: "payload",
                    name: "Payload",
                    terms: [
                        EnhancementTerm(
                            canonical: "payload",
                            spoken: payloadAliases
                        )
                    ]
                )
            )
        ) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidProfile(field: "payload")
            )
        }

        var slow = validPreset(id: "slow")
        slow.timeoutSeconds =
            EnhancementCatalogLimits.maximumPresetTimeoutSeconds + 1
        XCTAssertThrowsError(try store.savePostprocessor(slow)) { error in
            XCTAssertEqual(
                error as? EnhancementCatalogError,
                .invalidPostprocessor(field: "timeout_sec")
            )
        }
    }
}

private final class Fixture {
    let root: URL
    let runtimeRoot: URL
    let paths: AppPaths
    let bundledURL: URL

    init() throws {
        root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent(
                "zen-whisper-enhancement-tests-\(UUID().uuidString)",
                isDirectory: true
            )
        runtimeRoot = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent(
                "zwe-\(UUID().uuidString.prefix(8))",
                isDirectory: true
            )
        paths = AppPaths(
            appSupport: root.appendingPathComponent("support", isDirectory: true),
            logs: root.appendingPathComponent("logs", isDirectory: true),
            runtimeDirectoryOverride: runtimeRoot
        )
        bundledURL = root.appendingPathComponent("bundled.json")
        try paths.prepare()
        try Self.bundledJSON.write(
            to: bundledURL,
            atomically: true,
            encoding: .utf8
        )
    }

    func store() -> EnhancementCatalogStore {
        EnhancementCatalogStore(
            paths: paths,
            bundledPostprocessorsURL: bundledURL
        )
    }

    func cleanup() {
        try? FileManager.default.removeItem(at: root)
        try? FileManager.default.removeItem(at: runtimeRoot)
    }

    private static let bundledJSON = """
    {
      "version": 1,
      "postprocessors": {
        "remote": {
          "display_name": "Remote",
          "executable": "remote-cli",
          "arguments": ["--provider-specific"],
          "preflight_executable": "remote-cli",
          "preflight_arguments": ["--version"],
          "preflight_failure_message": "missing",
          "input_mode": "stdin",
          "data_destination": "remote",
          "timeout_sec": 30,
          "prompt_template": "{{transcript}}",
          "environment": {"TOKEN": "secret"}
        },
        "disabled": {
          "display_name": "Disabled baseline",
          "executable": "disabled-cli",
          "arguments": [],
          "preflight_executable": "disabled-cli",
          "preflight_arguments": ["--version"],
          "preflight_failure_message": "missing",
          "input_mode": "stdin",
          "data_destination": "local",
          "timeout_sec": 30,
          "prompt_template": "{{transcript}}",
          "environment": {}
        }
      }
    }
    """
}

private func profileJSON(id: String, name: String) -> String {
    """
    {
      "version": 1,
      "profile_id": "\(id)",
      "name": "\(name)",
      "context": "",
      "terms": []
    }
    """
}

private func validPreset(id: String) -> EnhancementPostprocessorPreset {
    EnhancementPostprocessorPreset(
        id: id,
        displayName: "Valid",
        executable: "tool",
        inputMode: .stdin,
        destination: .unknown
    )
}

private func fileMode(_ url: URL) -> mode_t {
    var metadata = stat()
    guard lstat(url.path, &metadata) == 0 else {
        return 0
    }
    return metadata.st_mode & 0o777
}
