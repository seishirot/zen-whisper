import AppKit
import Foundation

@MainActor
final class ProfileEditorWindowController: NSWindowController,
    NSWindowDelegate,
    NSTableViewDataSource,
    NSTableViewDelegate,
    NSTextFieldDelegate,
    NSTextViewDelegate
{
    typealias SaveHandler = (
        EnhancementProfile,
        EnhancementFileFingerprint?
    ) -> Result<EnhancementCatalogSnapshot, Error>

    var onSave: SaveHandler?
    var onSaved: ((EnhancementCatalogSnapshot, String) -> Void)?
    var onDismiss: (() -> Void)?

    enum AccessibilityIdentifier {
        static let name = "profileEditor.name"
        static let context = "profileEditor.context"
        static let terms = "profileEditor.terms"
        static let canonical = "profileEditor.canonical"
        static let spoken = "profileEditor.spoken"
        static let replaceFrom = "profileEditor.replaceFrom"
        static let description = "profileEditor.description"
        static let addTerm = "profileEditor.addTerm"
        static let removeTerm = "profileEditor.removeTerm"
        static let message = "profileEditor.message"
        static let cancel = "profileEditor.cancel"
        static let save = "profileEditor.save"
    }

    private enum Layout {
        static let contentSize = NSSize(width: 720, height: 650)
        static let inset: CGFloat = 20
    }

    private enum Column: String {
        case canonical
        case description
    }

    private let profileID: String
    private let expectedFingerprint: EnhancementFileFingerprint?
    private let profileExtraFields: [String: JSONValue]
    private var terms: [EnhancementTerm]
    private var baselineProfile: EnhancementProfile
    private var didDismiss = false
    private var isRenderingSelection = false
    private var isSaving = false

    private let nameField = NSTextField()
    private let contextTextView = NSTextView()
    private let termsTable = NSTableView()
    private let canonicalField = NSTextField()
    private let spokenTextView = NSTextView()
    private let replaceFromTextView = NSTextView()
    private let descriptionField = NSTextField()
    private let addTermButton = NSButton()
    private let removeTermButton = NSButton()
    private let messageLabel = NSTextField(wrappingLabelWithString: "")
    private let cancelButton = NSButton()
    private let saveButton = NSButton()

    init(
        profile: EnhancementProfile?,
        expectedFingerprint: EnhancementFileFingerprint?
    ) {
        let initialProfile = profile ?? EnhancementProfile(
            id: "profile-\(UUID().uuidString.lowercased())",
            name: "New Profile"
        )
        profileID = initialProfile.id
        self.expectedFingerprint = expectedFingerprint
        profileExtraFields = initialProfile.extraFields
        terms = initialProfile.terms
        baselineProfile = initialProfile

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: Layout.contentSize),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = profile == nil ? "New Recognition Profile" : "Edit Recognition Profile"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 620, height: 590)

        super.init(window: window)
        window.delegate = self
        configureControls()
        configureContentView()
        renderProfile(initialProfile)
    }

    required init?(coder: NSCoder) {
        nil
    }

    var isDirty: Bool {
        currentProfile() != baselineProfile
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard !isSaving, isDirty else {
            return !isSaving
        }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Discard profile changes?"
        alert.informativeText = "Your unsaved profile changes will be lost."
        alert.addButton(withTitle: "Discard")
        alert.addButton(withTitle: "Keep Editing")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func windowWillClose(_ notification: Notification) {
        dismissOnce()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        terms.count
    }

    func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        guard terms.indices.contains(row),
              let column = tableColumn,
              let columnID = Column(rawValue: column.identifier.rawValue) else {
            return nil
        }
        let identifier = NSUserInterfaceItemIdentifier("profile.term.\(columnID.rawValue)")
        let field: NSTextField
        if let reused = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTextField {
            field = reused
        } else {
            field = NSTextField(labelWithString: "")
            field.identifier = identifier
            field.lineBreakMode = .byTruncatingTail
        }
        switch columnID {
        case .canonical:
            field.stringValue = terms[row].canonical.isEmpty
                ? "Untitled term"
                : terms[row].canonical
        case .description:
            field.stringValue = terms[row].description
        }
        return field
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        renderSelectedTerm()
    }

    func controlTextDidChange(_ notification: Notification) {
        guard !isRenderingSelection else {
            return
        }
        clearMessage()
        if notification.object as? NSTextField === nameField {
            updateDocumentEditedState()
            return
        }
        guard let index = selectedTermIndex else {
            return
        }
        if notification.object as? NSTextField === canonicalField {
            terms[index].canonical = canonicalField.stringValue
        } else if notification.object as? NSTextField === descriptionField {
            terms[index].description = descriptionField.stringValue
        }
        termsTable.reloadData(forRowIndexes: IndexSet(integer: index), columnIndexes: [0, 1])
        updateDocumentEditedState()
    }

    func textDidChange(_ notification: Notification) {
        guard !isRenderingSelection else {
            return
        }
        clearMessage()
        if notification.object as? NSTextView === contextTextView {
            updateDocumentEditedState()
            return
        }
        guard let index = selectedTermIndex else {
            return
        }
        if notification.object as? NSTextView === spokenTextView {
            terms[index].spoken = nonemptyLines(spokenTextView.string)
        } else if notification.object as? NSTextView === replaceFromTextView {
            terms[index].replaceFrom = nonemptyLines(replaceFromTextView.string)
        }
        updateDocumentEditedState()
    }

    private var selectedTermIndex: Int? {
        let row = termsTable.selectedRow
        return terms.indices.contains(row) ? row : nil
    }

    private func configureControls() {
        nameField.placeholderString = "Profile name"
        nameField.delegate = self
        nameField.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.name)
        nameField.setAccessibilityLabel("Profile name")

        contextTextView.isRichText = false
        contextTextView.isAutomaticQuoteSubstitutionEnabled = false
        contextTextView.font = .systemFont(ofSize: NSFont.systemFontSize)
        contextTextView.delegate = self
        contextTextView.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.context)
        contextTextView.setAccessibilityLabel("Recognition context")

        let canonicalColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(Column.canonical.rawValue))
        canonicalColumn.title = "Canonical term"
        canonicalColumn.width = 220
        let descriptionColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(Column.description.rawValue))
        descriptionColumn.title = "Description"
        descriptionColumn.width = 340
        termsTable.addTableColumn(canonicalColumn)
        termsTable.addTableColumn(descriptionColumn)
        termsTable.headerView = NSTableHeaderView()
        termsTable.usesAlternatingRowBackgroundColors = true
        termsTable.allowsMultipleSelection = false
        termsTable.dataSource = self
        termsTable.delegate = self
        termsTable.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.terms)
        termsTable.setAccessibilityLabel("Profile terms")

        canonicalField.placeholderString = "Required"
        canonicalField.delegate = self
        canonicalField.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.canonical)
        canonicalField.setAccessibilityLabel("Canonical term")
        descriptionField.placeholderString = "Optional recognition hint"
        descriptionField.delegate = self
        descriptionField.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.description)
        descriptionField.setAccessibilityLabel("Term description")
        for textView in [spokenTextView, replaceFromTextView] {
            textView.isRichText = false
            textView.isAutomaticQuoteSubstitutionEnabled = false
            textView.font = .systemFont(ofSize: NSFont.systemFontSize)
            textView.delegate = self
        }
        spokenTextView.setAccessibilityLabel("Spoken aliases, one per line")
        spokenTextView.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.spoken)
        replaceFromTextView.setAccessibilityLabel("Replacement sources, one per line")
        replaceFromTextView.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.replaceFrom)

        configureButton(
            addTermButton,
            title: "Add Term",
            identifier: AccessibilityIdentifier.addTerm,
            action: #selector(addTerm)
        )
        configureButton(
            removeTermButton,
            title: "Remove Term",
            identifier: AccessibilityIdentifier.removeTerm,
            action: #selector(removeTerm)
        )
        configureButton(
            cancelButton,
            title: "Cancel",
            identifier: AccessibilityIdentifier.cancel,
            action: #selector(cancel)
        )
        cancelButton.keyEquivalent = "\u{1b}"
        configureButton(
            saveButton,
            title: "Save Profile",
            identifier: AccessibilityIdentifier.save,
            action: #selector(save)
        )
        saveButton.keyEquivalent = "\r"

        messageLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        messageLabel.textColor = .systemRed
        messageLabel.identifier = NSUserInterfaceItemIdentifier(AccessibilityIdentifier.message)
        messageLabel.setAccessibilityLabel("Profile validation message")
        messageLabel.isHidden = true
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
        let nameRow = makeLabeledRow(title: "Name:", control: nameField)
        let contextScroll = makeScrollView(contextTextView, minimumHeight: 78)
        let contextRow = makeLabeledRow(title: "Context:", control: contextScroll, alignment: .top)

        let tableScroll = NSScrollView()
        tableScroll.documentView = termsTable
        tableScroll.hasVerticalScroller = true
        tableScroll.borderType = .bezelBorder
        tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true

        let termButtonSpacer = NSView()
        termButtonSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let termButtons = NSStackView(views: [termButtonSpacer, addTermButton, removeTermButton])
        termButtons.orientation = .horizontal
        termButtons.alignment = .centerY
        termButtons.spacing = 8

        let termsLabel = NSTextField(labelWithString: "Terms")
        termsLabel.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        let termsStack = NSStackView(views: [termsLabel, tableScroll, termButtons])
        termsStack.orientation = .vertical
        termsStack.alignment = .leading
        termsStack.spacing = 8
        tableScroll.widthAnchor.constraint(equalTo: termsStack.widthAnchor).isActive = true
        termButtons.widthAnchor.constraint(equalTo: termsStack.widthAnchor).isActive = true

        let detailGrid = NSGridView(views: [
            [NSTextField(labelWithString: "Canonical:"), canonicalField],
            [
                NSTextField(labelWithString: "Spoken aliases:"),
                makeScrollView(spokenTextView, minimumHeight: 62)
            ],
            [
                NSTextField(labelWithString: "Replace from:"),
                makeScrollView(replaceFromTextView, minimumHeight: 62)
            ],
            [NSTextField(labelWithString: "Description:"), descriptionField]
        ])
        detailGrid.column(at: 0).xPlacement = .trailing
        detailGrid.column(at: 1).xPlacement = .fill
        detailGrid.rowSpacing = 8
        detailGrid.columnSpacing = 10
        for index in [1, 2] {
            detailGrid.cell(atColumnIndex: 0, rowIndex: index).contentView?
                .setContentHuggingPriority(.required, for: .horizontal)
        }

        let helpLabel = NSTextField(wrappingLabelWithString:
            "Spoken aliases improve recognition hints. Replace from values are literal, case-sensitive dictionary replacements. Enter one value per line."
        )
        helpLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        helpLabel.textColor = .secondaryLabelColor

        let buttonSpacer = NSView()
        buttonSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = NSStackView(views: [buttonSpacer, cancelButton, saveButton])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = 8

        let content = NSStackView(views: [
            nameRow,
            contextRow,
            termsStack,
            detailGrid,
            helpLabel,
            messageLabel,
            footer
        ])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.translatesAutoresizingMaskIntoConstraints = false
        for view in [nameRow, contextRow, termsStack, detailGrid, helpLabel, messageLabel, footer] {
            view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }

        let contentView = NSView()
        contentView.addSubview(content)
        window.contentView = contentView
        window.defaultButtonCell = saveButton.cell as? NSButtonCell
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: Layout.inset),
            content.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -Layout.inset),
            content.topAnchor.constraint(equalTo: contentView.topAnchor, constant: Layout.inset),
            content.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -Layout.inset)
        ])
    }

    private func makeLabeledRow(
        title: String,
        control: NSView,
        alignment: NSLayoutConstraint.Attribute = .centerY
    ) -> NSStackView {
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.widthAnchor.constraint(equalToConstant: 92).isActive = true
        label.setContentHuggingPriority(.required, for: .horizontal)
        let row = NSStackView(views: [label, control])
        row.orientation = .horizontal
        row.alignment = alignment == .top ? .top : .centerY
        row.spacing = 10
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return row
    }

    private func makeScrollView(_ textView: NSTextView, minimumHeight: CGFloat) -> NSScrollView {
        let scroll = NSScrollView()
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
        scroll.borderType = .bezelBorder
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: minimumHeight).isActive = true
        return scroll
    }

    private func renderProfile(_ profile: EnhancementProfile) {
        isRenderingSelection = true
        nameField.stringValue = profile.name
        contextTextView.string = profile.context
        termsTable.reloadData()
        if terms.isEmpty {
            termsTable.deselectAll(nil)
        } else {
            termsTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        }
        isRenderingSelection = false
        renderSelectedTerm()
        updateDocumentEditedState()
    }

    private func renderSelectedTerm() {
        isRenderingSelection = true
        defer { isRenderingSelection = false }
        guard let index = selectedTermIndex else {
            canonicalField.stringValue = ""
            spokenTextView.string = ""
            replaceFromTextView.string = ""
            descriptionField.stringValue = ""
            setTermDetailEnabled(false)
            return
        }
        let term = terms[index]
        canonicalField.stringValue = term.canonical
        spokenTextView.string = term.spoken.joined(separator: "\n")
        replaceFromTextView.string = term.replaceFrom.joined(separator: "\n")
        descriptionField.stringValue = term.description
        setTermDetailEnabled(true)
    }

    private func setTermDetailEnabled(_ enabled: Bool) {
        canonicalField.isEnabled = enabled && !isSaving
        spokenTextView.isEditable = enabled && !isSaving
        replaceFromTextView.isEditable = enabled && !isSaving
        descriptionField.isEnabled = enabled && !isSaving
        removeTermButton.isEnabled = enabled && !isSaving
    }

    @objc private func addTerm() {
        clearMessage()
        guard terms.count < EnhancementCatalogLimits.maximumProfileTermCount else {
            showMessage(
                "A profile can contain at most "
                    + "\(EnhancementCatalogLimits.maximumProfileTermCount) terms."
            )
            return
        }
        terms.append(EnhancementTerm(canonical: ""))
        termsTable.reloadData()
        let index = terms.count - 1
        termsTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        termsTable.scrollRowToVisible(index)
        canonicalField.becomeFirstResponder()
        updateDocumentEditedState()
    }

    @objc private func removeTerm() {
        guard let index = selectedTermIndex else {
            return
        }
        clearMessage()
        terms.remove(at: index)
        termsTable.reloadData()
        if !terms.isEmpty {
            termsTable.selectRowIndexes(
                IndexSet(integer: min(index, terms.count - 1)),
                byExtendingSelection: false
            )
        } else {
            renderSelectedTerm()
        }
        updateDocumentEditedState()
    }

    @objc private func cancel() {
        guard windowShouldClose(window!) else {
            return
        }
        close()
    }

    @objc private func save() {
        guard !isSaving, let onSave else {
            return
        }
        let profile = currentProfile()
        if let validationMessage = validate(profile) {
            showMessage(validationMessage)
            return
        }

        isSaving = true
        setControlsEnabled(false)
        switch onSave(profile, expectedFingerprint) {
        case .success(let catalog):
            baselineProfile = profile
            onSaved?(catalog, profile.id)
            close()
        case .failure(let error):
            isSaving = false
            setControlsEnabled(true)
            showMessage("Could not save profile: \(error.localizedDescription)")
        }
    }

    private func currentProfile() -> EnhancementProfile {
        EnhancementProfile(
            id: profileID,
            name: nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            context: contextTextView.string.trimmingCharacters(in: .whitespacesAndNewlines),
            terms: terms.map { term in
                EnhancementTerm(
                    canonical: term.canonical.trimmingCharacters(in: .whitespacesAndNewlines),
                    spoken: stableUnique(term.spoken),
                    replaceFrom: stableUnique(term.replaceFrom),
                    description: term.description.trimmingCharacters(in: .whitespacesAndNewlines),
                    extraFields: term.extraFields
                )
            },
            extraFields: profileExtraFields
        )
    }

    private func validate(_ profile: EnhancementProfile) -> String? {
        guard !profile.name.isEmpty else {
            return "Enter a profile name."
        }
        guard profile.name.utf8.count
                <= EnhancementCatalogLimits.maximumStringUTF8Bytes else {
            return "The profile name is too long."
        }
        guard profile.context.utf8.count
                <= EnhancementCatalogLimits.maximumContextUTF8Bytes else {
            return "The profile context is too long."
        }
        guard profile.terms.count
                <= EnhancementCatalogLimits.maximumProfileTermCount else {
            return "The profile contains too many terms."
        }
        for (index, term) in profile.terms.enumerated() where term.canonical.isEmpty {
            return "Term \(index + 1) needs a canonical value."
        }
        var replacementOwners: [String: String] = [:]
        for (index, term) in profile.terms.enumerated() {
            guard term.canonical.utf8.count
                    <= EnhancementCatalogLimits.maximumStringUTF8Bytes,
                  term.description.utf8.count
                    <= EnhancementCatalogLimits.maximumStringUTF8Bytes else {
                return "Term \(index + 1) contains text that is too long."
            }
            guard term.spoken.count
                    <= EnhancementCatalogLimits.maximumAliasesPerTerm,
                  term.replaceFrom.count
                    <= EnhancementCatalogLimits.maximumAliasesPerTerm else {
                return "Term \(index + 1) contains too many aliases."
            }
            guard (term.spoken + term.replaceFrom).allSatisfy({
                $0.utf8.count <= EnhancementCatalogLimits.maximumStringUTF8Bytes
            }) else {
                return "Term \(index + 1) contains an alias that is too long."
            }
            for source in term.replaceFrom {
                if let owner = replacementOwners[source], owner != term.canonical {
                    return "\"\(source)\" cannot replace both \"\(owner)\" and \"\(term.canonical)\"."
                }
                replacementOwners[source] = term.canonical
            }
        }
        guard JSONSerialization.isValidJSONObject(profile.backendPayload),
              let payload = try? JSONSerialization.data(
                  withJSONObject: profile.backendPayload,
                  options: [.sortedKeys]
              ),
              payload.count
                <= EnhancementCatalogLimits.maximumSerializedPayloadBytes else {
            return "The profile is too large to send to the transcription backend."
        }
        return nil
    }

    private func nonemptyLines(_ value: String) -> [String] {
        stableUnique(value.components(separatedBy: .newlines))
    }

    private func stableUnique(_ values: [String]) -> [String] {
        var seen: Set<String> = []
        return values.compactMap { value in
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, seen.insert(trimmed).inserted else {
                return nil
            }
            return trimmed
        }
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
        nameField.isEnabled = enabled
        contextTextView.isEditable = enabled
        termsTable.isEnabled = enabled
        addTermButton.isEnabled =
            enabled && terms.count < EnhancementCatalogLimits.maximumProfileTermCount
        cancelButton.isEnabled = enabled
        saveButton.isEnabled = enabled
        setTermDetailEnabled(enabled && selectedTermIndex != nil)
    }

    private func updateDocumentEditedState() {
        window?.isDocumentEdited = isDirty
        if !isSaving {
            addTermButton.isEnabled =
                terms.count < EnhancementCatalogLimits.maximumProfileTermCount
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
