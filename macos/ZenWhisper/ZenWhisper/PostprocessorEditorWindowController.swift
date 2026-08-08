import AppKit
import Foundation

enum PostprocessorLocalDestinationDecision {
    case saveAsUnknown
    case confirmLocal
    case cancel
}

enum PostprocessorOverwriteDecision {
    case replace
    case cancel
}

@MainActor
final class PostprocessorEditorWindowController: NSWindowController,
    NSWindowDelegate,
    NSTextFieldDelegate,
    NSTextViewDelegate
{
    typealias SaveHandler = (
        EnhancementPostprocessorPreset,
        EnhancementFileFingerprint?,
        Bool
    ) -> Result<EnhancementCatalogSnapshot, Error>
    typealias LocalDestinationReviewHandler = (
        EnhancementPostprocessorPreset,
        String
    ) -> PostprocessorLocalDestinationDecision
    typealias OverwriteReviewHandler = (
        EnhancementPostprocessorPreset,
        String
    ) -> PostprocessorOverwriteDecision

    var onSave: SaveHandler? {
        didSet { updateDocumentEditedState() }
    }
    var onSaved: ((EnhancementCatalogSnapshot, String) -> Void)?
    var onDismiss: (() -> Void)?
    var onReviewLocalDestination: LocalDestinationReviewHandler?
    var onReviewOverwrite: OverwriteReviewHandler?

    enum AccessibilityIdentifier {
        static let identifier = "postprocessorEditor.id"
        static let displayName = "postprocessorEditor.displayName"
        static let executable = "postprocessorEditor.executable"
        static let arguments = "postprocessorEditor.arguments"
        static let argumentsHelp = "postprocessorEditor.argumentsHelp"
        static let inputMode = "postprocessorEditor.inputMode"
        static let adapter = "postprocessorEditor.adapter"
        static let model = "postprocessorEditor.model"
        static let destination = "postprocessorEditor.destination"
        static let timeout = "postprocessorEditor.timeout"
        static let preflightExecutable = "postprocessorEditor.preflightExecutable"
        static let preflightArguments = "postprocessorEditor.preflightArguments"
        static let preflightFailureMessage =
            "postprocessorEditor.preflightFailureMessage"
        static let environment = "postprocessorEditor.environment"
        static let systemPrompt = "postprocessorEditor.systemPrompt"
        static let promptTemplate = "postprocessorEditor.promptTemplate"
        static let promptHelp = "postprocessorEditor.promptHelp"
        static let securityHelp = "postprocessorEditor.securityHelp"
        static let message = "postprocessorEditor.message"
        static let cancel = "postprocessorEditor.cancel"
        static let save = "postprocessorEditor.save"
    }

    private enum Layout {
        static let contentSize = NSSize(width: 820, height: 760)
        static let minimumContentSize = NSSize(width: 700, height: 620)
        static let inset: CGFloat = 20
        static let labelWidth: CGFloat = 150
    }

    private struct DraftState: Equatable {
        let id: String
        let displayName: String
        let executable: String
        let argumentsJSON: String
        let inputMode: String
        let adapter: String
        let model: String
        let destination: String
        let timeout: String
        let preflightExecutable: String
        let preflightArgumentsJSON: String
        let preflightFailureMessage: String
        let environmentJSON: String
        let systemPrompt: String
        let promptTemplate: String
    }

    private struct DraftValidationError: LocalizedError {
        let message: String

        var errorDescription: String? {
            message
        }
    }

    private struct CommandOptionMatch {
        enum Form {
            case separate
            case inline
        }

        let name: String
        let value: String
        let form: Form
    }

    private let expectedFingerprint: EnhancementFileFingerprint?
    private let existingPresetIDs: Set<String>
    private let isExistingPreset: Bool
    private let originalPreset: EnhancementPostprocessorPreset
    private var baselineState: DraftState!
    private var didDismiss = false
    private var isRendering = false
    private var isSaving = false

    private let idField = NSTextField()
    private let displayNameField = NSTextField()
    private let executableField = NSTextField()
    private let argumentsTextView = NSTextView()
    private let argumentsHelpLabel = NSTextField(wrappingLabelWithString: "")
    private let inputModePopup = NSPopUpButton()
    private let adapterPopup = NSPopUpButton()
    private let modelField = NSTextField()
    private let destinationPopup = NSPopUpButton()
    private let timeoutField = NSTextField()
    private let preflightExecutableField = NSTextField()
    private let preflightArgumentsTextView = NSTextView()
    private let preflightFailureMessageField = NSTextField()
    private let environmentTextView = NSTextView()
    private let systemPromptTextView = NSTextView()
    private let promptTemplateTextView = NSTextView()
    private let promptHelpLabel = NSTextField(wrappingLabelWithString: "")
    private let messageLabel = NSTextField(wrappingLabelWithString: "")
    private let cancelButton = NSButton()
    private let saveButton = NSButton()

    init(
        preset: EnhancementPostprocessorPreset?,
        expectedFingerprint: EnhancementFileFingerprint?,
        existingPresetIDs: Set<String> = []
    ) {
        let initialPreset = preset ?? EnhancementPostprocessorPreset(
            id: "custom-\(UUID().uuidString.lowercased())",
            displayName: "Custom Cleanup",
            executable: "",
            destination: .unknown
        )
        originalPreset = initialPreset
        self.expectedFingerprint = expectedFingerprint
        self.existingPresetIDs = existingPresetIDs
        isExistingPreset = preset != nil

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: Layout.contentSize),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = preset == nil
            ? "New CLI Post-Processor"
            : "Edit CLI Post-Processor"
        window.isReleasedWhenClosed = false
        window.contentMinSize = Layout.minimumContentSize

        super.init(window: window)
        window.delegate = self
        configureControls()
        configureContentView()
        render(initialPreset)
    }

    required init?(coder: NSCoder) {
        nil
    }

    var isDirty: Bool {
        baselineState != draftState
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard !isSaving, isDirty else {
            return !isSaving
        }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Discard CLI post-processor changes?"
        alert.informativeText =
            "Your unsaved command, arguments, and prompt changes will be lost."
        alert.addButton(withTitle: "Discard")
        alert.addButton(withTitle: "Keep Editing")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func windowWillClose(_ notification: Notification) {
        dismissOnce()
    }

    func controlTextDidChange(_ notification: Notification) {
        guard !isRendering else {
            return
        }
        refreshArgumentsHelp()
        clearMessage()
        updateDocumentEditedState()
    }

    func textDidChange(_ notification: Notification) {
        guard !isRendering else {
            return
        }
        refreshArgumentsHelp()
        clearMessage()
        updateDocumentEditedState()
    }

    private func configureControls() {
        configureTextField(
            idField,
            placeholder: "custom-cleanup",
            identifier: AccessibilityIdentifier.identifier,
            label: "Post-processor ID"
        )
        configureTextField(
            displayNameField,
            placeholder: "Custom Cleanup",
            identifier: AccessibilityIdentifier.displayName,
            label: "Post-processor display name"
        )
        configureTextField(
            executableField,
            placeholder: "Executable name or absolute path",
            identifier: AccessibilityIdentifier.executable,
            label: "Post-processor executable"
        )
        configureTextField(
            timeoutField,
            placeholder: "30",
            identifier: AccessibilityIdentifier.timeout,
            label: "Post-processor timeout in seconds"
        )
        configureTextField(
            modelField,
            placeholder: "Kiro model ID (for example gpt-5.6-luna)",
            identifier: AccessibilityIdentifier.model,
            label: "Adapter-specific model"
        )
        configureTextField(
            preflightExecutableField,
            placeholder: "Optional executable",
            identifier: AccessibilityIdentifier.preflightExecutable,
            label: "Preflight executable"
        )
        configureTextField(
            preflightFailureMessageField,
            placeholder: "Shown when the preflight command fails",
            identifier: AccessibilityIdentifier.preflightFailureMessage,
            label: "Preflight failure message"
        )

        configureTextView(
            argumentsTextView,
            identifier: AccessibilityIdentifier.arguments,
            label: "Command arguments as a JSON string array"
        )
        configureTextView(
            preflightArgumentsTextView,
            identifier: AccessibilityIdentifier.preflightArguments,
            label: "Preflight arguments as a JSON string array"
        )
        configureTextView(
            environmentTextView,
            identifier: AccessibilityIdentifier.environment,
            label: "Environment as a JSON string object"
        )
        configureTextView(
            systemPromptTextView,
            identifier: AccessibilityIdentifier.systemPrompt,
            label: "Replacement system prompt",
            monospaced: false
        )
        configureTextView(
            promptTemplateTextView,
            identifier: AccessibilityIdentifier.promptTemplate,
            label: "Post-processor prompt template",
            monospaced: false
        )
        argumentsHelpLabel.font = .systemFont(
            ofSize: NSFont.smallSystemFontSize
        )
        argumentsHelpLabel.textColor = .secondaryLabelColor
        argumentsHelpLabel.identifier = NSUserInterfaceItemIdentifier(
            AccessibilityIdentifier.argumentsHelp
        )
        argumentsHelpLabel.setAccessibilityLabel(
            "CLI model argument guidance"
        )
        promptHelpLabel.font = .systemFont(
            ofSize: NSFont.smallSystemFontSize
        )
        promptHelpLabel.textColor = .secondaryLabelColor
        promptHelpLabel.identifier = NSUserInterfaceItemIdentifier(
            AccessibilityIdentifier.promptHelp
        )
        promptHelpLabel.setAccessibilityLabel(
            "CLI prompt transport and placeholder guidance"
        )

        configurePopup(
            inputModePopup,
            identifier: AccessibilityIdentifier.inputMode,
            label: "Post-processor input mode",
            values: EnhancementPostprocessorInputMode.allCases.map(\.rawValue),
            action: #selector(popupChanged)
        )
        configurePopup(
            adapterPopup,
            identifier: AccessibilityIdentifier.adapter,
            label: "Post-processor adapter",
            values: EnhancementPostprocessorAdapter.allCases.map(\.rawValue),
            action: #selector(popupChanged)
        )
        refreshPromptHelp()
        configurePopup(
            destinationPopup,
            identifier: AccessibilityIdentifier.destination,
            label: "Post-processor data destination",
            values: [
                EnhancementDataDestination.unknown.rawValue,
                EnhancementDataDestination.local.rawValue,
                EnhancementDataDestination.remote.rawValue
            ],
            action: #selector(popupChanged)
        )

        messageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        messageLabel.textColor = .systemRed
        messageLabel.identifier =
            NSUserInterfaceItemIdentifier(AccessibilityIdentifier.message)
        messageLabel.setAccessibilityLabel("CLI post-processor validation message")
        messageLabel.isHidden = true

        configureButton(
            cancelButton,
            title: "Cancel",
            identifier: AccessibilityIdentifier.cancel,
            action: #selector(cancel)
        )
        cancelButton.keyEquivalent = "\u{1b}"
        configureButton(
            saveButton,
            title: "Save CLI Definition",
            identifier: AccessibilityIdentifier.save,
            action: #selector(save)
        )
        saveButton.keyEquivalent = "\r"
    }

    private func configureTextField(
        _ field: NSTextField,
        placeholder: String,
        identifier: String,
        label: String
    ) {
        field.placeholderString = placeholder
        field.delegate = self
        field.identifier = NSUserInterfaceItemIdentifier(identifier)
        field.setAccessibilityLabel(label)
    }

    private func configureTextView(
        _ textView: NSTextView,
        identifier: String,
        label: String,
        monospaced: Bool = true
    ) {
        textView.isRichText = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.font = monospaced
            ? .monospacedSystemFont(
                ofSize: NSFont.systemFontSize,
                weight: .regular
            )
            : .systemFont(ofSize: NSFont.systemFontSize)
        textView.delegate = self
        textView.identifier = NSUserInterfaceItemIdentifier(identifier)
        textView.setAccessibilityLabel(label)
    }

    private func configurePopup(
        _ popup: NSPopUpButton,
        identifier: String,
        label: String,
        values: [String],
        action: Selector
    ) {
        popup.removeAllItems()
        for value in values {
            popup.addItem(withTitle: value)
            popup.lastItem?.representedObject = value
        }
        popup.target = self
        popup.action = action
        popup.identifier = NSUserInterfaceItemIdentifier(identifier)
        popup.setAccessibilityLabel(label)
    }

    private func configureButton(
        _ button: NSButton,
        title: String,
        identifier: String,
        action: Selector
    ) {
        button.title = title
        button.bezelStyle = .rounded
        button.target = self
        button.action = action
        button.identifier = NSUserInterfaceItemIdentifier(identifier)
    }

    private func configureContentView() {
        guard let window else {
            return
        }

        let modeControls = NSStackView(views: [
            NSTextField(labelWithString: "Input:"),
            inputModePopup,
            NSTextField(labelWithString: "Destination:"),
            destinationPopup,
            NSTextField(labelWithString: "Timeout:"),
            timeoutField,
            NSTextField(labelWithString: "seconds")
        ])
        modeControls.orientation = .horizontal
        modeControls.alignment = .centerY
        modeControls.spacing = 8
        timeoutField.widthAnchor.constraint(equalToConstant: 72).isActive = true
        let adapterControls = NSStackView(views: [
            adapterPopup,
            NSTextField(labelWithString: "Model:"),
            modelField
        ])
        adapterControls.orientation = .horizontal
        adapterControls.alignment = .centerY
        adapterControls.spacing = 8

        let argumentsScroll = makeTextScrollView(
            argumentsTextView,
            minimumHeight: 105
        )
        let preflightArgumentsScroll = makeTextScrollView(
            preflightArgumentsTextView,
            minimumHeight: 72
        )
        let environmentScroll = makeTextScrollView(
            environmentTextView,
            minimumHeight: 88
        )
        let systemPromptScroll = makeTextScrollView(
            systemPromptTextView,
            minimumHeight: 150
        )
        let promptScroll = makeTextScrollView(
            promptTemplateTextView,
            minimumHeight: 180
        )

        let environmentHelp = makeHelpLabel(
            "Enter a JSON object whose keys and values are strings, for example "
                + "{\"OLLAMA_HOST\":\"127.0.0.1:11434\"}."
        )
        let systemPromptHelp = makeHelpLabel(
            "Static policy only; the actual transcript, profile context, terms, "
                + "and language are not inserted here. For generic adapters, add "
                + "{{system_prompt_file}} to the argument that accepts a "
                + "system-prompt file. ZenWhisper writes it to a private per-run "
                + "temporary file. For Kiro, it becomes the prompt of an isolated "
                + "per-run custom agent. Placeholders are not allowed here."
        )
        let securityHelp = makeHelpLabel(
            "CLI definitions are trusted executable configuration. Changing an "
                + "executable, arguments, preflight, input mode, or environment "
                + "invalidates an existing Local classification. Remote and Unknown "
                + "presets require exact-revision consent when selected."
        )
        securityHelp.textColor = .systemOrange
        securityHelp.identifier = NSUserInterfaceItemIdentifier(
            AccessibilityIdentifier.securityHelp
        )
        securityHelp.setAccessibilityLabel(
            "CLI post-processor security guidance"
        )

        let form = NSStackView(views: [
            makeLabeledRow(title: "ID:", control: idField),
            makeLabeledRow(title: "Display name:", control: displayNameField),
            makeLabeledRow(title: "Executable:", control: executableField),
            makeLabeledRow(title: "Execution:", control: modeControls),
            makeLabeledRow(title: "Adapter:", control: adapterControls),
            makeLabeledRow(
                title: "Arguments (JSON):",
                control: makeVerticalGroup(
                    argumentsScroll,
                    argumentsHelpLabel
                ),
                alignment: .top
            ),
            makeLabeledRow(
                title: "Preflight executable:",
                control: preflightExecutableField
            ),
            makeLabeledRow(
                title: "Preflight args (JSON):",
                control: preflightArgumentsScroll,
                alignment: .top
            ),
            makeLabeledRow(
                title: "Preflight failure:",
                control: preflightFailureMessageField
            ),
            makeLabeledRow(
                title: "Environment (JSON):",
                control: makeVerticalGroup(environmentScroll, environmentHelp),
                alignment: .top
            ),
            makeLabeledRow(
                title: "System prompt:",
                control: makeVerticalGroup(
                    systemPromptScroll,
                    systemPromptHelp
                ),
                alignment: .top
            ),
            makeLabeledRow(
                title: "Prompt template:",
                control: makeVerticalGroup(promptScroll, promptHelpLabel),
                alignment: .top
            ),
            securityHelp,
            messageLabel
        ])
        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = 12
        form.translatesAutoresizingMaskIntoConstraints = false
        for view in form.arrangedSubviews {
            view.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
        }

        let documentView = FlippedDocumentView()
        documentView.translatesAutoresizingMaskIntoConstraints = false
        documentView.addSubview(form)
        NSLayoutConstraint.activate([
            form.leadingAnchor.constraint(
                equalTo: documentView.leadingAnchor,
                constant: Layout.inset
            ),
            form.trailingAnchor.constraint(
                equalTo: documentView.trailingAnchor,
                constant: -Layout.inset
            ),
            form.topAnchor.constraint(
                equalTo: documentView.topAnchor,
                constant: Layout.inset
            ),
            form.bottomAnchor.constraint(
                equalTo: documentView.bottomAnchor,
                constant: -Layout.inset
            )
        ])

        let scrollView = NSScrollView()
        scrollView.documentView = documentView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .noBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        let footerSpacer = NSView()
        footerSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = NSStackView(views: [
            footerSpacer,
            cancelButton,
            saveButton
        ])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = 8
        footer.translatesAutoresizingMaskIntoConstraints = false

        let contentView = NSView()
        contentView.addSubview(scrollView)
        contentView.addSubview(footer)
        window.contentView = contentView
        window.defaultButtonCell = saveButton.cell as? NSButtonCell

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: contentView.topAnchor),
            scrollView.bottomAnchor.constraint(
                equalTo: footer.topAnchor,
                constant: -12
            ),
            documentView.widthAnchor.constraint(
                equalTo: scrollView.contentView.widthAnchor
            ),
            documentView.leadingAnchor.constraint(
                equalTo: scrollView.contentView.leadingAnchor
            ),
            documentView.topAnchor.constraint(
                equalTo: scrollView.contentView.topAnchor
            ),
            documentView.heightAnchor.constraint(
                greaterThanOrEqualTo: scrollView.contentView.heightAnchor
            ),
            footer.leadingAnchor.constraint(
                equalTo: contentView.leadingAnchor,
                constant: Layout.inset
            ),
            footer.trailingAnchor.constraint(
                equalTo: contentView.trailingAnchor,
                constant: -Layout.inset
            ),
            footer.bottomAnchor.constraint(
                equalTo: contentView.bottomAnchor,
                constant: -Layout.inset
            )
        ])
    }

    private func makeLabeledRow(
        title: String,
        control: NSView,
        alignment: NSLayoutConstraint.Attribute = .centerY
    ) -> NSStackView {
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.widthAnchor.constraint(equalToConstant: Layout.labelWidth).isActive = true
        label.setContentHuggingPriority(.required, for: .horizontal)
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let row = NSStackView(views: [label, control])
        row.orientation = .horizontal
        row.alignment = alignment == .top ? .top : .centerY
        row.spacing = 10
        return row
    }

    private func makeVerticalGroup(_ views: NSView...) -> NSStackView {
        let group = NSStackView(views: views)
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = 5
        for view in views {
            view.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
        }
        return group
    }

    private func makeTextScrollView(
        _ textView: NSTextView,
        minimumHeight: CGFloat
    ) -> NSScrollView {
        let scroll = ChainedEditorScrollView()
        textView.minSize = NSSize(width: 0, height: minimumHeight)
        textView.maxSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        textView.textContainer?.widthTracksTextView = true
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.verticalScrollElasticity = .none
        scroll.borderType = .bezelBorder
        scroll.heightAnchor.constraint(
            greaterThanOrEqualToConstant: minimumHeight
        ).isActive = true
        return scroll
    }

    private func makeHelpLabel(_ text: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        return label
    }

    private func render(_ preset: EnhancementPostprocessorPreset) {
        isRendering = true
        idField.stringValue = preset.id
        displayNameField.stringValue = preset.displayName
        executableField.stringValue = preset.executable
        argumentsTextView.string = Self.formattedJSON(preset.arguments)
        inputModePopup.selectItem(withTitle: preset.inputMode.rawValue)
        adapterPopup.selectItem(withTitle: preset.adapter.rawValue)
        modelField.stringValue = preset.model
        destinationPopup.selectItem(withTitle: preset.destination.rawValue)
        timeoutField.stringValue = Self.formattedTimeout(preset.timeoutSeconds)
        preflightExecutableField.stringValue = preset.preflightExecutable
        preflightArgumentsTextView.string =
            Self.formattedJSON(preset.preflightArguments)
        preflightFailureMessageField.stringValue =
            preset.preflightFailureMessage
        environmentTextView.string = Self.formattedJSON(preset.environment)
        systemPromptTextView.string = preset.systemPrompt
        promptTemplateTextView.string = preset.promptTemplate
        idField.isEnabled = !isExistingPreset
        refreshArgumentsHelp()
        refreshPromptHelp()
        isRendering = false
        baselineState = draftState
        updateDocumentEditedState()
    }

    private func refreshArgumentsHelp() {
        let executable = (executableField.stringValue as NSString)
            .lastPathComponent
            .lowercased()
        let arguments = argumentsTextView.string.data(using: .utf8)
            .flatMap { try? JSONDecoder().decode([String].self, from: $0) }
            ?? []
        let general =
            "Arguments are a JSON string array and run without a shell. "

        switch executable {
        case "codex":
            argumentsHelpLabel.stringValue =
                general
                + Self.codexModelGuidance(arguments)
                + " "
                + Self.codexEffortGuidance(arguments)
        case "claude":
            argumentsHelpLabel.stringValue =
                general
                + Self.claudeModelGuidance(arguments)
                + " "
                + Self.claudeEffortGuidance(arguments)
        case "ollama":
            if let runIndex = arguments.firstIndex(of: "run"),
               arguments.indices.contains(runIndex + 1) {
                argumentsHelpLabel.stringValue =
                    general
                    + "Ollama model: “\(arguments[runIndex + 1])”. Change the "
                    + "model name immediately after “run”."
            } else {
                argumentsHelpLabel.stringValue =
                    general
                    + "Ollama model selection normally follows the “run” item."
            }
        case "kiro-cli":
            argumentsHelpLabel.stringValue =
                general
                + "Kiro model selection uses the Adapter model field. "
                + "Keep {{agent}} in the argument array; ZenWhisper creates "
                + "that tool-less custom agent for each run."
        default:
            argumentsHelpLabel.stringValue =
                general
                + "Provider model selection remains an ordinary CLI argument."
        }
    }

    private static func codexModelGuidance(_ arguments: [String]) -> String {
        if let modelOption = commandOption(
            in: arguments,
            names: ["--model", "-m"]
        ) {
            switch modelOption.form {
            case .separate:
                return "Codex model override: “\(modelOption.value)” via "
                    + "“\(modelOption.name)”. Change the following item, or "
                    + "remove both items to use the Codex CLI default."
            case .inline:
                return "Codex model override: “\(modelOption.value)” via "
                    + "“\(modelOption.name)=…”. Change the value after "
                    + "“\(modelOption.name)=”, or remove that item to use "
                    + "the Codex CLI default."
            }
        }
        if arguments.last == "-" {
            return "Codex model is not pinned. To override it, insert "
                + "“--model”, “MODEL_ID” immediately before the final “-” item. "
                + "Without a model option, Codex uses its CLI default."
        }
        return "Codex model is not pinned. Add “--model”, “MODEL_ID” to "
            + "the argument array to override it. Without a model option, "
            + "Codex uses its CLI default."
    }

    private func refreshPromptHelp() {
        let transportGuidance: String
        switch inputModePopup.selectedItem?.representedObject as? String {
        case EnhancementPostprocessorInputMode.argument.rawValue:
            transportGuidance =
                "Per-run data is rendered into {{prompt}} in argv. Process "
                + "arguments may be visible to other local processes. "
        default:
            transportGuidance =
                "Per-run data is rendered into this user message and sent "
                + "through stdin. "
        }
        promptHelpLabel.stringValue =
            transportGuidance
            + "The prompt must include {{transcript}}. Supported placeholders: "
            + "{{agent}}, {{prompt}}, {{transcript}}, {{context}}, {{terms}}, "
            + "{{profile_name}}, {{language}}, and {{boundary}}. Use "
            + "{{boundary}} in delimiter names when separating untrusted data. "
            + "Output is always stdout."
    }

    private static func codexEffortGuidance(_ arguments: [String]) -> String {
        if let effortOption = codexConfigOption(
            in: arguments,
            key: "model_reasoning_effort"
        ) {
            switch effortOption.form {
            case .separate:
                return "Codex reasoning effort: “\(effortOption.value)” via "
                    + "“\(effortOption.name)”, "
                    + "“model_reasoning_effort=…”. Change the following "
                    + "config item to adjust it."
            case .inline:
                return "Codex reasoning effort: “\(effortOption.value)” via "
                    + "“\(effortOption.name)=model_reasoning_effort=…”. "
                    + "Change the value after “model_reasoning_effort=” "
                    + "to adjust it."
            }
        }
        return "To set Codex reasoning effort, add "
            + "“-c”, “model_reasoning_effort=LEVEL”."
    }

    private static func claudeModelGuidance(_ arguments: [String]) -> String {
        guard let modelOption = commandOption(
            in: arguments,
            names: ["--model"]
        ) else {
            return "To choose a Claude model, add “--model”, “MODEL_ID”."
        }
        switch modelOption.form {
        case .separate:
            return "Claude model: “\(modelOption.value)”. Change the "
                + "following item after “--model”."
        case .inline:
            return "Claude model: “\(modelOption.value)”. Change the "
                + "value after “--model=”."
        }
    }

    private static func claudeEffortGuidance(_ arguments: [String]) -> String {
        let model = commandOption(
            in: arguments,
            names: ["--model"]
        )?.value.lowercased()
        let usesHaiku = model == "haiku" || model?.contains("haiku") == true
        if let effortOption = commandOption(
            in: arguments,
            names: ["--effort"]
        ) {
            if usesHaiku {
                return "Claude reasoning effort “\(effortOption.value)” is "
                    + "configured, but current Haiku models do not support "
                    + "“--effort”. Remove the effort option or change "
                    + "“--model” to a supported model."
            }
            switch effortOption.form {
            case .separate:
                return "Claude reasoning effort: “\(effortOption.value)” via "
                    + "“--effort”. Change the following item to adjust it."
            case .inline:
                return "Claude reasoning effort: “\(effortOption.value)” via "
                    + "“--effort=…”. Change the value after “--effort=” "
                    + "to adjust it."
            }
        }
        if usesHaiku {
            return "Current Claude Haiku models do not support “--effort”. "
                + "To use effort, change “--model” to a supported model, then "
                + "add “--effort”, “LEVEL”."
        }
        return "For a supported Claude model, add “--effort”, “LEVEL” "
            + "to set reasoning effort."
    }

    private static func codexConfigOption(
        in arguments: [String],
        key: String
    ) -> CommandOptionMatch? {
        for (index, argument) in arguments.enumerated() {
            if ["-c", "--config"].contains(argument),
               arguments.indices.contains(index + 1),
               let value = configAssignmentValue(
                   arguments[index + 1],
                   key: key
               ) {
                return CommandOptionMatch(
                    name: argument,
                    value: value,
                    form: .separate
                )
            }
            for name in ["-c", "--config"] {
                let prefix = "\(name)="
                if argument.hasPrefix(prefix),
                   let value = configAssignmentValue(
                       String(argument.dropFirst(prefix.count)),
                       key: key
                   ) {
                    return CommandOptionMatch(
                        name: name,
                        value: value,
                        form: .inline
                    )
                }
            }
        }
        return nil
    }

    private static func configAssignmentValue(
        _ assignment: String,
        key: String
    ) -> String? {
        let prefix = "\(key)="
        guard assignment.hasPrefix(prefix) else {
            return nil
        }
        let value = String(assignment.dropFirst(prefix.count))
        guard value.count >= 2,
              let first = value.first,
              let last = value.last,
              (first == "\"" && last == "\"")
                || (first == "'" && last == "'") else {
            return value
        }
        return String(value.dropFirst().dropLast())
    }

    private static func commandOption(
        in arguments: [String],
        names: Set<String>
    ) -> CommandOptionMatch? {
        for (index, argument) in arguments.enumerated() {
            if names.contains(argument),
               arguments.indices.contains(index + 1) {
                return CommandOptionMatch(
                    name: argument,
                    value: arguments[index + 1],
                    form: .separate
                )
            }
            for name in names {
                let prefix = "\(name)="
                if argument.hasPrefix(prefix) {
                    return CommandOptionMatch(
                        name: name,
                        value: String(argument.dropFirst(prefix.count)),
                        form: .inline
                    )
                }
            }
        }
        return nil
    }

    private var draftState: DraftState {
        DraftState(
            id: idField.stringValue,
            displayName: displayNameField.stringValue,
            executable: executableField.stringValue,
            argumentsJSON: argumentsTextView.string,
            inputMode: inputModePopup.selectedItem?.representedObject as? String
                ?? EnhancementPostprocessorInputMode.stdin.rawValue,
            adapter: adapterPopup.selectedItem?.representedObject as? String
                ?? EnhancementPostprocessorAdapter.generic.rawValue,
            model: modelField.stringValue,
            destination:
                destinationPopup.selectedItem?.representedObject as? String
                ?? EnhancementDataDestination.unknown.rawValue,
            timeout: timeoutField.stringValue,
            preflightExecutable: preflightExecutableField.stringValue,
            preflightArgumentsJSON: preflightArgumentsTextView.string,
            preflightFailureMessage: preflightFailureMessageField.stringValue,
            environmentJSON: environmentTextView.string,
            systemPrompt: systemPromptTextView.string,
            promptTemplate: promptTemplateTextView.string
        )
    }

    @objc private func popupChanged() {
        guard !isRendering else {
            return
        }
        refreshPromptHelp()
        clearMessage()
        updateDocumentEditedState()
    }

    @objc private func cancel() {
        guard let window, windowShouldClose(window) else {
            return
        }
        close()
    }

    @objc private func save() {
        guard !isSaving, let onSave else {
            return
        }

        let proposed: EnhancementPostprocessorPreset
        do {
            proposed = try makePreset()
        } catch {
            showMessage(error.localizedDescription)
            return
        }
        guard reviewOverwriteIfNeeded(for: proposed) else {
            return
        }
        guard let review = reviewedDestination(for: proposed) else {
            return
        }

        isSaving = true
        setControlsEnabled(false)
        switch onSave(
            review.preset,
            expectedFingerprint,
            review.destinationWasExplicitlyReclassified
        ) {
        case .success(let catalog):
            baselineState = draftState
            onSaved?(catalog, review.preset.id)
            close()
        case .failure(let error):
            isSaving = false
            setControlsEnabled(true)
            showMessage(
                "Could not save CLI post-processor: \(error.localizedDescription)"
            )
        }
    }

    private func makePreset() throws -> EnhancementPostprocessorPreset {
        let id = idField.stringValue.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard id.range(
            of: #"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"#,
            options: .regularExpression
        ) != nil,
        id != "off",
        id != "dictionary" else {
            throw DraftValidationError(
                message: "Enter an ID of up to 64 letters, numbers, underscores, or hyphens. “off” and “dictionary” are reserved."
            )
        }

        let displayName = displayNameField.stringValue.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard !displayName.isEmpty else {
            throw DraftValidationError(message: "Enter a display name.")
        }
        let executable = executableField.stringValue.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard !executable.isEmpty else {
            throw DraftValidationError(message: "Enter an executable.")
        }
        guard !executable.contains("{{"), !executable.contains("}}") else {
            throw DraftValidationError(
                message: "The executable cannot contain placeholders."
            )
        }

        let arguments: [String] = try Self.decodeJSON(
            argumentsTextView.string,
            field: "Arguments",
            as: [String].self
        )
        let preflightArguments: [String] = try Self.decodeJSON(
            preflightArgumentsTextView.string,
            field: "Preflight arguments",
            as: [String].self
        )
        let environment: [String: String] = try Self.decodeJSON(
            environmentTextView.string,
            field: "Environment",
            as: [String: String].self
        )
        guard arguments.count
                <= EnhancementCatalogLimits.maximumPresetArgumentCount,
              preflightArguments.count
                <= EnhancementCatalogLimits.maximumPresetArgumentCount else {
            throw DraftValidationError(
                message: "The definition contains too many arguments."
            )
        }
        guard environment.count
                <= EnhancementCatalogLimits.maximumPresetEnvironmentEntryCount else {
            throw DraftValidationError(
                message: "The definition contains too many environment entries."
            )
        }

        guard let inputModeRaw =
                inputModePopup.selectedItem?.representedObject as? String,
              let inputMode = EnhancementPostprocessorInputMode(
                rawValue: inputModeRaw
              ) else {
            throw DraftValidationError(message: "Choose an input mode.")
        }
        guard let destinationRaw =
                destinationPopup.selectedItem?.representedObject as? String,
              let destination = EnhancementDataDestination(
                rawValue: destinationRaw
              ) else {
            throw DraftValidationError(message: "Choose a data destination.")
        }
        guard let adapterRaw =
                adapterPopup.selectedItem?.representedObject as? String,
              let adapter = EnhancementPostprocessorAdapter(
                rawValue: adapterRaw
              ) else {
            throw DraftValidationError(message: "Choose an adapter.")
        }
        let model = modelField.stringValue.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        let timeoutText = timeoutField.stringValue.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard let timeout = Double(timeoutText),
              timeout.isFinite,
              timeout > 0,
              timeout
                <= EnhancementCatalogLimits.maximumPresetTimeoutSeconds else {
            throw DraftValidationError(
                message: "Enter a timeout greater than 0 and no more than 300 seconds."
            )
        }

        let promptTemplate = promptTemplateTextView.string
        guard promptTemplate.contains("{{transcript}}") else {
            throw DraftValidationError(
                message: "The prompt template must contain {{transcript}}."
            )
        }
        let systemPrompt = systemPromptTextView.string
        guard !systemPrompt.contains("{{"), !systemPrompt.contains("}}") else {
            throw DraftValidationError(
                message: "The system prompt cannot contain placeholders."
            )
        }
        let hasSystemPromptFile = arguments.contains {
            $0.contains("{{system_prompt_file}}")
        }
        switch adapter {
        case .generic:
            guard model.isEmpty else {
                throw DraftValidationError(
                    message: "Set generic CLI models in the argument array, not the Adapter model field."
                )
            }
            guard systemPrompt.isEmpty != hasSystemPromptFile else {
                throw DraftValidationError(
                    message: "Set both the system prompt and "
                        + "{{system_prompt_file}} argument, or leave both empty."
                )
            }
            switch inputMode {
            case .stdin:
                guard arguments.allSatisfy({
                    let withoutSystemPromptFile = $0.replacingOccurrences(
                        of: "{{system_prompt_file}}",
                        with: ""
                    )
                    return !withoutSystemPromptFile.contains("{{")
                        && !withoutSystemPromptFile.contains("}}")
                }) else {
                    throw DraftValidationError(
                        message: "stdin arguments may only use "
                            + "{{system_prompt_file}}. Put transcript placeholders "
                            + "in the prompt template."
                    )
                }
            case .argument:
                guard arguments.contains(where: { $0.contains("{{prompt}}") }) else {
                    throw DraftValidationError(
                        message: "argument mode requires {{prompt}} in an argument."
                    )
                }
            }
        case .kiro:
            let executableName = (executable as NSString).lastPathComponent.lowercased()
            let hasAgent = arguments.contains { $0.contains("{{agent}}") }
            let usesOnlyAgent = arguments.allSatisfy {
                let withoutAgent = $0.replacingOccurrences(
                    of: "{{agent}}",
                    with: ""
                )
                return !withoutAgent.contains("{{") && !withoutAgent.contains("}}")
            }
            guard executableName == "kiro-cli",
                  inputMode == .stdin,
                  !model.isEmpty,
                  !systemPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  hasAgent,
                  usesOnlyAgent else {
                throw DraftValidationError(
                    message: "Kiro requires executable kiro-cli, stdin, a model, "
                        + "a system prompt, and only {{agent}} in arguments."
                )
            }
        }

        let preflightExecutable = preflightExecutableField.stringValue
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !preflightExecutable.isEmpty || preflightArguments.isEmpty else {
            throw DraftValidationError(
                message: "Preflight arguments require a preflight executable."
            )
        }

        return EnhancementPostprocessorPreset(
            id: id,
            displayName: displayName,
            executable: executable,
            arguments: arguments,
            preflightExecutable: preflightExecutable,
            preflightArguments: preflightArguments,
            preflightFailureMessage:
                preflightFailureMessageField.stringValue
                    .trimmingCharacters(in: .whitespacesAndNewlines),
            inputMode: inputMode,
            destination: destination,
            timeoutSeconds: timeout,
            adapter: adapter,
            model: model,
            systemPrompt: systemPrompt,
            promptTemplate: promptTemplate,
            environment: environment,
            destinationReviewRevision:
                originalPreset.destinationReviewRevision,
            extraFields: originalPreset.extraFields
        )
    }

    private func reviewOverwriteIfNeeded(
        for proposed: EnhancementPostprocessorPreset
    ) -> Bool {
        guard !isExistingPreset, existingPresetIDs.contains(proposed.id) else {
            return true
        }

        let explanation =
            "A bundled default or local definition already uses “\(proposed.id)”. "
            + "Replacing it will save a local override with the values shown here."
        let decision: PostprocessorOverwriteDecision
        if let onReviewOverwrite {
            decision = onReviewOverwrite(proposed, explanation)
        } else {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Replace Existing CLI Post-Processor?"
            alert.informativeText = explanation
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Replace")
            decision = alert.runModal() == .alertSecondButtonReturn
                ? .replace
                : .cancel
        }
        return decision == .replace
    }

    private func reviewedDestination(
        for proposed: EnhancementPostprocessorPreset
    ) -> (
        preset: EnhancementPostprocessorPreset,
        destinationWasExplicitlyReclassified: Bool
    )? {
        let transportChanged =
            proposed.commandRevision != originalPreset.commandRevision
        guard proposed.destination == .local,
              transportChanged || originalPreset.destination != .local else {
            return (
                proposed,
                transportChanged && proposed.destination == .remote
            )
        }

        let explanation =
            "This command or its trust boundary is new or changed. “Local” is safe "
            + "only after you have verified that the executable, arguments, "
            + "preflight, and environment do not send transcript or profile data "
            + "outside this Mac."
        let decision: PostprocessorLocalDestinationDecision
        if let onReviewLocalDestination {
            decision = onReviewLocalDestination(proposed, explanation)
        } else {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Verify Local Data Destination"
            alert.informativeText = explanation
            alert.addButton(withTitle: "Save as Unknown")
            alert.addButton(withTitle: "I Verified It Is Local")
            alert.addButton(withTitle: "Cancel")
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                decision = .saveAsUnknown
            case .alertSecondButtonReturn:
                decision = .confirmLocal
            default:
                decision = .cancel
            }
        }

        switch decision {
        case .saveAsUnknown:
            var unknown = proposed
            unknown.destination = .unknown
            unknown.destinationReviewRevision = nil
            return (unknown, false)
        case .confirmLocal:
            return (proposed, true)
        case .cancel:
            return nil
        }
    }

    private static func formattedTimeout(_ value: Double) -> String {
        value.rounded() == value
            ? String(Int(value))
            : String(value)
    }

    private static func formattedJSON<T: Encodable>(_ value: T) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [
            .prettyPrinted,
            .sortedKeys,
            .withoutEscapingSlashes
        ]
        guard let data = try? encoder.encode(value),
              let text = String(data: data, encoding: .utf8) else {
            return ""
        }
        return text
    }

    private static func decodeJSON<T: Decodable>(
        _ text: String,
        field: String,
        as type: T.Type
    ) throws -> T {
        guard let data = text.data(using: .utf8),
              let value = try? JSONDecoder().decode(type, from: data) else {
            throw DraftValidationError(
                message: "\(field) must be valid JSON with string values."
            )
        }
        return value
    }

    private func showMessage(_ message: String) {
        messageLabel.stringValue = message
        messageLabel.isHidden = false
        updateDocumentEditedState()
    }

    private func clearMessage() {
        messageLabel.stringValue = ""
        messageLabel.isHidden = true
    }

    private func setControlsEnabled(_ enabled: Bool) {
        idField.isEnabled = enabled && !isExistingPreset
        for field in [
            displayNameField,
            executableField,
            timeoutField,
            modelField,
            preflightExecutableField,
            preflightFailureMessageField
        ] {
            field.isEnabled = enabled
        }
        for textView in [
            argumentsTextView,
            preflightArgumentsTextView,
            environmentTextView,
            systemPromptTextView,
            promptTemplateTextView
        ] {
            textView.isEditable = enabled
        }
        inputModePopup.isEnabled = enabled
        adapterPopup.isEnabled = enabled
        destinationPopup.isEnabled = enabled
        cancelButton.isEnabled = enabled
        saveButton.isEnabled = enabled && isDirty && onSave != nil
    }

    private func updateDocumentEditedState() {
        guard baselineState != nil else {
            return
        }
        window?.isDocumentEdited = isDirty
        if !isSaving {
            saveButton.isEnabled = isDirty && onSave != nil
        }
    }

    private func dismissOnce() {
        guard !didDismiss else {
            return
        }
        didDismiss = true
        onDismiss?()
    }
}

private final class FlippedDocumentView: NSView {
    override var isFlipped: Bool {
        true
    }
}

final class ChainedEditorScrollView: NSScrollView {
    override func scrollWheel(with event: NSEvent) {
        guard let ancestorScrollView else {
            super.scrollWheel(with: event)
            return
        }
        let originBeforeScrolling = contentView.bounds.origin
        let ancestorOriginBeforeScrolling =
            ancestorScrollView.contentView.bounds.origin
        super.scrollWheel(with: event)

        guard abs(verticalDelta(for: event)) > abs(event.scrollingDeltaX),
              abs(contentView.bounds.origin.y - originBeforeScrolling.y) < 0.5,
              abs(
                  ancestorScrollView.contentView.bounds.origin.y
                      - ancestorOriginBeforeScrolling.y
              ) < 0.5 else {
            return
        }

        let proposedBounds = ancestorScrollView.contentView.bounds.offsetBy(
            dx: 0,
            dy: -verticalDelta(for: event)
        )
        let constrainedBounds =
            ancestorScrollView.contentView.constrainBoundsRect(proposedBounds)
        ancestorScrollView.contentView.scroll(to: constrainedBounds.origin)
        ancestorScrollView.reflectScrolledClipView(
            ancestorScrollView.contentView
        )
    }

    var ancestorScrollView: NSScrollView? {
        var ancestor = superview
        while let current = ancestor {
            if let scrollView = current as? NSScrollView {
                return scrollView
            }
            ancestor = current.superview
        }
        return nil
    }

    private func verticalDelta(for event: NSEvent) -> CGFloat {
        if event.scrollingDeltaY != 0 {
            return event.scrollingDeltaY
        }
        return event.deltaY * 10
    }
}
