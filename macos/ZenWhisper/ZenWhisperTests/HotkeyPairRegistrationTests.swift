import XCTest
@testable import ZenWhisper

final class HotkeyPairRegistrationTests: XCTestCase {
    func testSubmitRegistrationFailureRestoresBothPreviousHotkeys() {
        let primary = FakeHotkeyManager()
        let submit = FakeHotkeyManager(
            failures: [1: FakeHotkeyError("proposed submit unavailable")]
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .registrationFailed("proposed submit unavailable")
            )
        }

        XCTAssertEqual(primary.registeredShortcut, .shiftSpace)
        XCTAssertEqual(submit.registeredShortcut, .shiftCommandSpace)
        XCTAssertEqual(primary.suspendCount, 1)
        XCTAssertEqual(submit.suspendCount, 1)
        XCTAssertEqual(primary.unregisterCount, 1)
        XCTAssertEqual(submit.unregisterCount, 1)
    }

    func testPrimaryRegistrationFailureRestoresPreviousPair() {
        let primary = FakeHotkeyManager(
            failures: [1: FakeHotkeyError("proposed primary unavailable")]
        )
        let submit = FakeHotkeyManager()
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        )

        XCTAssertEqual(primary.registeredShortcut, .shiftSpace)
        XCTAssertEqual(submit.registeredShortcut, .shiftCommandSpace)
        XCTAssertEqual(primary.registerCallCount, 2)
        XCTAssertEqual(submit.registerCallCount, 1)
    }

    func testRollbackFailureReportsBothErrorsAndLeavesNoPartialPair() {
        let primary = FakeHotkeyManager(
            failures: [2: FakeHotkeyError("previous primary unavailable")]
        )
        let submit = FakeHotkeyManager(
            failures: [1: FakeHotkeyError("proposed submit unavailable")]
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .rollbackFailed(
                    registration: "proposed submit unavailable",
                    rollback: "previous primary unavailable"
                )
            )
        }

        XCTAssertNil(primary.registeredShortcut)
        XCTAssertNil(submit.registeredShortcut)
    }

    func testSubmitRollbackFailureRemovesRestoredPrimary() {
        let primary = FakeHotkeyManager()
        let submit = FakeHotkeyManager(
            failures: [
                1: FakeHotkeyError("proposed submit unavailable"),
                2: FakeHotkeyError("previous submit unavailable"),
            ]
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .rollbackFailed(
                    registration: "proposed submit unavailable",
                    rollback: "previous submit unavailable"
                )
            )
        }

        XCTAssertNil(primary.registeredShortcut)
        XCTAssertNil(submit.registeredShortcut)
        XCTAssertEqual(primary.unregisterCount, 2)
        XCTAssertEqual(submit.unregisterCount, 2)
    }

    func testDisablingSubmitHotkeyUnregistersSubmitManager() throws {
        let primary = FakeHotkeyManager()
        let submit = FakeHotkeyManager()
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        try transaction.replace(
            with: makeSettings(
                primary: .controlOptionCommandSpace,
                submit: nil
            ),
            restoring: makeSettings(
                primary: .shiftSpace,
                submit: .shiftCommandSpace
            ),
            primaryAction: {},
            submitAction: {}
        )

        XCTAssertEqual(primary.registeredShortcut, .controlOptionCommandSpace)
        XCTAssertNil(submit.registeredShortcut)
        XCTAssertEqual(submit.unregisterCount, 1)
    }

    func testReconcileReplacesMismatchedActivePairWithCommittedPair() throws {
        let primary = FakeHotkeyManager(
            initialShortcut: .controlOptionCommandSpace
        )
        let submit = FakeHotkeyManager()
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        try transaction.reconcile(
            with: makeSettings(
                primary: .shiftSpace,
                submit: .shiftCommandSpace
            ),
            primaryAction: {},
            submitAction: {}
        )

        XCTAssertEqual(primary.activeShortcut, .shiftSpace)
        XCTAssertEqual(submit.activeShortcut, .shiftCommandSpace)
        XCTAssertEqual(primary.suspendCount, 1)
        XCTAssertEqual(submit.suspendCount, 1)
    }

    func testReconcileRemovesUnexpectedSubmitRegistration() throws {
        let primary = FakeHotkeyManager(initialShortcut: .shiftSpace)
        let submit = FakeHotkeyManager(initialShortcut: .shiftCommandSpace)
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        try transaction.reconcile(
            with: makeSettings(primary: .shiftSpace, submit: nil),
            primaryAction: {},
            submitAction: {}
        )

        XCTAssertEqual(primary.activeShortcut, .shiftSpace)
        XCTAssertNil(submit.activeShortcut)
    }

    func testReconcileLeavesExactActivePairUntouched() throws {
        let primary = FakeHotkeyManager(initialShortcut: .shiftSpace)
        let submit = FakeHotkeyManager(initialShortcut: .shiftCommandSpace)
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        try transaction.reconcile(
            with: makeSettings(
                primary: .shiftSpace,
                submit: .shiftCommandSpace
            ),
            primaryAction: {},
            submitAction: {}
        )

        XCTAssertEqual(primary.suspendCount, 0)
        XCTAssertEqual(submit.suspendCount, 0)
        XCTAssertEqual(primary.registerCallCount, 0)
        XCTAssertEqual(submit.registerCallCount, 0)
    }

    func testSubmitSuspendFailureRestoresSuspendedPrimaryAndAbortsReplacement() {
        let primary = FakeHotkeyManager(initialShortcut: .shiftSpace)
        let submit = FakeHotkeyManager(
            initialShortcut: .shiftCommandSpace,
            suspendFailure: FakeHotkeyError("submit cleanup failed")
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .registrationFailed(
                    "Could not suspend the previous submit hotkey: submit cleanup failed"
                )
            )
        }

        XCTAssertEqual(primary.registeredShortcut, .shiftSpace)
        XCTAssertEqual(submit.registeredShortcut, .shiftCommandSpace)
    }

    func testProposalCleanupFailureIsReportedWithoutAttemptingRollback() {
        let primary = FakeHotkeyManager(
            unregisterFailures: [1: FakeHotkeyError("primary unregister failed")]
        )
        let submit = FakeHotkeyManager(
            failures: [1: FakeHotkeyError("proposed submit unavailable")]
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .rollbackFailed(
                    registration: "proposed submit unavailable",
                    rollback: "Could not clear the failed hotkey pair: primary: primary unregister failed"
                )
            )
        }

        XCTAssertEqual(primary.registeredShortcut, .controlOptionCommandSpace)
        XCTAssertNil(submit.registeredShortcut)
        XCTAssertEqual(primary.registerCallCount, 1)
    }

    func testSubmitSuspendAndPrimaryCompensationFailureCleansUpOldSubmit() {
        let primary = FakeHotkeyManager(
            initialShortcut: .shiftSpace,
            failures: [1: FakeHotkeyError("primary compensation failed")]
        )
        let submit = FakeHotkeyManager(
            initialShortcut: .shiftCommandSpace,
            suspendFailure: FakeHotkeyError("submit cleanup failed")
        )
        let transaction = HotkeyPairRegistrationTransaction(
            primaryManager: primary,
            submitManager: submit
        )

        XCTAssertThrowsError(
            try transaction.replace(
                with: makeSettings(
                    primary: .controlOptionCommandSpace,
                    submit: .controlOptionCommandReturn
                ),
                restoring: makeSettings(
                    primary: .shiftSpace,
                    submit: .shiftCommandSpace
                ),
                primaryAction: {},
                submitAction: {}
            )
        ) { error in
            XCTAssertEqual(
                error as? HotkeyPairReplacementError,
                .rollbackFailed(
                    registration: "Could not suspend the previous submit hotkey: submit cleanup failed",
                    rollback: "primary compensation failed"
                )
            )
        }

        XCTAssertNil(primary.registeredShortcut)
        XCTAssertNil(submit.registeredShortcut)
    }

    private func makeSettings(
        primary: HotkeyShortcut,
        submit: HotkeyShortcut?
    ) -> SettingsSnapshot {
        SettingsSnapshot(
            hotkey: primary,
            submitHotkey: submit,
            language: "ja",
            engine: "mlx-whisper",
            lastModelByEngine: [
                "mlx-whisper": "mlx-community/whisper-large-v3-turbo"
            ],
            silenceAutoStopEnabled: true,
            microphoneDeviceUID: nil,
            outputMode: .pasteRestoreClipboard,
            allowUnverifiedPasteFallback: false
        )
    }
}

private struct FakeHotkeyError: Error, CustomStringConvertible {
    let description: String

    init(_ description: String) {
        self.description = description
    }
}

private final class FakeHotkeyManager: HotkeyRegistrationManaging {
    private let failures: [Int: Error]
    private let suspendFailure: Error?
    private let unregisterFailures: [Int: Error]

    private(set) var registeredShortcut: HotkeyShortcut?
    private(set) var registerCallCount = 0
    private(set) var suspendCount = 0
    private(set) var unregisterCount = 0

    var activeShortcut: HotkeyShortcut? {
        registeredShortcut
    }

    init(
        initialShortcut: HotkeyShortcut? = nil,
        failures: [Int: Error] = [:],
        suspendFailure: Error? = nil,
        unregisterFailures: [Int: Error] = [:]
    ) {
        registeredShortcut = initialShortcut
        self.failures = failures
        self.suspendFailure = suspendFailure
        self.unregisterFailures = unregisterFailures
    }

    func register(shortcut: HotkeyShortcut, action: @escaping () -> Void) throws {
        registerCallCount += 1
        if let error = failures[registerCallCount] {
            registeredShortcut = nil
            throw error
        }
        registeredShortcut = shortcut
    }

    func suspendReportingFailure() throws {
        suspendCount += 1
        if let suspendFailure {
            throw suspendFailure
        }
        registeredShortcut = nil
    }

    func unregisterReportingFailure() throws {
        unregisterCount += 1
        if let error = unregisterFailures[unregisterCount] {
            throw error
        }
        registeredShortcut = nil
    }
}
