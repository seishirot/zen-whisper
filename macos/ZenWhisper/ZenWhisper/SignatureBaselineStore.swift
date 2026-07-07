import Foundation

enum SignatureBaselineStoreError: Error {
    case notDailyBundle
    case wrongDailyLocation(String)
    case invalidBundleSignature
    case missingDesignatedRequirement
    case missingExecutableHash
    case rollbackFailed(original: String, rollback: String, backupPath: String)
}

enum SignatureBaselineStore {
    static func acceptCurrentDailyApp(appSupport: URL) throws {
        guard AppRuntimeIdentity.isDailyContext else {
            throw SignatureBaselineStoreError.notDailyBundle
        }
        guard AppRuntimeIdentity.currentBundlePath == AppRuntimeIdentity.dailyAppPath else {
            throw SignatureBaselineStoreError.wrongDailyLocation(AppRuntimeIdentity.currentBundlePath)
        }
        guard SignatureValidator.verifyBundleIntegrity(for: Bundle.main.bundleURL) else {
            throw SignatureBaselineStoreError.invalidBundleSignature
        }
        guard let requirement = SignatureValidator.designatedRequirement(for: Bundle.main.bundleURL) else {
            throw SignatureBaselineStoreError.missingDesignatedRequirement
        }
        guard let executableHash = SignatureValidator.executableHash(for: Bundle.main.bundleURL) else {
            throw SignatureBaselineStoreError.missingExecutableHash
        }

        let fm = FileManager.default
        let installDir = appSupport.appendingPathComponent("install", isDirectory: true)
        try fm.createDirectory(
            at: installDir,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: installDir.path)

        let signingURL = installDir.appendingPathComponent("signing.json")
        let temporaryURL = installDir.appendingPathComponent(
            "signing.json.accepting.\(UUID().uuidString)"
        )
        let backupURL = installDir.appendingPathComponent(
            "signing.json.previous.\(UUID().uuidString)"
        )
        let payload: [String: Any] = [
            "accepted_in_app": true,
            "app_path": AppRuntimeIdentity.dailyAppPath,
            "created_at": ISO8601DateFormatter().string(from: Date()),
            "designated_requirement": requirement,
            "executable_sha256": executableHash,
            "identity": "accepted in app"
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: temporaryURL, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporaryURL.path)

        var movedExisting = false
        if fm.fileExists(atPath: signingURL.path) {
            try fm.moveItem(at: signingURL, to: backupURL)
            movedExisting = true
        }
        do {
            try fm.moveItem(at: temporaryURL, to: signingURL)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: signingURL.path)
            if movedExisting {
                do {
                    try fm.removeItem(at: backupURL)
                } catch {
                    NSLog(
                        "zen-whisper: accepted signature but could not remove previous baseline backup %@: %@",
                        backupURL.path,
                        String(describing: error)
                    )
                }
            }
        } catch {
            do {
                if fm.fileExists(atPath: temporaryURL.path) {
                    try fm.removeItem(at: temporaryURL)
                }
            } catch {
                NSLog(
                    "zen-whisper: could not remove failed signature baseline temp file %@: %@",
                    temporaryURL.path,
                    String(describing: error)
                )
            }
            if movedExisting {
                do {
                    try fm.moveItem(at: backupURL, to: signingURL)
                } catch let rollbackError {
                    throw SignatureBaselineStoreError.rollbackFailed(
                        original: String(describing: error),
                        rollback: String(describing: rollbackError),
                        backupPath: backupURL.path
                    )
                }
            }
            throw error
        }
    }
}
