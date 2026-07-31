import AppKit
import XCTest
@testable import ZenWhisper

final class EnhancementRequestResolverTests: XCTestCase {
    func testResolverUsesImmutableCatalogValuesAndPreservesCLIIntentOnFallback() {
        let profile = EnhancementProfile(id: "work", name: "Work")
        let preset = EnhancementPostprocessorPreset(
            id: "local",
            displayName: "Local",
            executable: "local-tool",
            destination: .local
        )
        let catalog = EnhancementCatalogSnapshot(
            profiles: ["work": profile],
            postprocessors: ["local": preset]
        )

        let resolved = EnhancementRequestResolver.resolve(
            selection: EnhancementSelection(
                profileID: "work",
                postprocessing: .preset("local")
            ),
            catalog: catalog
        )
        XCTAssertEqual(resolved.profile, profile)
        XCTAssertEqual(resolved.postprocessor, .preset(preset))
        XCTAssertTrue(resolved.cliWasSelected)
        XCTAssertNil(resolved.warningCode)

        let unavailable = EnhancementRequestResolver.resolve(
            selection: EnhancementSelection(
                profileID: "missing-profile",
                postprocessing: .preset("missing-tool")
            ),
            catalog: catalog
        )
        XCTAssertNil(unavailable.profile)
        XCTAssertEqual(unavailable.postprocessor, .dictionary)
        XCTAssertTrue(
            unavailable.cliWasSelected,
            "A missing CLI preset must still suppress automatic Enter"
        )
        XCTAssertEqual(unavailable.warningCode, "POSTPROCESSOR_UNAVAILABLE")
    }

    func testResolverRequiresExactConsentRevisionForRemoteAndUnknownPresets() {
        for destination in [
            EnhancementDataDestination.remote,
            EnhancementDataDestination.unknown
        ] {
            let preset = EnhancementPostprocessorPreset(
                id: destination.rawValue,
                displayName: destination.rawValue,
                executable: "tool",
                destination: destination
            )
            let catalog = EnhancementCatalogSnapshot(
                postprocessors: [preset.id: preset]
            )

            for approval in [nil, "sha256:stale"] {
                let blocked = EnhancementRequestResolver.resolve(
                    selection: EnhancementSelection(
                        postprocessing: .preset(preset.id),
                        approvedPostprocessorRevision: approval
                    ),
                    catalog: catalog
                )
                XCTAssertEqual(blocked.postprocessor, .dictionary)
                XCTAssertTrue(blocked.cliWasSelected)
                XCTAssertEqual(
                    blocked.warningCode,
                    "POSTPROCESSOR_CONSENT_REQUIRED"
                )
            }

            let approved = EnhancementRequestResolver.resolve(
                selection: EnhancementSelection(
                    postprocessing: .preset(preset.id),
                    approvedPostprocessorRevision: preset.reviewRevision
                ),
                catalog: catalog
            )
            XCTAssertEqual(approved.postprocessor, .preset(preset))
            XCTAssertNil(approved.warningCode)
        }
    }

    func testSettingsStoreRoundTripsEnhancementSelection() throws {
        let suiteName = "ZenWhisper.EnhancementIntegrationTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let registry = try ModelRegistry.loadDefault()
        let store = SettingsStore(defaults: defaults, registry: registry)
        var settings = store.load()
        settings.enhancement = EnhancementSelection(
            profileID: "work",
            postprocessing: .preset("local"),
            approvedPostprocessorRevision: "sha256:reviewed"
        )

        store.save(settings)

        XCTAssertEqual(store.load().enhancement, settings.enhancement)

        settings.enhancement = EnhancementSelection(
            profileID: "work",
            postprocessing: .dictionary,
            approvedPostprocessorRevision: "sha256:must-not-persist"
        )
        store.save(settings)
        XCTAssertNil(store.load().enhancement.approvedPostprocessorRevision)
    }

    func testCombinedEnhancementValueBudgetFallsBackBeforeBackend() {
        let profile = makeCombinedBudgetProfile()
        let preset = makeCombinedBudgetPreset()
        XCTAssertTrue(
            EnhancementCatalogLimits.jsonPayloadFitsBudget(
                profile.backendPayload
            )
        )
        XCTAssertTrue(
            EnhancementCatalogLimits.jsonPayloadFitsBudget(
                preset.backendPayload
            )
        )
        XCTAssertFalse(
            EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: preset
            ),
            "Compact aliases can exceed the combined value budget without exceeding the byte budget"
        )

        let resolved = EnhancementRequestResolver.resolve(
            selection: EnhancementSelection(
                profileID: profile.id,
                postprocessing: .preset(preset.id)
            ),
            catalog: EnhancementCatalogSnapshot(
                profiles: [profile.id: profile],
                postprocessors: [preset.id: preset]
            )
        )

        XCTAssertEqual(resolved.profile, profile)
        XCTAssertEqual(resolved.postprocessor, .dictionary)
        XCTAssertTrue(resolved.cliWasSelected)
        XCTAssertEqual(
            resolved.warningCode,
            "ENHANCEMENT_CONFIGURATION_TOO_LARGE"
        )
    }
}

@MainActor
final class SettingsEnhancementUITests: XCTestCase {
    func testEnhancementControlsRenderSelectionDestinationAndUnavailableIDs() throws {
        let registry = try ModelRegistry.loadDefault()
        var settings = makeSettings()
        settings.enhancement = EnhancementSelection(
            profileID: "work",
            postprocessing: .preset("remote")
        )
        let catalog = EnhancementCatalogSnapshot(
            profiles: [
                "work": EnhancementProfile(id: "work", name: "Work")
            ],
            postprocessors: [
                "remote": EnhancementPostprocessorPreset(
                    id: "remote",
                    displayName: "Remote Tool",
                    executable: "remote-tool",
                    destination: .remote
                )
            ]
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: catalog
        )
        guard let contentView = controller.window?.contentView,
              let profile = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.profile,
                  in: contentView
              ) as? NSPopUpButton,
              let postprocessing = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.postprocessing,
                  in: contentView
              ) as? NSPopUpButton,
              let message = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.enhancementMessage,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected enhancement controls")
        }

        XCTAssertEqual(profile.titleOfSelectedItem, "Work")
        XCTAssertEqual(postprocessing.titleOfSelectedItem, "Remote Tool")
        XCTAssertTrue(message.stringValue.contains("Remote service"))
        XCTAssertTrue(message.stringValue.contains("Automatic Enter is disabled"))

        controller.synchronize(
            authoritativeSettings: settings,
            launchAtLoginStatus: .disabled,
            audioInputDevices: [],
            isBusy: false,
            enhancementCatalog: EnhancementCatalogSnapshot()
        )

        XCTAssertEqual(profile.titleOfSelectedItem, "Unavailable — work")
        XCTAssertEqual(postprocessing.titleOfSelectedItem, "Unavailable — remote")
        XCTAssertTrue(message.stringValue.contains("safe fallback"))
    }

    func testUnsupportedHintEngineDoesNotClaimDisabledDictionaryReplacementApplies() throws {
        let registry = try ModelRegistry.loadDefault()
        var settings = makeSettings()
        settings.engine = "mlx-qwen3-asr"
        settings.enhancement = EnhancementSelection(
            profileID: "work",
            postprocessing: .off
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                profiles: [
                    "work": EnhancementProfile(id: "work", name: "Work")
                ]
            )
        )
        guard let contentView = controller.window?.contentView,
              let message = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier
                      .enhancementMessage,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected enhancement status message")
        }

        XCTAssertTrue(message.stringValue.contains("does not support profile hints"))
        XCTAssertTrue(message.stringValue.contains("applies only when"))
        XCTAssertFalse(message.stringValue.contains("still applies"))
    }

    func testProfileEditorCreatesStructuredTermsThroughNativeControls() throws {
        let controller = ProfileEditorWindowController(
            profile: nil,
            expectedFingerprint: nil
        )
        var savedProfile: EnhancementProfile?
        controller.onSave = { profile, fingerprint in
            XCTAssertNil(fingerprint)
            savedProfile = profile
            return .success(
                EnhancementCatalogSnapshot(profiles: [profile.id: profile])
            )
        }
        guard let contentView = controller.window?.contentView,
              let addTerm = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.addTerm,
                  in: contentView
              ) as? NSButton,
              let canonical = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.canonical,
                  in: contentView
              ) as? NSTextField,
              let spoken = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.spoken,
                  in: contentView
              ) as? NSTextView,
              let replaceFrom = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.replaceFrom,
                  in: contentView
              ) as? NSTextView,
              let description = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.description,
                  in: contentView
              ) as? NSTextField,
              let save = findView(
                  identifier: ProfileEditorWindowController.AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected profile editor controls")
        }

        addTerm.performClick(nil)
        canonical.stringValue = "ZenWhisper"
        controller.controlTextDidChange(
            Notification(name: NSControl.textDidChangeNotification, object: canonical)
        )
        spoken.string = "Zen Whisper\nゼンウィスパー"
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: spoken)
        )
        replaceFrom.string = "Zen Whisper"
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: replaceFrom)
        )
        description.stringValue = "Application name"
        controller.controlTextDidChange(
            Notification(name: NSControl.textDidChangeNotification, object: description)
        )

        XCTAssertTrue(save.isEnabled)
        save.performClick(nil)

        let profile = try XCTUnwrap(savedProfile)
        XCTAssertTrue(profile.id.hasPrefix("profile-"))
        XCTAssertEqual(profile.name, "New Profile")
        XCTAssertEqual(
            profile.terms,
            [
                EnhancementTerm(
                    canonical: "ZenWhisper",
                    spoken: ["Zen Whisper", "ゼンウィスパー"],
                    replaceFrom: ["Zen Whisper"],
                    description: "Application name"
                )
            ]
        )
    }

    func testPostprocessorEditorRoundTripsStructuredArgumentsAndEnvironment()
        throws
    {
        let original = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote Tool",
            executable: "remote-tool",
            arguments: [
                "--model",
                "small",
                "--empty",
                "",
                "--system-prompt-file",
                "{{system_prompt_file}}"
            ],
            preflightExecutable: "remote-tool",
            preflightArguments: ["--version"],
            preflightFailureMessage: "Remote Tool is unavailable.",
            inputMode: .stdin,
            destination: .remote,
            timeoutSeconds: 30,
            systemPrompt: "Original proofreader",
            promptTemplate: "Fix {{transcript}}",
            environment: ["REMOTE_MODE": "safe"]
        )
        let controller = PostprocessorEditorWindowController(
            preset: original,
            expectedFingerprint: .missing
        )
        var savedPreset: EnhancementPostprocessorPreset?
        var wasExplicitlyReclassified: Bool?
        controller.onSave = { preset, fingerprint, explicitlyReclassified in
            XCTAssertEqual(fingerprint, .missing)
            savedPreset = preset
            wasExplicitlyReclassified = explicitlyReclassified
            return .success(
                EnhancementCatalogSnapshot(
                    postprocessors: [preset.id: preset]
                )
            )
        }
        guard let contentView = controller.window?.contentView,
              let id = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.identifier,
                  in: contentView
              ) as? NSTextField,
              let arguments = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.arguments,
                  in: contentView
              ) as? NSTextView,
              let environment = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.environment,
                  in: contentView
              ) as? NSTextView,
              let systemPrompt = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.systemPrompt,
                  in: contentView
              ) as? NSTextView,
              let inputMode = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.inputMode,
                  in: contentView
              ) as? NSPopUpButton,
              let promptHelp = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.promptHelp,
                  in: contentView
              ) as? NSTextField,
              let save = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected CLI post-processor editor controls")
        }

        XCTAssertFalse(id.isEnabled)
        XCTAssertTrue(promptHelp.stringValue.contains("through stdin"))
        XCTAssertFalse(promptHelp.stringValue.contains("visible to other"))
        inputMode.selectItem(withTitle: EnhancementPostprocessorInputMode.argument.rawValue)
        _ = inputMode.sendAction(inputMode.action, to: inputMode.target)
        XCTAssertTrue(promptHelp.stringValue.contains("{{prompt}} in argv"))
        XCTAssertTrue(promptHelp.stringValue.contains("visible to other"))
        inputMode.selectItem(withTitle: EnhancementPostprocessorInputMode.stdin.rawValue)
        _ = inputMode.sendAction(inputMode.action, to: inputMode.target)
        XCTAssertTrue(promptHelp.stringValue.contains("through stdin"))
        arguments.string = """
        [
          "--model",
          "large",
          "--empty",
          "",
          "--system-prompt-file",
          "{{system_prompt_file}}"
        ]
        """
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        environment.string = """
        {
          "REMOTE_MODE": "strict",
          "TRACE": "0"
        }
        """
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: environment)
        )
        systemPrompt.string = "Dedicated voice proofreader"
        controller.textDidChange(
            Notification(
                name: NSText.didChangeNotification,
                object: systemPrompt
            )
        )

        XCTAssertTrue(save.isEnabled)
        save.performClick(nil)

        XCTAssertEqual(
            savedPreset?.arguments,
            [
                "--model",
                "large",
                "--empty",
                "",
                "--system-prompt-file",
                "{{system_prompt_file}}"
            ]
        )
        XCTAssertEqual(
            savedPreset?.environment,
            ["REMOTE_MODE": "strict", "TRACE": "0"]
        )
        XCTAssertEqual(savedPreset?.id, original.id)
        XCTAssertEqual(savedPreset?.systemPrompt, "Dedicated voice proofreader")
        XCTAssertEqual(wasExplicitlyReclassified, true)
    }

    func testPostprocessorEditorExplainsCodexModelOptionsAndTracksArgumentEdits()
        throws
    {
        let controller = PostprocessorEditorWindowController(
            preset: EnhancementPostprocessorPreset(
                id: "codex",
                displayName: "Codex",
                executable: "codex",
                arguments: ["exec", "-"],
                destination: .remote
            ),
            expectedFingerprint: .missing
        )
        guard let contentView = controller.window?.contentView,
              let arguments = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.arguments,
                  in: contentView
              ) as? NSTextView,
              let argumentsHelp = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.argumentsHelp,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected Codex model argument guidance")
        }

        XCTAssertTrue(argumentsHelp.stringValue.contains("not pinned"))
        XCTAssertTrue(argumentsHelp.stringValue.contains("CLI default"))
        XCTAssertTrue(argumentsHelp.stringValue.contains("final “-”"))
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "model_reasoning_effort=LEVEL"
            )
        )

        let overrides = [
            (
                #"["exec", "--model", "explicit-model", "-"]"#,
                "explicit-model",
                "“--model”",
                "both items"
            ),
            (
                #"["exec", "-m", "short-model", "-"]"#,
                "short-model",
                "“-m”",
                "both items"
            ),
            (
                #"["exec", "--model=inline-model", "-"]"#,
                "inline-model",
                "“--model=…”",
                "that item"
            )
        ]
        for (json, model, option, removal) in overrides {
            arguments.string = json
            controller.textDidChange(
                Notification(
                    name: NSText.didChangeNotification,
                    object: arguments
                )
            )

            XCTAssertTrue(argumentsHelp.stringValue.contains(model))
            XCTAssertTrue(argumentsHelp.stringValue.contains(option))
            XCTAssertTrue(argumentsHelp.stringValue.contains(removal))
        }

        let effortOverrides = [
            (
                #"["exec", "-c", "model_reasoning_effort=low", "-"]"#,
                "Codex reasoning effort: “low”",
                "“-c”, “model_reasoning_effort=…”",
                "Change the following config item"
            ),
            (
                #"["exec", "--config", "model_reasoning_effort=medium", "-"]"#,
                "Codex reasoning effort: “medium”",
                "“--config”, “model_reasoning_effort=…”",
                "Change the following config item"
            ),
            (
                #"["exec", "--config=model_reasoning_effort=\"high\"", "-"]"#,
                "Codex reasoning effort: “high”",
                "“--config=model_reasoning_effort=…”",
                "Change the value after “model_reasoning_effort=”"
            ),
            (
                #"["exec", "-c=model_reasoning_effort=low", "-"]"#,
                "Codex reasoning effort: “low”",
                "“-c=model_reasoning_effort=…”",
                "Change the value after “model_reasoning_effort=”"
            )
        ]
        for (json, expectedGuidance, expectedSyntax, expectedEdit) in effortOverrides {
            arguments.string = json
            controller.textDidChange(
                Notification(
                    name: NSText.didChangeNotification,
                    object: arguments
                )
            )
            XCTAssertTrue(
                argumentsHelp.stringValue.contains(expectedGuidance)
            )
            XCTAssertTrue(argumentsHelp.stringValue.contains(expectedSyntax))
            XCTAssertTrue(argumentsHelp.stringValue.contains(expectedEdit))
        }

        arguments.string = #"["exec"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        XCTAssertTrue(argumentsHelp.stringValue.contains("argument array"))
        XCTAssertFalse(argumentsHelp.stringValue.contains("final “-”"))
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "To set Codex reasoning effort"
            )
        )
    }

    func testPostprocessorEditorExplainsClaudeModelAndEffortOptions() throws {
        let controller = PostprocessorEditorWindowController(
            preset: EnhancementPostprocessorPreset(
                id: "claude",
                displayName: "Claude Code",
                executable: "claude",
                arguments: [
                    "--print",
                    "--model",
                    "haiku",
                    "--safe-mode"
                ],
                destination: .remote
            ),
            expectedFingerprint: .missing
        )
        guard let contentView = controller.window?.contentView,
              let arguments = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.arguments,
                  in: contentView
              ) as? NSTextView,
              let argumentsHelp = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.argumentsHelp,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected Claude model and effort guidance")
        }

        XCTAssertTrue(argumentsHelp.stringValue.contains("Claude model: “haiku”"))
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "Haiku models do not support “--effort”"
            )
        )

        arguments.string =
            #"["--print", "--model=opus", "--effort=medium"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        XCTAssertTrue(argumentsHelp.stringValue.contains("Claude model: “opus”"))
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "Claude reasoning effort: “medium”"
            )
        )
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "Change the value after “--effort=”"
            )
        )

        arguments.string =
            #"["--print", "--model", "haiku", "--effort", "low"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "Claude reasoning effort “low” is configured"
            )
        )
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "current Haiku models do not support “--effort”"
            )
        )

        arguments.string =
            #"["--print", "--model", "sonnet", "--effort", "low"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "Claude reasoning effort: “low”"
            )
        )
        XCTAssertTrue(argumentsHelp.stringValue.contains("via “--effort”"))
        XCTAssertTrue(
            argumentsHelp.stringValue.contains("Change the following item")
        )

        arguments.string = #"["--print", "--model", "sonnet"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        XCTAssertTrue(
            argumentsHelp.stringValue.contains(
                "For a supported Claude model, add “--effort”"
            )
        )
        XCTAssertFalse(
            argumentsHelp.stringValue.contains(
                "Claude reasoning effort: “medium”"
            )
        )
    }

    func testPostprocessorEditorChangedLocalCommandCanSaveAsUnknown() throws {
        let original = EnhancementPostprocessorPreset(
            id: "local",
            displayName: "Local Tool",
            executable: "local-tool",
            arguments: ["--model", "old"],
            destination: .local
        )
        let controller = PostprocessorEditorWindowController(
            preset: original,
            expectedFingerprint: .missing
        )
        var reviewCount = 0
        controller.onReviewLocalDestination = { _, text in
            reviewCount += 1
            XCTAssertTrue(text.contains("outside this Mac"))
            return .saveAsUnknown
        }
        var savedPreset: EnhancementPostprocessorPreset?
        var wasExplicitlyReclassified: Bool?
        controller.onSave = { preset, _, explicitlyReclassified in
            savedPreset = preset
            wasExplicitlyReclassified = explicitlyReclassified
            return .success(
                EnhancementCatalogSnapshot(
                    postprocessors: [preset.id: preset]
                )
            )
        }
        guard let contentView = controller.window?.contentView,
              let arguments = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.arguments,
                  in: contentView
              ) as? NSTextView,
              let save = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected local CLI post-processor editor controls")
        }

        arguments.string = #"["--model", "new"]"#
        controller.textDidChange(
            Notification(name: NSText.didChangeNotification, object: arguments)
        )
        save.performClick(nil)

        XCTAssertEqual(reviewCount, 1)
        XCTAssertEqual(savedPreset?.destination, .unknown)
        XCTAssertEqual(wasExplicitlyReclassified, false)
    }

    func testNewPostprocessorRequiresConfirmationBeforeReplacingExistingID()
        throws
    {
        let controller = PostprocessorEditorWindowController(
            preset: nil,
            expectedFingerprint: .missing,
            existingPresetIDs: ["codex"]
        )
        var overwriteDecision = PostprocessorOverwriteDecision.cancel
        var reviewCount = 0
        controller.onReviewOverwrite = { preset, text in
            reviewCount += 1
            XCTAssertEqual(preset.id, "codex")
            XCTAssertTrue(text.contains("local override"))
            return overwriteDecision
        }
        var saveCount = 0
        controller.onSave = { preset, _, _ in
            saveCount += 1
            return .success(
                EnhancementCatalogSnapshot(
                    postprocessors: [preset.id: preset]
                )
            )
        }
        guard let contentView = controller.window?.contentView,
              let id = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.identifier,
                  in: contentView
              ) as? NSTextField,
              let executable = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.executable,
                  in: contentView
              ) as? NSTextField,
              let save = findView(
                  identifier: PostprocessorEditorWindowController
                      .AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected new CLI post-processor editor controls")
        }

        XCTAssertTrue(id.isEnabled)
        id.stringValue = "codex"
        controller.controlTextDidChange(
            Notification(name: NSControl.textDidChangeNotification, object: id)
        )
        executable.stringValue = "codex"
        controller.controlTextDidChange(
            Notification(
                name: NSControl.textDidChangeNotification,
                object: executable
            )
        )

        save.performClick(nil)
        XCTAssertEqual(reviewCount, 1)
        XCTAssertEqual(saveCount, 0)

        overwriteDecision = .replace
        save.performClick(nil)
        XCTAssertEqual(reviewCount, 2)
        XCTAssertEqual(saveCount, 1)
    }

    func testSettingsExposeNewAndSelectedPostprocessorEditingButtons() throws {
        let registry = try ModelRegistry.loadDefault()
        var settings = makeSettings()
        let preset = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote Tool",
            executable: "remote-tool",
            destination: .remote
        )
        settings.enhancement = EnhancementSelection(
            postprocessing: .preset(preset.id)
        )
        let catalog = EnhancementCatalogSnapshot(
            postprocessors: [preset.id: preset]
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: catalog
        )
        controller.onSavePostprocessor = { saved, _, _ in
            .success(
                EnhancementCatalogSnapshot(
                    postprocessors: [saved.id: saved]
                )
            )
        }
        guard let contentView = controller.window?.contentView,
              let newButton = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier
                      .newPostprocessor,
                  in: contentView
              ) as? NSButton,
              let editButton = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier
                      .editPostprocessor,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected CLI post-processor management buttons")
        }

        XCTAssertTrue(newButton.isEnabled)
        XCTAssertTrue(editButton.isEnabled)

        controller.synchronize(
            authoritativeSettings: settings,
            launchAtLoginStatus: .disabled,
            audioInputDevices: [],
            isBusy: true,
            enhancementCatalog: catalog
        )
        XCTAssertFalse(newButton.isEnabled)
        XCTAssertFalse(editButton.isEnabled)
    }

    func testUnknownDestinationWarningAndConsentEnumerateAllSharedData() throws {
        let registry = try ModelRegistry.loadDefault()
        var settings = makeSettings()
        let preset = EnhancementPostprocessorPreset(
            id: "unknown",
            displayName: "Unknown Tool",
            executable: "unknown-tool",
            destination: .unknown
        )
        settings.enhancement = EnhancementSelection(
            postprocessing: .preset(preset.id)
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                postprocessors: [preset.id: preset]
            )
        )
        guard let contentView = controller.window?.contentView,
              let message = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.enhancementMessage,
                  in: contentView
              ) as? NSTextField,
              let consentText = SettingsWindowController
                  .externalDestinationConsentText(for: preset) else {
            return XCTFail("Expected unknown destination warning")
        }

        for text in [message.stringValue, consentText] {
            XCTAssertTrue(text.contains("transcript"))
            XCTAssertTrue(text.contains("recognition language"))
            XCTAssertTrue(text.contains("profile name"))
            XCTAssertTrue(text.contains("profile context"))
            XCTAssertTrue(text.contains("terms"))
            XCTAssertTrue(text.contains("may leave this Mac"))
        }
    }

    func testConsentConfirmationPersistsExactRevisionAndSelectionChangeClearsIt() throws {
        let registry = try ModelRegistry.loadDefault()
        let remote = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote Tool",
            executable: "remote-tool",
            destination: .remote
        )
        let local = EnhancementPostprocessorPreset(
            id: "local",
            displayName: "Local Tool",
            executable: "local-tool",
            destination: .local
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: makeSettings(),
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                profiles: [
                    "work": EnhancementProfile(id: "work", name: "Work")
                ],
                postprocessors: [
                    remote.id: remote,
                    local.id: local
                ]
            )
        )
        var savedRequests: [SettingsSaveRequest] = []
        controller.onSave = { request in
            savedRequests.append(request)
            return .success(
                AuthoritativeSettings(
                    settings: request.settings,
                    launchAtLoginStatus: .disabled
                )
            )
        }
        var confirmationCount = 0
        controller.onConfirmExternalDestination = { _, _ in
            confirmationCount += 1
            return false
        }
        guard let contentView = controller.window?.contentView,
              let postprocessing = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.postprocessing,
                  in: contentView
              ) as? NSPopUpButton,
              let profile = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.profile,
                  in: contentView
              ) as? NSPopUpButton,
              let save = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected enhancement settings controls")
        }

        postprocessing.selectItem(withTitle: remote.displayName)
        _ = postprocessing.sendAction(
            postprocessing.action,
            to: postprocessing.target
        )
        save.performClick(nil)
        XCTAssertEqual(confirmationCount, 1)
        XCTAssertTrue(savedRequests.isEmpty)

        controller.onConfirmExternalDestination = { preset, text in
            confirmationCount += 1
            XCTAssertEqual(preset.id, remote.id)
            XCTAssertTrue(text.contains("profile context"))
            return true
        }
        save.performClick(nil)
        XCTAssertEqual(confirmationCount, 2)
        XCTAssertEqual(
            savedRequests.last?.settings.enhancement
                .approvedPostprocessorRevision,
            remote.reviewRevision
        )

        controller.onConfirmExternalDestination = { _, _ in
            confirmationCount += 1
            return false
        }
        profile.selectItem(withTitle: "Work")
        _ = profile.sendAction(profile.action, to: profile.target)
        save.performClick(nil)
        XCTAssertEqual(
            confirmationCount,
            2,
            "An exact persisted revision must not prompt again"
        )
        XCTAssertEqual(savedRequests.count, 2)

        postprocessing.selectItem(withTitle: local.displayName)
        _ = postprocessing.sendAction(
            postprocessing.action,
            to: postprocessing.target
        )
        save.performClick(nil)
        XCTAssertNil(
            savedRequests.last?.settings.enhancement
                .approvedPostprocessorRevision
        )
        XCTAssertEqual(savedRequests.count, 3)
    }

    func testStalePersistedApprovalEnablesOneClickReconfirmation() throws {
        let registry = try ModelRegistry.loadDefault()
        let remote = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote Tool",
            executable: "remote-tool",
            arguments: ["--new-revision"],
            destination: .remote
        )
        var settings = makeSettings()
        settings.enhancement = EnhancementSelection(
            postprocessing: .preset(remote.id),
            approvedPostprocessorRevision: "sha256:stale"
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                postprocessors: [remote.id: remote]
            )
        )
        var savedSettings: SettingsSnapshot?
        var savedRuntimeSettingsChanged = false
        controller.onSave = { request in
            savedSettings = request.settings
            savedRuntimeSettingsChanged = request.runtimeSettingsChanged
            return .success(
                AuthoritativeSettings(
                    settings: request.settings,
                    launchAtLoginStatus: .disabled
                )
            )
        }
        controller.onConfirmExternalDestination = { _, _ in true }
        guard let contentView = controller.window?.contentView,
              let save = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton,
              let message = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier
                      .enhancementMessage,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected Save button")
        }

        XCTAssertTrue(save.isEnabled)
        XCTAssertEqual(save.title, "Review & Save…")
        XCTAssertTrue(message.stringValue.contains("Post-processing inactive"))
        XCTAssertTrue(message.stringValue.contains("Dictionary Replacement"))
        save.performClick(nil)

        XCTAssertEqual(
            savedSettings?.enhancement.approvedPostprocessorRevision,
            remote.reviewRevision
        )
        XCTAssertTrue(savedRuntimeSettingsChanged)
        XCTAssertEqual(save.title, "Save")
    }

    func testMissingApprovalEnablesOneClickConfirmationWithoutTogglingSelection() throws {
        let registry = try ModelRegistry.loadDefault()
        let remote = EnhancementPostprocessorPreset(
            id: "remote",
            displayName: "Remote Tool",
            executable: "remote-tool",
            destination: .remote
        )
        var settings = makeSettings()
        settings.enhancement = EnhancementSelection(
            postprocessing: .preset(remote.id)
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                postprocessors: [remote.id: remote]
            )
        )
        var savedSettings: SettingsSnapshot?
        var savedRuntimeSettingsChanged = false
        controller.onSave = { request in
            savedSettings = request.settings
            savedRuntimeSettingsChanged = request.runtimeSettingsChanged
            return .success(
                AuthoritativeSettings(
                    settings: request.settings,
                    launchAtLoginStatus: .disabled
                )
            )
        }
        controller.onConfirmExternalDestination = { _, _ in true }
        guard let contentView = controller.window?.contentView,
              let save = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton,
              let message = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier
                      .enhancementMessage,
                  in: contentView
              ) as? NSTextField else {
            return XCTFail("Expected Save button")
        }

        XCTAssertTrue(save.isEnabled)
        XCTAssertEqual(save.title, "Review & Save…")
        XCTAssertTrue(message.stringValue.contains("Post-processing inactive"))
        save.performClick(nil)

        XCTAssertEqual(
            savedSettings?.enhancement.approvedPostprocessorRevision,
            remote.reviewRevision
        )
        XCTAssertTrue(savedRuntimeSettingsChanged)
        XCTAssertEqual(save.title, "Save")
    }

    func testCombinedEnhancementBudgetShowsActionableValidationAndBlocksSave() throws {
        let registry = try ModelRegistry.loadDefault()
        let profile = makeCombinedBudgetProfile()
        let preset = makeCombinedBudgetPreset()
        var settings = makeSettings()
        settings.enhancement = EnhancementSelection(
            profileID: profile.id,
            postprocessing: .preset(preset.id)
        )
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled,
            enhancementCatalog: EnhancementCatalogSnapshot(
                profiles: [profile.id: profile],
                postprocessors: [preset.id: preset]
            )
        )
        controller.onSave = { request in
            XCTFail("Oversized enhancement configuration must not be saved")
            return .success(
                AuthoritativeSettings(
                    settings: request.settings,
                    launchAtLoginStatus: .disabled
                )
            )
        }
        guard let contentView = controller.window?.contentView,
              let output = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.outputMode,
                  in: contentView
              ) as? NSPopUpButton,
              let validation = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.validationMessage,
                  in: contentView
              ) as? NSTextField,
              let save = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.save,
                  in: contentView
              ) as? NSButton else {
            return XCTFail("Expected Settings validation controls")
        }

        output.selectItem(withTitle: OutputMode.copyOnly.label)
        _ = output.sendAction(output.action, to: output.target)

        XCTAssertFalse(validation.isHidden)
        XCTAssertTrue(validation.stringValue.contains("too large"))
        XCTAssertTrue(validation.stringValue.contains("Reduce"))
        XCTAssertFalse(save.isEnabled)
    }

    private func findView(identifier: String, in root: NSView) -> NSView? {
        if root.identifier?.rawValue == identifier {
            return root
        }
        for subview in root.subviews {
            if let found = findView(identifier: identifier, in: subview) {
                return found
            }
        }
        return nil
    }

    private func makeSettings() -> SettingsSnapshot {
        SettingsSnapshot(
            hotkey: .shiftSpace,
            submitHotkey: .shiftCommandSpace,
            language: "ja",
            engine: "mlx-whisper",
            lastModelByEngine: [
                "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
                "mlx-qwen3-asr": "mlx-community/Qwen3-ASR-0.6B-8bit"
            ],
            silenceAutoStopEnabled: true,
            microphoneDeviceUID: nil,
            outputMode: .pasteRestoreClipboard
        )
    }
}

private func makeCombinedBudgetProfile() -> EnhancementProfile {
    EnhancementProfile(
        id: "value-heavy",
        name: "Value Heavy",
        terms: (0..<6).map { termIndex in
            EnhancementTerm(
                canonical: "term-\(termIndex)",
                spoken: (0..<EnhancementCatalogLimits.maximumAliasesPerTerm)
                    .map { "spoken-\(termIndex)-\($0)" },
                replaceFrom: (0..<EnhancementCatalogLimits.maximumAliasesPerTerm)
                    .map { "replace-\(termIndex)-\($0)" }
            )
        }
    )
}

private func makeCombinedBudgetPreset() -> EnhancementPostprocessorPreset {
    EnhancementPostprocessorPreset(
        id: "value-heavy",
        displayName: "Value Heavy",
        executable: "value-heavy-cli",
        arguments: (0..<EnhancementCatalogLimits.maximumPresetArgumentCount)
            .map { "--argument-\($0)" },
        preflightExecutable: "value-heavy-cli",
        preflightArguments:
            (0..<EnhancementCatalogLimits.maximumPresetArgumentCount)
            .map { "--preflight-\($0)" },
        destination: .local,
        environment: Dictionary(
            uniqueKeysWithValues:
                (0..<EnhancementCatalogLimits.maximumPresetEnvironmentEntryCount)
                .map { ("VALUE_\($0)", "\($0)") }
        )
    )
}
