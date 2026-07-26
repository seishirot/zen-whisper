import AppKit
import XCTest
@testable import ZenWhisper

@MainActor
final class SettingsWindowLayoutTests: XCTestCase {
    func testDefaultLayoutKeepsGeneralSectionIntactAndButtonsAtBottom() throws {
        let registry = try ModelRegistry.loadDefault()
        let controller = SettingsWindowController(
            registry: registry,
            settings: makeSettings(),
            launchAtLoginStatus: .disabled
        )

        guard let window = controller.window,
              let contentView = window.contentView,
              let launchAtLogin = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.launchAtLogin,
                  in: contentView
              ),
              let generalSection = ancestors(of: launchAtLogin).first(where: { $0 is NSBox }),
              let cancelButton = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.cancel,
                  in: contentView
              ) else {
            return XCTFail("Expected Settings window views")
        }

        contentView.layoutSubtreeIfNeeded()

        let launchFrame = launchAtLogin.convert(launchAtLogin.bounds, to: generalSection)
        XCTAssertTrue(
            generalSection.bounds.contains(launchFrame),
            "General section \(generalSection.bounds) must contain Launch at Login \(launchFrame)"
        )

        let cancelFrame = cancelButton.convert(cancelButton.bounds, to: contentView)
        XCTAssertEqual(
            cancelFrame.minY,
            18,
            accuracy: 1,
            "Settings footer should stay at the bottom instead of leaving unused space"
        )
        XCTAssertLessThan(
            window.contentLayoutRect.height,
            650,
            "The default Settings window should fit its content without excessive empty space"
        )
    }

    func testVisibleWarningsExpandTheWindowWithoutCompressingGeneralSection() throws {
        let registry = try ModelRegistry.loadDefault()
        let settings = makeSettings()
        let controller = SettingsWindowController(
            registry: registry,
            settings: settings,
            launchAtLoginStatus: .disabled
        )

        guard let window = controller.window,
              let contentView = window.contentView else {
            return XCTFail("Expected Settings window")
        }
        window.setContentSize(NSSize(width: 720, height: 620))
        controller.windowDidResize(
            Notification(name: NSWindow.didResizeNotification, object: window)
        )
        controller.synchronize(
            authoritativeSettings: settings,
            launchAtLoginStatus: .invalid(
                String(
                    repeating: "The saved LaunchAgent could not be read and needs an explicit replacement. ",
                    count: 10
                )
            ),
            audioInputDevices: [],
            isBusy: true
        )
        contentView.layoutSubtreeIfNeeded()
        XCTAssertEqual(window.contentLayoutRect.width, 720, accuracy: 1)
        let wideMinimumHeight = window.contentMinSize.height

        guard let launchAtLogin = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.launchAtLogin,
                  in: contentView
              ),
              let launchMessage = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.launchAtLoginMessage,
                  in: contentView
              ),
              let generalSection = ancestors(of: launchAtLogin).first(where: { $0 is NSBox }),
              let cancelButton = findView(
                  identifier: SettingsWindowController.AccessibilityIdentifier.cancel,
                  in: contentView
              ) else {
            return XCTFail("Expected Settings warning views")
        }

        let launchFrame = launchAtLogin.convert(launchAtLogin.bounds, to: generalSection)
        let messageFrame = launchMessage.convert(launchMessage.bounds, to: generalSection)
        XCTAssertTrue(generalSection.bounds.contains(launchFrame))
        XCTAssertTrue(generalSection.bounds.contains(messageFrame))
        XCTAssertGreaterThan(window.contentLayoutRect.height, 620)
        XCTAssertGreaterThan(window.contentMinSize.height, 620)

        let cancelFrame = cancelButton.convert(cancelButton.bounds, to: contentView)
        let generalFrame = generalSection.convert(generalSection.bounds, to: contentView)
        XCTAssertEqual(cancelFrame.minY, 18, accuracy: 1)
        XCTAssertGreaterThanOrEqual(generalFrame.minY, cancelFrame.maxY + 16)

        window.setContentSize(
            NSSize(width: 620, height: window.contentLayoutRect.height)
        )
        controller.windowDidResize(
            Notification(name: NSWindow.didResizeNotification, object: window)
        )
        contentView.layoutSubtreeIfNeeded()

        let resizedLaunchFrame = launchAtLogin.convert(launchAtLogin.bounds, to: generalSection)
        let resizedMessageFrame = launchMessage.convert(launchMessage.bounds, to: generalSection)
        let narrowMinimumHeight = window.contentMinSize.height
        XCTAssertEqual(window.contentLayoutRect.width, 620, accuracy: 1)
        XCTAssertGreaterThan(narrowMinimumHeight, wideMinimumHeight)
        XCTAssertTrue(generalSection.bounds.contains(resizedLaunchFrame))
        XCTAssertTrue(generalSection.bounds.contains(resizedMessageFrame))

        window.setContentSize(NSSize(width: 620, height: 620))
        controller.windowDidResize(
            Notification(name: NSWindow.didResizeNotification, object: window)
        )
        contentView.layoutSubtreeIfNeeded()
        XCTAssertGreaterThanOrEqual(
            window.contentLayoutRect.height + 1,
            window.contentMinSize.height
        )

        window.setContentSize(
            NSSize(width: 720, height: window.contentLayoutRect.height)
        )
        controller.windowDidResize(
            Notification(name: NSWindow.didResizeNotification, object: window)
        )
        XCTAssertLessThan(window.contentMinSize.height, narrowMinimumHeight)

        controller.synchronize(
            authoritativeSettings: settings,
            launchAtLoginStatus: .disabled,
            audioInputDevices: [],
            isBusy: false
        )
        XCTAssertEqual(window.contentMinSize.height, 620, accuracy: 1)
    }

    func testReopeningWindowPreservesUserPosition() throws {
        let registry = try ModelRegistry.loadDefault()
        let controller = SettingsWindowController(
            registry: registry,
            settings: makeSettings(),
            launchAtLoginStatus: .disabled
        )

        guard let window = controller.window else {
            return XCTFail("Expected Settings window")
        }
        defer { window.orderOut(nil) }

        controller.showSettings()
        let movedOrigin = NSPoint(
            x: window.frame.origin.x + 37,
            y: window.frame.origin.y + 29
        )
        window.setFrameOrigin(movedOrigin)
        window.orderOut(nil)

        controller.showSettings()

        XCTAssertEqual(window.frame.origin.x, movedOrigin.x, accuracy: 1)
        XCTAssertEqual(window.frame.origin.y, movedOrigin.y, accuracy: 1)
    }

    private func findView(identifier: String, in root: NSView) -> NSView? {
        if root.identifier?.rawValue == identifier {
            return root
        }
        for subview in root.subviews {
            if let match = findView(identifier: identifier, in: subview) {
                return match
            }
        }
        return nil
    }

    private func ancestors(of view: NSView) -> [NSView] {
        var result: [NSView] = []
        var current = view.superview
        while let view = current {
            result.append(view)
            current = view.superview
        }
        return result
    }

    private func makeSettings() -> SettingsSnapshot {
        SettingsSnapshot(
            hotkey: .shiftSpace,
            submitHotkey: .shiftCommandSpace,
            language: "ja",
            engine: "mlx-whisper",
            lastModelByEngine: [
                "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
                "mlx-qwen3-asr": "mlx-community/Qwen3-ASR-0.6B-8bit",
            ],
            silenceAutoStopEnabled: true,
            microphoneDeviceUID: nil,
            outputMode: .pasteRestoreClipboard,
            allowUnverifiedPasteFallback: false
        )
    }
}
