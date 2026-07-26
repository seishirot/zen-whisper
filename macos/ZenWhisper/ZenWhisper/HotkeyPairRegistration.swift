import Foundation

protocol HotkeyRegistrationManaging: AnyObject {
    var activeShortcut: HotkeyShortcut? { get }

    func register(shortcut: HotkeyShortcut, action: @escaping () -> Void) throws
    func suspendReportingFailure() throws
    func unregisterReportingFailure() throws
}

extension HotkeyManager: HotkeyRegistrationManaging {}

enum HotkeyPairReplacementError: Error, Equatable {
    case registrationFailed(String)
    case rollbackFailed(registration: String, rollback: String)
}

struct HotkeyPairRegistrationTransaction {
    let primaryManager: HotkeyRegistrationManaging
    let submitManager: HotkeyRegistrationManaging

    func reconcile(
        with settings: SettingsSnapshot,
        primaryAction: @escaping () -> Void,
        submitAction: @escaping () -> Void
    ) throws {
        guard primaryManager.activeShortcut != settings.hotkey
                || submitManager.activeShortcut != settings.submitHotkey else {
            return
        }
        try replace(
            with: settings,
            restoring: settings,
            primaryAction: primaryAction,
            submitAction: submitAction
        )
    }

    func replace(
        with proposedSettings: SettingsSnapshot,
        restoring previousSettings: SettingsSnapshot,
        primaryAction: @escaping () -> Void,
        submitAction: @escaping () -> Void
    ) throws {
        do {
            try primaryManager.suspendReportingFailure()
        } catch {
            throw HotkeyPairReplacementError.registrationFailed(
                "Could not suspend the previous primary hotkey: \(error)"
            )
        }
        do {
            try submitManager.suspendReportingFailure()
        } catch {
            let suspensionError = "Could not suspend the previous submit hotkey: \(error)"
            do {
                try primaryManager.register(
                    shortcut: previousSettings.hotkey,
                    action: primaryAction
                )
            } catch {
                let rollbackError = String(describing: error)
                let cleanupErrors = cleanupPair()
                let rollbackDescription = cleanupErrors.isEmpty
                    ? rollbackError
                    : "\(rollbackError); cleanup: \(cleanupErrors.joined(separator: "; "))"
                throw HotkeyPairReplacementError.rollbackFailed(
                    registration: suspensionError,
                    rollback: rollbackDescription
                )
            }
            throw HotkeyPairReplacementError.registrationFailed(suspensionError)
        }

        do {
            try registerPair(
                from: proposedSettings,
                primaryAction: primaryAction,
                submitAction: submitAction
            )
        } catch {
            let registrationError = String(describing: error)
            let proposalCleanupErrors = cleanupPair()
            guard proposalCleanupErrors.isEmpty else {
                throw HotkeyPairReplacementError.rollbackFailed(
                    registration: registrationError,
                    rollback: "Could not clear the failed hotkey pair: \(proposalCleanupErrors.joined(separator: "; "))"
                )
            }
            do {
                try registerPair(
                    from: previousSettings,
                    primaryAction: primaryAction,
                    submitAction: submitAction
                )
            } catch {
                let rollbackError = String(describing: error)
                let rollbackCleanupErrors = cleanupPair()
                let rollbackDescription = rollbackCleanupErrors.isEmpty
                    ? rollbackError
                    : "\(rollbackError); cleanup: \(rollbackCleanupErrors.joined(separator: "; "))"
                throw HotkeyPairReplacementError.rollbackFailed(
                    registration: registrationError,
                    rollback: rollbackDescription
                )
            }
            throw HotkeyPairReplacementError.registrationFailed(registrationError)
        }
    }

    private func registerPair(
        from settings: SettingsSnapshot,
        primaryAction: @escaping () -> Void,
        submitAction: @escaping () -> Void
    ) throws {
        try primaryManager.register(
            shortcut: settings.hotkey,
            action: primaryAction
        )
        if let submitHotkey = settings.submitHotkey {
            try submitManager.register(
                shortcut: submitHotkey,
                action: submitAction
            )
        } else {
            try submitManager.unregisterReportingFailure()
        }
    }

    private func cleanupPair() -> [String] {
        var errors: [String] = []
        do {
            try primaryManager.unregisterReportingFailure()
        } catch {
            errors.append("primary: \(error)")
        }
        do {
            try submitManager.unregisterReportingFailure()
        } catch {
            errors.append("submit: \(error)")
        }
        return errors
    }
}
