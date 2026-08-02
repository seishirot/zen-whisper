import AppKit
import Foundation

struct SettingsSaveRequest {
    let settings: SettingsSnapshot
    let launchAtLoginEnabled: Bool
    let runtimeSettingsChanged: Bool
    let launchAtLoginChanged: Bool
}

struct AuthoritativeSettings {
    let settings: SettingsSnapshot
    let launchAtLoginStatus: LoginItemStatus
}

enum SettingsSaveOutcome {
    case success(AuthoritativeSettings)
    case partial(AuthoritativeSettings, Error)
    case failure(Error)
}

struct SettingsHotkeyRecorderSession {
    private(set) var generation = 0
    private(set) var isActive = false

    mutating func begin() -> Int {
        generation += 1
        isActive = true
        return generation
    }

    mutating func finish(generation: Int) -> Bool {
        guard isActive, generation == self.generation else {
            return false
        }
        isActive = false
        return true
    }

    mutating func cancel() -> Bool {
        guard isActive else {
            return false
        }
        generation += 1
        isActive = false
        return true
    }
}

@MainActor
final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    typealias SaveHandler = (SettingsSaveRequest) -> SettingsSaveOutcome
    typealias ProfileSaveHandler = (
        EnhancementProfile,
        EnhancementFileFingerprint?
    ) -> Result<EnhancementCatalogSnapshot, Error>
    typealias PostprocessorSaveHandler = (
        EnhancementPostprocessorPreset,
        EnhancementFileFingerprint?,
        Bool
    ) -> Result<EnhancementCatalogSnapshot, Error>
    typealias ExternalDestinationConfirmationHandler = (
        EnhancementPostprocessorPreset,
        String
    ) -> Bool
    typealias PrimaryHotkeyRecorder = (
        HotkeyShortcut,
        @escaping (HotkeyShortcut?) -> Void
    ) -> Void
    typealias SubmitHotkeyRecorder = (
        HotkeyShortcut?,
        @escaping (HotkeyShortcut?) -> Void
    ) -> Void

    enum AccessibilityIdentifier {
        static let window = "settings.window"
        static let primaryHotkey = "settings.primaryHotkey"
        static let recordPrimaryHotkey = "settings.recordPrimaryHotkey"
        static let submitHotkey = "settings.submitHotkey"
        static let recordSubmitHotkey = "settings.recordSubmitHotkey"
        static let engine = "settings.engine"
        static let model = "settings.model"
        static let language = "settings.language"
        static let profile = "settings.profile"
        static let newProfile = "settings.newProfile"
        static let editProfile = "settings.editProfile"
        static let postprocessing = "settings.postprocessing"
        static let newPostprocessor = "settings.newPostprocessor"
        static let editPostprocessor = "settings.editPostprocessor"
        static let enhancementMessage = "settings.enhancementMessage"
        static let silenceAutoStop = "settings.silenceAutoStop"
        static let microphone = "settings.microphone"
        static let outputMode = "settings.outputMode"
        static let launchAtLogin = "settings.launchAtLogin"
        static let launchAtLoginMessage = "settings.launchAtLoginMessage"
        static let busyMessage = "settings.busyMessage"
        static let validationMessage = "settings.validationMessage"
        static let save = "settings.save"
        static let cancel = "settings.cancel"
    }

    private enum Layout {
        static let defaultContentSize = NSSize(width: 720, height: 620)
        static let minimumContentSize = NSSize(width: 620, height: 620)
        static let frameAutosaveName = "ZenWhisperSettingsWindow.v2"
        static let horizontalInset: CGFloat = 22
        static let topInset: CGFloat = 20
        static let footerSpacing: CGFloat = 16
        static let bottomInset: CGFloat = 18
        static let generalSectionMinimumHeight: CGFloat = 52
    }

    var onSave: SaveHandler? {
        didSet { refreshEnabledState() }
    }
    var onRecordPrimaryHotkey: PrimaryHotkeyRecorder? {
        didSet { refreshEnabledState() }
    }
    var onRecordSubmitHotkey: SubmitHotkeyRecorder? {
        didSet { refreshEnabledState() }
    }
    var onCancelHotkeyRecording: (() -> Void)?
    var onRequestAudioInputDevices: (() -> [AudioInputDevice])?
    var onSaveProfile: ProfileSaveHandler? {
        didSet { refreshEnabledState() }
    }
    var onSavePostprocessor: PostprocessorSaveHandler? {
        didSet { refreshEnabledState() }
    }
    var onConfirmExternalDestination: ExternalDestinationConfirmationHandler?

    private let registry: ModelRegistry
    private var editorState: SettingsEditorState
    private var enhancementCatalog: EnhancementCatalogSnapshot
    private var audioInputDevices: [AudioInputDevice] = []
    private var hotkeyRecorderSession = SettingsHotkeyRecorderSession()
    private var isBusy = false
    private var isSaving = false
    private var isConfirmingClose = false
    private var allowConfirmedClose = false

    private var primaryHotkeyChoices: [HotkeyShortcut] = []
    private var submitHotkeyChoices: [HotkeyShortcut?] = []
    private var engineChoices: [String] = []
    private var modelChoices: [String] = []
    private var languageChoices: [String] = []
    private var profileChoices: [String?] = []
    private var postprocessingChoices: [PostprocessingSelection] = []

    private let primaryHotkeyPopup = NSPopUpButton()
    private let recordPrimaryHotkeyButton = NSButton()
    private let submitHotkeyPopup = NSPopUpButton()
    private let recordSubmitHotkeyButton = NSButton()
    private let enginePopup = NSPopUpButton()
    private let modelPopup = NSPopUpButton()
    private let languagePopup = NSPopUpButton()
    private let profilePopup = NSPopUpButton()
    private let newProfileButton = NSButton()
    private let editProfileButton = NSButton()
    private let postprocessingPopup = NSPopUpButton()
    private let newPostprocessorButton = NSButton()
    private let editPostprocessorButton = NSButton()
    private let enhancementMessageLabel = NSTextField(wrappingLabelWithString: "")
    private let silenceAutoStopCheckbox = NSButton()
    private let microphonePopup = NSPopUpButton()
    private let microphoneMessageLabel = NSTextField(wrappingLabelWithString: "")
    private let outputModePopup = NSPopUpButton()
    private let launchAtLoginCheckbox = NSButton()
    private let launchAtLoginMessageLabel = NSTextField(wrappingLabelWithString: "")
    private let busyMessageLabel = NSTextField(wrappingLabelWithString: "")
    private let validationMessageLabel = NSTextField(wrappingLabelWithString: "")
    private let cancelButton = NSButton()
    private let saveButton = NSButton()
    private var contentStack: NSStackView?
    private var footerStack: NSStackView?
    private var managedVisibilityContexts:
        [ObjectIdentifier: (stack: NSStackView, index: Int)] = [:]
    private var isUpdatingWindowLayout = false
    private var shouldCenterWindowOnFirstShow: Bool
    private var profileEditorWindowController: ProfileEditorWindowController?
    private var postprocessorEditorWindowController:
        PostprocessorEditorWindowController?

    init(
        registry: ModelRegistry,
        settings: SettingsSnapshot,
        launchAtLoginStatus: LoginItemStatus,
        enhancementCatalog: EnhancementCatalogSnapshot = EnhancementCatalogSnapshot()
    ) {
        self.registry = registry
        self.enhancementCatalog = enhancementCatalog
        editorState = SettingsEditorState(
            settings: settings,
            launchAtLoginStatus: launchAtLoginStatus,
            registry: registry
        )

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: Layout.defaultContentSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Settings…"
        window.isReleasedWhenClosed = false
        window.contentMinSize = Layout.minimumContentSize
        shouldCenterWindowOnFirstShow =
            !window.setFrameUsingName(Layout.frameAutosaveName)
        window.setFrameAutosaveName(Layout.frameAutosaveName)

        super.init(window: window)
        reconcileDraftPostprocessorApproval()
        window.delegate = self
        window.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.window)
        window.setAccessibilityLabel("Zen Whisper Settings")
        configureControls()
        configureContentView()
        render()
    }

    required init?(coder: NSCoder) {
        nil
    }

    func showSettings() {
        guard let window else {
            return
        }
        refreshAudioInputDevices()
        render()
        NSApp.activate(ignoringOtherApps: true)
        if shouldCenterWindowOnFirstShow {
            window.center()
            shouldCenterWindowOnFirstShow = false
        }
        showWindow(nil)
        window.makeKeyAndOrderFront(nil)
    }

    func synchronize(
        authoritativeSettings: SettingsSnapshot,
        launchAtLoginStatus: LoginItemStatus,
        audioInputDevices: [AudioInputDevice],
        isBusy: Bool,
        enhancementCatalog: EnhancementCatalogSnapshot? = nil
    ) {
        editorState.synchronize(
            authoritativeSettings: authoritativeSettings,
            launchAtLoginStatus: launchAtLoginStatus
        )
        replaceAudioInputDevices(audioInputDevices)
        if let enhancementCatalog {
            self.enhancementCatalog = enhancementCatalog
        }
        reconcileDraftPostprocessorApproval()
        self.isBusy = isBusy
        render()
    }

    func updateBusyState(_ isBusy: Bool) {
        self.isBusy = isBusy
        refreshEnabledState()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        cancelActiveHotkeyRecording()
        if allowConfirmedClose {
            allowConfirmedClose = false
            return true
        }
        guard editorState.isDirty else {
            return true
        }
        confirmDiscardBeforeClosing()
        return false
    }

    func windowDidBecomeKey(_ notification: Notification) {
        refreshAudioInputDevices()
        render()
    }

    func windowDidResize(_ notification: Notification) {
        updateWindowSizeConstraints()
    }

    func windowWillClose(_ notification: Notification) {
        cancelActiveHotkeyRecording()
        if let editorWindow = profileEditorWindowController?.window,
           window?.attachedSheet === editorWindow {
            window?.endSheet(editorWindow)
        }
        profileEditorWindowController?.close()
        profileEditorWindowController = nil
        if let editorWindow = postprocessorEditorWindowController?.window,
           window?.attachedSheet === editorWindow {
            window?.endSheet(editorWindow)
        }
        postprocessorEditorWindowController?.close()
        postprocessorEditorWindowController = nil
    }

    private func configureControls() {
        configurePopup(
            primaryHotkeyPopup,
            label: "Primary hotkey",
            identifier: AccessibilityIdentifier.primaryHotkey,
            action: #selector(primaryHotkeyChanged)
        )
        configureButton(
            recordPrimaryHotkeyButton,
            title: "Record…",
            label: "Record primary hotkey",
            identifier: AccessibilityIdentifier.recordPrimaryHotkey,
            action: #selector(recordPrimaryHotkey)
        )
        configurePopup(
            submitHotkeyPopup,
            label: "Submit hotkey",
            identifier: AccessibilityIdentifier.submitHotkey,
            action: #selector(submitHotkeyChanged)
        )
        configureButton(
            recordSubmitHotkeyButton,
            title: "Record…",
            label: "Record submit hotkey",
            identifier: AccessibilityIdentifier.recordSubmitHotkey,
            action: #selector(recordSubmitHotkey)
        )
        configurePopup(
            enginePopup,
            label: "Recognition engine",
            identifier: AccessibilityIdentifier.engine,
            action: #selector(engineChanged)
        )
        configurePopup(
            modelPopup,
            label: "Recognition model",
            identifier: AccessibilityIdentifier.model,
            action: #selector(modelChanged)
        )
        configurePopup(
            languagePopup,
            label: "Recognition language",
            identifier: AccessibilityIdentifier.language,
            action: #selector(languageChanged)
        )
        configurePopup(
            profilePopup,
            label: "Recognition profile",
            identifier: AccessibilityIdentifier.profile,
            action: #selector(profileChanged)
        )
        configureButton(
            newProfileButton,
            title: "New…",
            label: "Create recognition profile",
            identifier: AccessibilityIdentifier.newProfile,
            action: #selector(createProfile)
        )
        configureButton(
            editProfileButton,
            title: "Edit…",
            label: "Edit selected recognition profile",
            identifier: AccessibilityIdentifier.editProfile,
            action: #selector(editProfile)
        )
        configurePopup(
            postprocessingPopup,
            label: "Post-processing",
            identifier: AccessibilityIdentifier.postprocessing,
            action: #selector(postprocessingChanged)
        )
        configureButton(
            newPostprocessorButton,
            title: "New…",
            label: "Create CLI post-processor",
            identifier: AccessibilityIdentifier.newPostprocessor,
            action: #selector(createPostprocessor)
        )
        configureButton(
            editPostprocessorButton,
            title: "Edit…",
            label: "Edit selected CLI post-processor",
            identifier: AccessibilityIdentifier.editPostprocessor,
            action: #selector(editPostprocessor)
        )
        enhancementMessageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        enhancementMessageLabel.textColor = .secondaryLabelColor
        enhancementMessageLabel.identifier =
            NSUserInterfaceItemIdentifier(AccessibilityIdentifier.enhancementMessage)
        enhancementMessageLabel.setAccessibilityLabel("Enhancement data destination and status")
        configureCheckbox(
            silenceAutoStopCheckbox,
            title: "Automatically stop after silence",
            label: "Automatically stop recording after silence",
            identifier: AccessibilityIdentifier.silenceAutoStop,
            action: #selector(silenceAutoStopChanged)
        )
        configurePopup(
            microphonePopup,
            label: "Microphone",
            identifier: AccessibilityIdentifier.microphone,
            action: #selector(microphoneChanged)
        )
        microphoneMessageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        microphoneMessageLabel.textColor = .systemOrange
        microphoneMessageLabel.isHidden = true

        configurePopup(
            outputModePopup,
            label: "Output mode",
            identifier: AccessibilityIdentifier.outputMode,
            action: #selector(outputModeChanged)
        )
        configureCheckbox(
            launchAtLoginCheckbox,
            title: "Launch Zen Whisper at login",
            label: "Launch Zen Whisper at login",
            identifier: AccessibilityIdentifier.launchAtLogin,
            action: #selector(launchAtLoginChanged)
        )
        launchAtLoginCheckbox.allowsMixedState = true
        launchAtLoginMessageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        launchAtLoginMessageLabel.textColor = .systemOrange
        launchAtLoginMessageLabel.identifier =
            NSUserInterfaceItemIdentifier(AccessibilityIdentifier.launchAtLoginMessage)
        launchAtLoginMessageLabel.setAccessibilityLabel("Launch at Login status")
        launchAtLoginMessageLabel.isHidden = true

        busyMessageLabel.stringValue =
            "Recording or model work is in progress. Runtime changes can be saved when it finishes."
        busyMessageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        busyMessageLabel.textColor = .secondaryLabelColor
        busyMessageLabel.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.busyMessage)
        busyMessageLabel.setAccessibilityLabel(busyMessageLabel.stringValue)
        busyMessageLabel.isHidden = true

        validationMessageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        validationMessageLabel.textColor = .systemRed
        validationMessageLabel.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.validationMessage)
        validationMessageLabel.setAccessibilityLabel("Settings message")
        validationMessageLabel.isHidden = true

        configureButton(
            cancelButton,
            title: "Cancel",
            label: "Cancel settings changes",
            identifier: AccessibilityIdentifier.cancel,
            action: #selector(cancel)
        )
        cancelButton.keyEquivalent = "\u{1b}"

        configureButton(
            saveButton,
            title: "Save",
            label: "Save settings",
            identifier: AccessibilityIdentifier.save,
            action: #selector(save)
        )
        saveButton.keyEquivalent = "\r"
    }

    private func configureContentView() {
        guard let window else {
            return
        }

        let shortcutSection = makeSection(
            title: "Keyboard Shortcuts",
            views: [
                makeLabeledRow(
                    title: "Primary:",
                    control: makeControlRow(primaryHotkeyPopup, recordPrimaryHotkeyButton)
                ),
                makeLabeledRow(
                    title: "Submit:",
                    control: makeControlRow(submitHotkeyPopup, recordSubmitHotkeyButton)
                )
            ]
        )
        let recognitionSection = makeSection(
            title: "Recognition",
            views: [
                makeLabeledRow(title: "Engine:", control: enginePopup),
                makeLabeledRow(title: "Model:", control: modelPopup),
                makeLabeledRow(title: "Language:", control: languagePopup)
            ]
        )
        let enhancementSection = makeSection(
            title: "Enhancement",
            views: [
                makeLabeledRow(
                    title: "Profile:",
                    control: makeControlRow(profilePopup, newProfileButton, editProfileButton)
                ),
                makeLabeledRow(
                    title: "Post-process:",
                    control: makeControlRow(
                        postprocessingPopup,
                        newPostprocessorButton,
                        editPostprocessorButton
                    )
                ),
                enhancementMessageLabel
            ]
        )
        let recordingSection = makeSection(
            title: "Recording",
            views: [
                silenceAutoStopCheckbox,
                makeLabeledRow(title: "Microphone:", control: microphonePopup),
                microphoneMessageLabel
            ]
        )
        let outputSection = makeSection(
            title: "Output",
            views: [
                makeLabeledRow(title: "Mode:", control: outputModePopup)
            ]
        )
        let generalSection = makeSection(
            title: "General",
            views: [launchAtLoginCheckbox, launchAtLoginMessageLabel],
            minimumHeight: Layout.generalSectionMinimumHeight
        )

        let buttonSpacer = NSView()
        buttonSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttonRow = NSStackView(views: [buttonSpacer, cancelButton, saveButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.spacing = 10
        buttonRow.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView(views: [
            shortcutSection,
            recognitionSection,
            enhancementSection,
            recordingSection,
            outputSection,
            generalSection,
            busyMessageLabel,
            validationMessageLabel
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setContentCompressionResistancePriority(.required, for: .vertical)
        stack.setContentHuggingPriority(.required, for: .vertical)

        for view in [shortcutSection, recognitionSection, enhancementSection, recordingSection, outputSection, generalSection,
                     busyMessageLabel, validationMessageLabel] {
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }

        let contentView = NSView()
        contentView.addSubview(stack)
        contentView.addSubview(buttonRow)
        window.contentView = contentView
        window.defaultButtonCell = saveButton.cell as? NSButtonCell
        contentStack = stack
        footerStack = buttonRow
        registerManagedArrangedViews([
            microphoneMessageLabel,
            launchAtLoginMessageLabel,
            busyMessageLabel,
            validationMessageLabel
        ])

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(
                equalTo: contentView.leadingAnchor,
                constant: Layout.horizontalInset
            ),
            stack.trailingAnchor.constraint(
                equalTo: contentView.trailingAnchor,
                constant: -Layout.horizontalInset
            ),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor, constant: Layout.topInset),
            stack.bottomAnchor.constraint(
                lessThanOrEqualTo: buttonRow.topAnchor,
                constant: -Layout.footerSpacing
            ),
            buttonRow.leadingAnchor.constraint(
                equalTo: contentView.leadingAnchor,
                constant: Layout.horizontalInset
            ),
            buttonRow.trailingAnchor.constraint(
                equalTo: contentView.trailingAnchor,
                constant: -Layout.horizontalInset
            ),
            buttonRow.bottomAnchor.constraint(
                equalTo: contentView.bottomAnchor,
                constant: -Layout.bottomInset
            )
        ])
    }

    private func configurePopup(
        _ popup: NSPopUpButton,
        label: String,
        identifier: String,
        action: Selector
    ) {
        popup.target = self
        popup.action = action
        popup.identifier = NSUserInterfaceItemIdentifier(identifier)
        popup.setAccessibilityLabel(label)
        popup.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    }

    private func configureButton(
        _ button: NSButton,
        title: String,
        label: String,
        identifier: String,
        action: Selector
    ) {
        button.title = title
        button.bezelStyle = .rounded
        button.target = self
        button.action = action
        button.identifier = NSUserInterfaceItemIdentifier(identifier)
        button.setAccessibilityLabel(label)
    }

    private func configureCheckbox(
        _ checkbox: NSButton,
        title: String,
        label: String,
        identifier: String,
        action: Selector
    ) {
        checkbox.setButtonType(.switch)
        configureButton(
            checkbox,
            title: title,
            label: label,
            identifier: identifier,
            action: action
        )
    }

    private func makeSection(
        title: String,
        views: [NSView],
        minimumHeight: CGFloat? = nil
    ) -> NSBox {
        let box = NSBox()
        box.title = title
        box.titlePosition = .atTop
        box.boxType = .primary
        box.contentViewMargins = NSSize(width: 14, height: 12)
        box.setContentCompressionResistancePriority(.required, for: .vertical)
        if let minimumHeight {
            box.heightAnchor.constraint(greaterThanOrEqualToConstant: minimumHeight).isActive = true
        }

        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 9
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setContentCompressionResistancePriority(.required, for: .vertical)

        guard let contentView = box.contentView else {
            return box
        }
        contentView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor),
            stack.bottomAnchor.constraint(equalTo: contentView.bottomAnchor)
        ])
        for view in views {
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        return box
    }

    private func makeLabeledRow(title: String, control: NSView) -> NSStackView {
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.setContentHuggingPriority(.required, for: .horizontal)
        label.widthAnchor.constraint(equalToConstant: 108).isActive = true
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let row = NSStackView(views: [label, control])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        return row
    }

    private func makeControlRow(_ views: NSView...) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        if let first = views.first {
            first.setContentHuggingPriority(.defaultLow, for: .horizontal)
        }
        return row
    }

    private func render() {
        renderHotkeys()
        renderRecognition()
        renderEnhancement()
        renderRecording()
        renderOutput()
        if editorState.launchAtLoginSelectionIsIndeterminate {
            launchAtLoginCheckbox.state = .mixed
        } else {
            launchAtLoginCheckbox.state = editorState.draftLaunchAtLoginEnabled ? .on : .off
        }
        if let issue = editorState.launchAtLoginStatusIssue {
            launchAtLoginMessageLabel.stringValue =
                "Launch at Login status could not be read. Choose On or Off to replace it. \(issue)"
            setArrangedView(launchAtLoginMessageLabel, visible: true)
        } else {
            launchAtLoginMessageLabel.stringValue = ""
            setArrangedView(launchAtLoginMessageLabel, visible: false)
        }
        refreshEnabledState()
    }

    private func setArrangedView(_ view: NSView, visible: Bool) {
        let identifier = ObjectIdentifier(view)
        var context = managedVisibilityContexts[identifier]
        if context == nil,
           let stack = view.superview as? NSStackView,
           let index = stack.arrangedSubviews.firstIndex(where: { $0 === view }) {
            context = (stack, index)
            managedVisibilityContexts[identifier] = context
        }
        if let context {
            let isArranged = context.stack.arrangedSubviews.contains(where: { $0 === view })
            if visible, !isArranged {
                context.stack.insertArrangedSubview(
                    view,
                    at: min(context.index, context.stack.arrangedSubviews.count)
                )
            } else if !visible, isArranged {
                context.stack.removeArrangedSubview(view)
            }
        }
        view.isHidden = !visible
    }

    private func registerManagedArrangedViews(_ views: [NSView]) {
        for view in views {
            guard let stack = view.superview as? NSStackView,
                  let index = stack.arrangedSubviews.firstIndex(where: { $0 === view }) else {
                continue
            }
            managedVisibilityContexts[ObjectIdentifier(view)] = (stack, index)
        }
    }

    private func renderHotkeys() {
        let settings = editorState.draftSettings

        primaryHotkeyChoices = HotkeyShortcut.presets
        if !primaryHotkeyChoices.contains(settings.hotkey) {
            primaryHotkeyChoices.append(settings.hotkey)
        }
        primaryHotkeyPopup.removeAllItems()
        for shortcut in primaryHotkeyChoices {
            let isPreset = HotkeyShortcut.presets.contains(shortcut)
            primaryHotkeyPopup.addItem(withTitle: isPreset ? shortcut.label : "Custom — \(shortcut.label)")
        }
        if let index = primaryHotkeyChoices.firstIndex(of: settings.hotkey) {
            primaryHotkeyPopup.selectItem(at: index)
        }

        submitHotkeyChoices = [nil] + HotkeyShortcut.submitPresets.map(Optional.some)
        if let current = settings.submitHotkey,
           !submitHotkeyChoices.contains(where: { $0 == current }) {
            submitHotkeyChoices.append(current)
        }
        submitHotkeyPopup.removeAllItems()
        for shortcut in submitHotkeyChoices {
            if let shortcut {
                let isPreset = HotkeyShortcut.submitPresets.contains(shortcut)
                submitHotkeyPopup.addItem(
                    withTitle: isPreset ? shortcut.label : "Custom — \(shortcut.label)"
                )
            } else {
                submitHotkeyPopup.addItem(withTitle: "Off")
            }
        }
        if let index = submitHotkeyChoices.firstIndex(where: { $0 == settings.submitHotkey }) {
            submitHotkeyPopup.selectItem(at: index)
        }
    }

    private func renderRecognition() {
        let settings = editorState.draftSettings

        engineChoices = registry.engines.map(\.id)
        enginePopup.removeAllItems()
        for engine in registry.engines {
            enginePopup.addItem(withTitle: engine.label)
        }
        if let index = engineChoices.firstIndex(of: settings.engine) {
            enginePopup.selectItem(at: index)
        }

        let engine = registry.engine(settings.engine)
        modelChoices = engine.models.map(\.id)
        modelPopup.removeAllItems()
        for model in engine.models {
            modelPopup.addItem(withTitle: model.label)
            modelPopup.lastItem?.toolTip = model.id
        }
        let selectedModel = registry.validModel(
            settings.lastModelByEngine[engine.id],
            for: engine.id
        )
        if let index = modelChoices.firstIndex(of: selectedModel) {
            modelPopup.selectItem(at: index)
        }

        let languages = registry.supportedLanguages(for: engine.id)
        languageChoices = languages.map(\.id)
        languagePopup.removeAllItems()
        for language in languages {
            languagePopup.addItem(withTitle: language.label)
        }
        if let index = languageChoices.firstIndex(of: settings.language) {
            languagePopup.selectItem(at: index)
        }
    }

    private func renderEnhancement() {
        let selection = editorState.draftSettings.enhancement
        let sortedProfiles = enhancementCatalog.profiles.values.sorted {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        profileChoices = [nil] + sortedProfiles.map(\.id)
        if let selectedID = selection.profileID,
           enhancementCatalog.profiles[selectedID] == nil {
            profileChoices.append(selectedID)
        }
        profilePopup.removeAllItems()
        for profileID in profileChoices {
            guard let profileID else {
                profilePopup.addItem(withTitle: "Off")
                continue
            }
            if let profile = enhancementCatalog.profiles[profileID] {
                profilePopup.addItem(withTitle: profile.name)
                profilePopup.lastItem?.toolTip = profile.id
            } else {
                profilePopup.addItem(withTitle: "Unavailable — \(profileID)")
                profilePopup.lastItem?.toolTip = profileID
            }
        }
        if let index = profileChoices.firstIndex(of: selection.profileID) {
            profilePopup.selectItem(at: index)
        }

        postprocessingChoices = [.off, .dictionary]
        let sortedPostprocessors = enhancementCatalog.postprocessors.values.sorted {
            $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
        postprocessingChoices.append(contentsOf: sortedPostprocessors.map { .preset($0.id) })
        if let presetID = selection.postprocessing.presetID,
           enhancementCatalog.postprocessors[presetID] == nil {
            postprocessingChoices.append(.preset(presetID))
        }
        postprocessingPopup.removeAllItems()
        for choice in postprocessingChoices {
            switch choice {
            case .off:
                postprocessingPopup.addItem(withTitle: "Off")
            case .dictionary:
                postprocessingPopup.addItem(withTitle: "Dictionary Replacement")
            case .preset(let presetID):
                if let preset = enhancementCatalog.postprocessors[presetID] {
                    postprocessingPopup.addItem(withTitle: preset.displayName)
                    postprocessingPopup.lastItem?.toolTip = preset.id
                } else {
                    postprocessingPopup.addItem(withTitle: "Unavailable — \(presetID)")
                    postprocessingPopup.lastItem?.toolTip = presetID
                }
            }
        }
        if let index = postprocessingChoices.firstIndex(of: selection.postprocessing) {
            postprocessingPopup.selectItem(at: index)
        }

        enhancementMessageLabel.stringValue = enhancementMessage(
            for: selection,
            engine: editorState.draftSettings.engine
        )
        enhancementMessageLabel.textColor = enhancementMessageColor(for: selection)
    }

    private func enhancementMessage(
        for selection: EnhancementSelection,
        engine: String
    ) -> String {
        var messages: [String] = []
        if selection.profileID != nil, engine != "mlx-whisper" {
            messages.append(
                "This recognition engine does not support profile hints. Dictionary replacement applies only when Dictionary Replacement or a CLI preset is selected."
            )
        }
        switch selection.postprocessing {
        case .off, .dictionary:
            messages.append("Data destination: Local. No transcript is sent by post-processing.")
        case .preset(let id):
            guard let preset = enhancementCatalog.postprocessors[id] else {
                messages.append(
                    "The selected post-processor is unavailable. Dictionary replacement will be used as a safe fallback."
                )
                break
            }
            switch preset.destination {
            case .local:
                messages.append("Data destination: Local process.")
            case .remote:
                messages.append(
                    "Data destination: Remote service. The transcript, recognition language, profile name, profile context, and terms are passed to this CLI and may leave this Mac."
                )
            case .unknown:
                messages.append(
                    "Data destination: Unknown. The transcript, recognition language, profile name, profile context, and terms are passed to this CLI and may leave this Mac. Review the command before continuing."
                )
            }
            if enhancementCatalog.localPostprocessorIDs.contains(id) {
                messages.append("Definition: Local setting.")
            } else {
                messages.append(
                    "Definition: Bundled default. Edit saves a local override."
                )
            }
            if preset.destination != .local,
               selection.approvedPostprocessorRevision != preset.reviewRevision {
                messages.append(
                    "Post-processing inactive: review and save this command "
                        + "revision before it can run. Dictionary Replacement is "
                        + "being used. Choose “Review & Save…” below."
                )
            }
            messages.append("Automatic Enter is disabled whenever CLI post-processing is selected.")
        }
        return messages.joined(separator: " ")
    }

    private func enhancementMessageColor(
        for selection: EnhancementSelection
    ) -> NSColor {
        guard let presetID = selection.postprocessing.presetID,
              let preset = enhancementCatalog.postprocessors[presetID] else {
            return selection.postprocessing.isCLI ? .systemOrange : .secondaryLabelColor
        }
        return preset.destination == .local ? .secondaryLabelColor : .systemOrange
    }

    private func reconcileDraftPostprocessorApproval() {
        let selection = editorState.draftSettings.enhancement
        guard selection.approvedPostprocessorRevision != nil else {
            return
        }
        let currentRevision: String?
        if let presetID = selection.postprocessing.presetID,
           let preset = enhancementCatalog.postprocessors[presetID],
           preset.destination != .local {
            currentRevision = preset.reviewRevision
        } else {
            currentRevision = nil
        }
        guard selection.approvedPostprocessorRevision != currentRevision else {
            return
        }
        // Preserve the stale baseline value. Clearing only the draft marks
        // Settings dirty and gives the user a one-click review-and-save path.
        editorState.draftSettings.enhancement
            .approvedPostprocessorRevision = nil
    }

    private func renderRecording() {
        let settings = editorState.draftSettings
        silenceAutoStopCheckbox.state = settings.silenceAutoStopEnabled ? .on : .off

        microphonePopup.removeAllItems()
        microphonePopup.addItem(withTitle: "System Default")
        microphonePopup.lastItem?.representedObject = nil

        for device in audioInputDevices {
            microphonePopup.addItem(withTitle: device.name)
            microphonePopup.lastItem?.representedObject = device.uid
            microphonePopup.lastItem?.toolTip = device.uid
        }

        let selectedUID = settings.microphoneDeviceUID
        if let selectedUID,
           !audioInputDevices.contains(where: { $0.uid == selectedUID }) {
            microphonePopup.addItem(withTitle: "Unavailable — \(abbreviatedUID(selectedUID))")
            microphonePopup.lastItem?.representedObject = selectedUID
            microphonePopup.lastItem?.toolTip = selectedUID
            microphoneMessageLabel.stringValue =
                "The saved microphone is unavailable. Its full device identifier is preserved."
            setArrangedView(microphoneMessageLabel, visible: true)
        } else {
            microphoneMessageLabel.stringValue = ""
            setArrangedView(microphoneMessageLabel, visible: false)
        }

        if let selectedUID,
           let item = microphonePopup.itemArray.first(where: {
               ($0.representedObject as? String) == selectedUID
           }) {
            microphonePopup.select(item)
        } else {
            microphonePopup.selectItem(at: 0)
        }
    }

    private func renderOutput() {
        let settings = editorState.draftSettings
        outputModePopup.removeAllItems()
        for mode in OutputMode.allCases {
            outputModePopup.addItem(withTitle: mode.label)
            outputModePopup.lastItem?.representedObject = mode.rawValue
        }
        if let item = outputModePopup.itemArray.first(where: {
            ($0.representedObject as? String) == settings.outputMode.rawValue
        }) {
            outputModePopup.select(item)
        }
    }

    private func refreshEnabledState() {
        window?.isDocumentEdited = editorState.isDirty
        let runtimeControlsEnabled = !isBusy && !isSaving
        for control in [
            primaryHotkeyPopup,
            submitHotkeyPopup,
            enginePopup,
            modelPopup,
            languagePopup,
            profilePopup,
            postprocessingPopup,
            silenceAutoStopCheckbox,
            microphonePopup,
            outputModePopup
        ] {
            control.isEnabled = runtimeControlsEnabled
        }
        recordPrimaryHotkeyButton.isEnabled =
            runtimeControlsEnabled && onRecordPrimaryHotkey != nil
        recordSubmitHotkeyButton.isEnabled =
            runtimeControlsEnabled && onRecordSubmitHotkey != nil
        newProfileButton.isEnabled =
            runtimeControlsEnabled && onSaveProfile != nil
        editProfileButton.isEnabled =
            runtimeControlsEnabled
            && onSaveProfile != nil
            && editorState.draftSettings.enhancement.profileID.flatMap {
                enhancementCatalog.profiles[$0]
            } != nil
        newPostprocessorButton.isEnabled =
            runtimeControlsEnabled && onSavePostprocessor != nil
        editPostprocessorButton.isEnabled =
            runtimeControlsEnabled
            && onSavePostprocessor != nil
            && editorState.draftSettings.enhancement.postprocessing.presetID
                .flatMap { enhancementCatalog.postprocessors[$0] } != nil
        launchAtLoginCheckbox.isEnabled = !isSaving
        cancelButton.isEnabled = !isSaving

        setArrangedView(busyMessageLabel, visible: isBusy)
        if let issue = editorState.validationIssue {
            validationMessageLabel.stringValue = issue.message
            setArrangedView(validationMessageLabel, visible: true)
        } else if !enhancementConfigurationFitsBudget {
            validationMessageLabel.stringValue =
                "The selected profile and post-processor are too large to use together. Reduce profile terms or context, or shorten the CLI arguments and environment."
            setArrangedView(validationMessageLabel, visible: true)
        } else if hasSaveError {
            setArrangedView(validationMessageLabel, visible: true)
        } else {
            validationMessageLabel.stringValue = ""
            setArrangedView(validationMessageLabel, visible: false)
        }

        saveButton.isEnabled =
            !isSaving
            && onSave != nil
            && canSaveCurrentDraft
            && enhancementConfigurationFitsBudget
        if requiresPostprocessorConsent {
            saveButton.title = "Review & Save…"
            saveButton.setAccessibilityLabel(
                "Review post-processing data sharing and save settings"
            )
        } else {
            saveButton.title = "Save"
            saveButton.setAccessibilityLabel("Save settings")
        }
        updateWindowSizeConstraints()
    }

    private var requiresPostprocessorConsent: Bool {
        let selection = editorState.draftSettings.enhancement
        guard let presetID = selection.postprocessing.presetID,
              let preset = enhancementCatalog.postprocessors[presetID],
              preset.destination != .local else {
            return false
        }
        return selection.approvedPostprocessorRevision != preset.reviewRevision
    }

    private var canSaveCurrentDraft: Bool {
        guard editorState.validationIssue == nil else {
            return false
        }
        if requiresPostprocessorConsent {
            return !isBusy
        }
        return editorState.canSave(isBusy: isBusy)
    }

    private var enhancementConfigurationFitsBudget: Bool {
        let selection = editorState.draftSettings.enhancement
        let profile = selection.profileID.flatMap {
            enhancementCatalog.profiles[$0]
        }
        switch selection.postprocessing {
        case .off:
            return EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: nil
            )
        case .dictionary:
            return EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: nil,
                includesDictionaryPostprocessor: true
            )
        case .preset(let id):
            if let preset = enhancementCatalog.postprocessors[id] {
                return EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                    profile: profile,
                    preset: preset
                )
            }
            return EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: nil,
                includesDictionaryPostprocessor: true
            )
        }
    }

    private func updateWindowSizeConstraints() {
        guard !isUpdatingWindowLayout,
              let window,
              let contentView = window.contentView,
              let contentStack,
              let footerStack else {
            return
        }
        isUpdatingWindowLayout = true
        defer { isUpdatingWindowLayout = false }

        contentView.layoutSubtreeIfNeeded()
        let requiredHeight = ceil(
            Layout.topInset
                + contentStack.fittingSize.height
                + Layout.footerSpacing
                + footerStack.fittingSize.height
                + Layout.bottomInset
        )
        let minimumHeight = max(Layout.minimumContentSize.height, requiredHeight)
        window.contentMinSize = NSSize(
            width: Layout.minimumContentSize.width,
            height: minimumHeight
        )

        let currentSize = window.contentLayoutRect.size
        guard minimumHeight > currentSize.height + 1 else {
            return
        }
        window.setContentSize(NSSize(width: currentSize.width, height: minimumHeight))
    }

    private func clearSaveError() {
        if hasSaveError {
            validationMessageLabel.stringValue = ""
        }
    }

    private var hasSaveError: Bool {
        validationMessageLabel.stringValue.hasPrefix("Could not save")
            || validationMessageLabel.stringValue.hasPrefix("Some settings were saved")
    }

    private func abbreviatedUID(_ uid: String) -> String {
        let maximumLength = 42
        guard uid.count > maximumLength else {
            return uid
        }
        let prefix = uid.prefix(22)
        let suffix = uid.suffix(14)
        return "\(prefix)…\(suffix)"
    }

    @objc private func primaryHotkeyChanged() {
        guard primaryHotkeyChoices.indices.contains(primaryHotkeyPopup.indexOfSelectedItem) else {
            return
        }
        editorState.draftSettings.hotkey =
            primaryHotkeyChoices[primaryHotkeyPopup.indexOfSelectedItem]
        didEdit()
    }

    @objc private func submitHotkeyChanged() {
        guard submitHotkeyChoices.indices.contains(submitHotkeyPopup.indexOfSelectedItem) else {
            return
        }
        editorState.draftSettings.submitHotkey =
            submitHotkeyChoices[submitHotkeyPopup.indexOfSelectedItem]
        didEdit()
    }

    @objc private func recordPrimaryHotkey() {
        guard let onRecordPrimaryHotkey else {
            return
        }
        let generation = hotkeyRecorderSession.begin()
        onRecordPrimaryHotkey(editorState.draftSettings.hotkey) { [weak self] shortcut in
            Task { @MainActor [weak self] in
                guard let self,
                      self.hotkeyRecorderSession.finish(generation: generation),
                      let shortcut else {
                    return
                }
                self.editorState.draftSettings.hotkey = shortcut
                self.didEdit()
            }
        }
    }

    @objc private func recordSubmitHotkey() {
        guard let onRecordSubmitHotkey else {
            return
        }
        let generation = hotkeyRecorderSession.begin()
        onRecordSubmitHotkey(editorState.draftSettings.submitHotkey) { [weak self] shortcut in
            Task { @MainActor [weak self] in
                guard let self,
                      self.hotkeyRecorderSession.finish(generation: generation),
                      let shortcut else {
                    return
                }
                self.editorState.draftSettings.submitHotkey = shortcut
                self.didEdit()
            }
        }
    }

    @objc private func engineChanged() {
        guard engineChoices.indices.contains(enginePopup.indexOfSelectedItem) else {
            return
        }
        let engine = engineChoices[enginePopup.indexOfSelectedItem]
        editorState.draftSettings.engine = engine
        editorState.draftSettings.language =
            registry.validLanguage(editorState.draftSettings.language, for: engine)
        didEdit()
    }

    @objc private func modelChanged() {
        guard modelChoices.indices.contains(modelPopup.indexOfSelectedItem) else {
            return
        }
        let engine = registry.validEngine(editorState.draftSettings.engine)
        editorState.draftSettings.lastModelByEngine[engine] =
            modelChoices[modelPopup.indexOfSelectedItem]
        didEdit()
    }

    @objc private func languageChanged() {
        guard languageChoices.indices.contains(languagePopup.indexOfSelectedItem) else {
            return
        }
        editorState.draftSettings.language =
            languageChoices[languagePopup.indexOfSelectedItem]
        didEdit()
    }

    @objc private func profileChanged() {
        guard profileChoices.indices.contains(profilePopup.indexOfSelectedItem) else {
            return
        }
        editorState.draftSettings.enhancement.profileID =
            profileChoices[profilePopup.indexOfSelectedItem]
        didEdit()
    }

    @objc private func postprocessingChanged() {
        guard postprocessingChoices.indices.contains(postprocessingPopup.indexOfSelectedItem) else {
            return
        }
        let newSelection =
            postprocessingChoices[postprocessingPopup.indexOfSelectedItem]
        if newSelection != editorState.draftSettings.enhancement.postprocessing {
            editorState.draftSettings.enhancement.postprocessing = newSelection
            editorState.draftSettings.enhancement.approvedPostprocessorRevision = nil
        }
        didEdit()
    }

    @objc private func createProfile() {
        presentProfileEditor(profile: nil, expectedFingerprint: nil)
    }

    @objc private func editProfile() {
        guard let profileID = editorState.draftSettings.enhancement.profileID,
              let profile = enhancementCatalog.profiles[profileID] else {
            return
        }
        presentProfileEditor(
            profile: profile,
            expectedFingerprint: enhancementCatalog.fingerprints.profiles[profileID]
        )
    }

    private func presentProfileEditor(
        profile: EnhancementProfile?,
        expectedFingerprint: EnhancementFileFingerprint?
    ) {
        guard profileEditorWindowController == nil,
              let onSaveProfile,
              let parentWindow = window else {
            return
        }
        let editor = ProfileEditorWindowController(
            profile: profile,
            expectedFingerprint: expectedFingerprint
        )
        editor.onSave = onSaveProfile
        editor.onSaved = { [weak self] catalog, profileID in
            guard let self else {
                return
            }
            self.enhancementCatalog = catalog
            self.editorState.draftSettings.enhancement.profileID = profileID
            self.didEdit()
        }
        editor.onDismiss = { [weak self, weak editor] in
            guard let self else {
                return
            }
            if let editorWindow = editor?.window,
               parentWindow.attachedSheet === editorWindow {
                parentWindow.endSheet(editorWindow)
            }
            self.profileEditorWindowController = nil
        }
        profileEditorWindowController = editor
        if let editorWindow = editor.window {
            parentWindow.beginSheet(editorWindow)
        }
    }

    @objc private func createPostprocessor() {
        presentPostprocessorEditor(preset: nil)
    }

    @objc private func editPostprocessor() {
        guard let presetID =
                editorState.draftSettings.enhancement.postprocessing.presetID,
              let preset = enhancementCatalog.postprocessors[presetID] else {
            return
        }
        presentPostprocessorEditor(preset: preset)
    }

    private func presentPostprocessorEditor(
        preset: EnhancementPostprocessorPreset?
    ) {
        guard postprocessorEditorWindowController == nil,
              let onSavePostprocessor,
              let parentWindow = window else {
            return
        }
        let editor = PostprocessorEditorWindowController(
            preset: preset,
            expectedFingerprint:
                enhancementCatalog.fingerprints.postprocessors,
            existingPresetIDs:
                Set(enhancementCatalog.postprocessors.keys)
                .union(enhancementCatalog.localPostprocessorIDs)
                .union(enhancementCatalog.blockedPostprocessorIDs)
        )
        editor.onSave = onSavePostprocessor
        editor.onSaved = { [weak self] catalog, presetID in
            guard let self else {
                return
            }
            self.enhancementCatalog = catalog
            if self.editorState.draftSettings.enhancement.postprocessing
                == .preset(presetID) {
                self.editorState.draftSettings.enhancement
                    .approvedPostprocessorRevision = nil
            }
            self.didEdit()
        }
        editor.onDismiss = { [weak self, weak editor] in
            guard let self else {
                return
            }
            if let editorWindow = editor?.window,
               parentWindow.attachedSheet === editorWindow {
                parentWindow.endSheet(editorWindow)
            }
            self.postprocessorEditorWindowController = nil
        }
        postprocessorEditorWindowController = editor
        if let editorWindow = editor.window {
            parentWindow.beginSheet(editorWindow)
        }
    }

    @objc private func silenceAutoStopChanged() {
        editorState.draftSettings.silenceAutoStopEnabled =
            silenceAutoStopCheckbox.state == .on
        didEdit()
    }

    @objc private func microphoneChanged() {
        editorState.draftSettings.microphoneDeviceUID =
            microphonePopup.selectedItem?.representedObject as? String
        didEdit()
    }

    @objc private func outputModeChanged() {
        guard let rawValue = outputModePopup.selectedItem?.representedObject as? String,
              let mode = OutputMode(rawValue: rawValue) else {
            return
        }
        editorState.draftSettings.outputMode = mode
        didEdit()
    }

    @objc private func launchAtLoginChanged() {
        editorState.setDraftLaunchAtLoginEnabled(launchAtLoginCheckbox.state == .on)
        didEdit()
    }

    private func didEdit() {
        clearSaveError()
        render()
    }

    @objc private func cancel() {
        guard !isSaving else {
            return
        }
        cancelActiveHotkeyRecording()
        editorState.cancel()
        clearSaveError()
        render()
        allowConfirmedClose = true
        close()
    }

    @objc private func save() {
        guard !isSaving,
              canSaveCurrentDraft,
              enhancementConfigurationFitsBudget,
              let onSave,
              confirmExternalDestinationIfNeeded() else {
            return
        }
        editorState.normalizeDraft()
        let request = SettingsSaveRequest(
            settings: editorState.draftSettings,
            launchAtLoginEnabled: editorState.draftLaunchAtLoginEnabled,
            runtimeSettingsChanged: editorState.runtimeSettingsChanged,
            launchAtLoginChanged: editorState.launchAtLoginChanged
        )

        isSaving = true
        clearSaveError()
        refreshEnabledState()
        let result = onSave(request)
        isSaving = false

        switch result {
        case .success(let authoritative):
            editorState.synchronize(
                authoritativeSettings: authoritative.settings,
                launchAtLoginStatus: authoritative.launchAtLoginStatus
            )
            clearSaveError()
        case .partial(let authoritative, let error):
            editorState.synchronize(
                authoritativeSettings: authoritative.settings,
                launchAtLoginStatus: authoritative.launchAtLoginStatus
            )
            validationMessageLabel.stringValue =
                "Some settings were saved, but Launch at Login could not be updated: \(error.localizedDescription)"
        case .failure(let error):
            validationMessageLabel.stringValue =
                "Could not save settings: \(error.localizedDescription)"
        }
        render()
    }

    private func confirmExternalDestinationIfNeeded() -> Bool {
        guard let presetID = editorState.draftSettings.enhancement.postprocessing.presetID,
              let preset = enhancementCatalog.postprocessors[presetID],
              preset.destination != .local else {
            return true
        }
        let revision = preset.reviewRevision
        if editorState.draftSettings.enhancement.approvedPostprocessorRevision
            == revision {
            return true
        }
        guard let informativeText = Self.externalDestinationConsentText(
            for: preset
        ) else {
            return true
        }
        let confirmed: Bool
        if let onConfirmExternalDestination {
            confirmed = onConfirmExternalDestination(preset, informativeText)
        } else {
            let alert = NSAlert()
            alert.alertStyle = .warning
            switch preset.destination {
            case .remote:
                alert.messageText = "Allow Remote Post-Processing?"
            case .unknown:
                alert.messageText = "Allow Post-Processing With an Unknown Destination?"
            case .local:
                return true
            }
            alert.informativeText = informativeText
            alert.addButton(withTitle: "Allow and Save")
            alert.addButton(withTitle: "Cancel")
            confirmed = alert.runModal() == .alertFirstButtonReturn
        }
        guard confirmed else {
            return false
        }
        editorState.draftSettings.enhancement.approvedPostprocessorRevision =
            revision
        return true
    }

    static func externalDestinationConsentText(
        for preset: EnhancementPostprocessorPreset
    ) -> String? {
        let dataNotice =
            "The transcript, recognition language, profile name, profile context, and terms are passed to this CLI and may leave this Mac."
        switch preset.destination {
        case .remote:
            return "\"\(preset.displayName)\" uses a remote service. \(dataNotice)"
        case .unknown:
            return "The destination for \"\(preset.displayName)\" could not be verified. \(dataNotice) Review its local configuration before continuing."
        case .local:
            return nil
        }
    }

    private func confirmDiscardBeforeClosing() {
        guard !isConfirmingClose, let window else {
            return
        }
        isConfirmingClose = true
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Discard Unsaved Changes?"
        alert.informativeText = "Your changes have not been saved."
        alert.addButton(withTitle: "Keep Editing")
        alert.addButton(withTitle: "Discard Changes")
        alert.beginSheetModal(for: window) { [weak self] response in
            Task { @MainActor [weak self] in
                guard let self else {
                    return
                }
                self.isConfirmingClose = false
                guard response == .alertSecondButtonReturn else {
                    return
                }
                self.cancelActiveHotkeyRecording()
                self.editorState.cancel()
                self.clearSaveError()
                self.render()
                self.allowConfirmedClose = true
                self.close()
            }
        }
    }

    private func replaceAudioInputDevices(_ devices: [AudioInputDevice]) {
        audioInputDevices = devices.sorted {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }

    private func refreshAudioInputDevices() {
        guard let onRequestAudioInputDevices else {
            return
        }
        replaceAudioInputDevices(onRequestAudioInputDevices())
    }

    private func cancelActiveHotkeyRecording() {
        guard hotkeyRecorderSession.cancel() else {
            return
        }
        onCancelHotkeyRecording?()
    }
}
