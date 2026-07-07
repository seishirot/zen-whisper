import AppKit
import Carbon

final class HotkeyRecorderWindowController: NSWindowController, NSWindowDelegate {
    private let completion: (HotkeyShortcut?) -> Void
    private let statusLabel = NSTextField(labelWithString: "Press a shortcut now.")
    private var didFinish = false
    private let captureView = HotkeyCaptureView()

    init(
        title: String = "Record Hotkey",
        instruction: String = "Press the shortcut to use for recording. Use Ctrl, Option, or Cmd. Shift+Space is also allowed.",
        currentShortcut: HotkeyShortcut,
        completion: @escaping (HotkeyShortcut?) -> Void
    ) {
        self.completion = completion

        let contentView = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 210))
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = .systemFont(ofSize: 17, weight: .semibold)

        let instructionLabel = NSTextField(wrappingLabelWithString: instruction)
        instructionLabel.textColor = .secondaryLabelColor

        let currentLabel = NSTextField(labelWithString: "Current: \(currentShortcut.label)")
        currentLabel.textColor = .secondaryLabelColor

        statusLabel.font = .monospacedSystemFont(ofSize: 14, weight: .medium)
        statusLabel.alignment = .center

        let cancelButton = NSButton(title: "Cancel", target: nil, action: #selector(cancel))
        cancelButton.bezelStyle = .rounded

        let buttonRow = NSStackView(views: [cancelButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .trailing
        buttonRow.distribution = .gravityAreas

        let stack = NSStackView(views: [titleLabel, instructionLabel, currentLabel, captureView, statusLabel, buttonRow])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -18),
            captureView.widthAnchor.constraint(equalTo: stack.widthAnchor),
            captureView.heightAnchor.constraint(equalToConstant: 56),
            statusLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            buttonRow.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])

        let window = NSWindow(
            contentRect: contentView.frame,
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = title
        window.contentView = contentView
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self

        cancelButton.target = self
        captureView.onShortcut = { [weak self] shortcut in
            self?.finish(shortcut)
        }
        captureView.onInvalid = { [weak self] message in
            self?.statusLabel.stringValue = message
        }
        captureView.onCancel = { [weak self] in
            self?.finish(nil)
        }
    }

    required init?(coder: NSCoder) {
        return nil
    }

    func showRecorder() {
        guard let window else {
            return
        }
        NSApp.activate(ignoringOtherApps: true)
        window.center()
        showWindow(nil)
        window.makeKeyAndOrderFront(nil)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.window?.makeFirstResponder(self.captureView)
        }
    }

    func windowWillClose(_ notification: Notification) {
        finish(nil)
    }

    @objc private func cancel() {
        finish(nil)
    }

    private func finish(_ shortcut: HotkeyShortcut?) {
        guard !didFinish else {
            return
        }
        didFinish = true
        close()
        completion(shortcut)
    }
}

private final class HotkeyCaptureView: NSView {
    var onShortcut: ((HotkeyShortcut) -> Void)?
    var onInvalid: ((String) -> Void)?
    var onCancel: (() -> Void)?

    override var acceptsFirstResponder: Bool {
        true
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 8
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
    }

    required init?(coder: NSCoder) {
        return nil
    }

    override func drawFocusRingMask() {
        bounds.fill()
    }

    override var focusRingMaskBounds: NSRect {
        bounds
    }

    override func keyDown(with event: NSEvent) {
        let activeModifiers = event.modifierFlags.intersection([.shift, .control, .option, .command])
        if event.keyCode == UInt16(kVK_Escape), activeModifiers.isEmpty {
            onCancel?()
            return
        }
        guard let shortcut = HotkeyShortcut.fromEvent(event) else {
            onInvalid?("Use Ctrl, Option, or Cmd. Shift+Space is also allowed.")
            return
        }
        onShortcut?(shortcut)
    }
}
