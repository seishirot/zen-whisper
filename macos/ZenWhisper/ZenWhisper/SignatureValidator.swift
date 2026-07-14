import CryptoKit
import Foundation

enum SignatureStatus: Equatable {
    case valid
    case missingBaseline
    case invalidBaseline(String)
    case changed
    case notDailyBundle
}

struct SignatureValidator {
    let appSupport: URL

    func validateCurrentApp() -> SignatureStatus {
        guard AppRuntimeIdentity.isDailyContext else {
            return .notDailyBundle
        }
        guard AppRuntimeIdentity.currentBundlePath == AppRuntimeIdentity.dailyAppPath else {
            return .changed
        }
        guard Self.verifyBundleIntegrity(for: Bundle.main.bundleURL) else {
            return .changed
        }
        let signingJSON = appSupport.appendingPathComponent("install/signing.json")
        guard FileManager.default.fileExists(atPath: signingJSON.path) else {
            return .missingBaseline
        }
        let data: Data
        do {
            data = try Data(contentsOf: signingJSON)
        } catch {
            return .invalidBaseline("Could not read \(signingJSON.path): \(error.localizedDescription)")
        }
        let object: [String: Any]
        do {
            guard let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return .invalidBaseline("Signing baseline is not a JSON object: \(signingJSON.path)")
            }
            object = decoded
        } catch {
            return .invalidBaseline("Could not parse \(signingJSON.path): \(error.localizedDescription)")
        }
        guard let expected = object["designated_requirement"] as? String, !expected.isEmpty,
              let expectedPath = object["app_path"] as? String, !expectedPath.isEmpty,
              let expectedExecutableHash = object["executable_sha256"] as? String, !expectedExecutableHash.isEmpty else {
            return .invalidBaseline("Signing baseline is incomplete: \(signingJSON.path)")
        }
        guard URL(fileURLWithPath: expectedPath).standardizedFileURL.path
            == AppRuntimeIdentity.currentBundlePath else {
            return .changed
        }
        guard let current = Self.designatedRequirement(for: Bundle.main.bundleURL) else {
            return .changed
        }
        guard current == Self.normalizedDesignatedRequirement(expected) else {
            return .changed
        }
        guard Self.executableHash(for: Bundle.main.bundleURL) == expectedExecutableHash else {
            return .changed
        }
        return .valid
    }

    static func designatedRequirement(for appURL: URL) -> String? {
        let process = Process()
        let outputPipe = Pipe()
        let errorPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        process.arguments = ["-dr", "-", appURL.path]
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return nil
        }
        let outputData = outputPipe.fileHandleForReading.readDataToEndOfFile()
        let errorData = errorPipe.fileHandleForReading.readDataToEndOfFile()
        let output = (String(data: outputData, encoding: .utf8) ?? "")
            + "\n"
            + (String(data: errorData, encoding: .utf8) ?? "")
        let line = output
            .split(separator: "\n")
            .map(String.init)
            .first(where: { $0.contains("designated =>") })
        return normalizedDesignatedRequirement(line)
    }

    static func verifyBundleIntegrity(for appURL: URL) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        process.arguments = ["--verify", "--strict", appURL.path]
        process.standardOutput = Pipe()
        let errorPipe = Pipe()
        process.standardError = errorPipe
        do {
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 {
                return true
            }
            let data = errorPipe.fileHandleForReading.readDataToEndOfFile()
            let message = String(data: data, encoding: .utf8) ?? ""
            return isLocalTrustOnlyFailure(message)
        } catch {
            return false
        }
    }

    static func isLocalTrustOnlyFailure(_ message: String) -> Bool {
        guard message.contains("CSSMERR_TP_NOT_TRUSTED") else {
            return false
        }
        let lower = message.lowercased()
        let integrityFailureTerms = [
            "bundle format",
            "code object is not signed",
            "code object is not signed at all",
            "invalid",
            "main executable failed",
            "modified",
            "not signed",
            "rejected",
            "resource envelope",
            "sealed resource",
            "unsealed"
        ]
        return !integrityFailureTerms.contains { lower.contains($0) }
    }

    static func executableHash(for appURL: URL) -> String? {
        let executableURL = appURL.appendingPathComponent("Contents/MacOS/zen-whisper")
        guard let data = try? Data(contentsOf: executableURL) else {
            return nil
        }
        return SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }

    static func normalizedDesignatedRequirement(_ value: String?) -> String? {
        guard let value else {
            return nil
        }
        let marker = "designated =>"
        if let range = value.range(of: marker) {
            return String(value[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
