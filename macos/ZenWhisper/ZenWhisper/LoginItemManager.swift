import Foundation
import Darwin

enum LoginItemStatus: Equatable {
    case enabled
    case disabled
    case invalid(String)

    var isEnabled: Bool {
        if case .enabled = self {
            return true
        }
        return false
    }
}

struct LoginItemManager {
    static let label = "com.seishirot.zenwhisper"
    static let appPath = "/Applications/zen-whisper.app"

    typealias LaunchctlRunner = ([String]) throws -> Void

    private let fileManager: FileManager
    private let homeDirectory: URL
    private let appPath: String
    private let appExists: (String) -> Bool
    private let launchctlRunner: LaunchctlRunner?
    private let removeItem: (URL) throws -> Void

    init(
        fileManager: FileManager = .default,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        appPath: String = LoginItemManager.appPath,
        appExists: @escaping (String) -> Bool = { FileManager.default.fileExists(atPath: $0) },
        launchctlRunner: LaunchctlRunner? = nil,
        removeItem: ((URL) throws -> Void)? = nil
    ) {
        self.fileManager = fileManager
        self.homeDirectory = homeDirectory
        self.appPath = appPath
        self.appExists = appExists
        self.launchctlRunner = launchctlRunner
        self.removeItem = removeItem ?? { url in
            try fileManager.removeItem(at: url)
        }
    }

    var plistURL: URL {
        homeDirectory
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("LaunchAgents", isDirectory: true)
            .appendingPathComponent("\(Self.label).plist")
    }

    func isEnabled() -> Bool {
        status().isEnabled
    }

    func status() -> LoginItemStatus {
        guard fileManager.fileExists(atPath: plistURL.path) else {
            return .disabled
        }
        do {
            let data = try Data(contentsOf: plistURL)
            guard let plist = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else {
                return .invalid("LaunchAgent plist is not a dictionary: \(plistURL.path)")
            }
            guard plist["Label"] as? String == Self.label,
                  let args = plist["ProgramArguments"] as? [String] else {
                return .disabled
            }
            return args == ["/usr/bin/open", appPath] ? .enabled : .disabled
        } catch {
            return .invalid("LaunchAgent plist is unreadable: \(plistURL.path): \(error.localizedDescription)")
        }
    }

    func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try enable()
        } else {
            try disable()
        }
    }

    private func enable() throws {
        guard appPath == Self.appPath else {
            throw LoginItemError.unsupportedAppPath(appPath)
        }
        guard appExists(appPath) else {
            throw LoginItemError.appNotInstalled(appPath)
        }

        let launchAgents = plistURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: launchAgents, withIntermediateDirectories: true)
        let logDirectory = homeDirectory
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Logs", isDirectory: true)
            .appendingPathComponent("zen-whisper", isDirectory: true)
        try fileManager.createDirectory(at: logDirectory, withIntermediateDirectories: true)

        let plist: [String: Any] = [
            "Label": Self.label,
            "ProgramArguments": ["/usr/bin/open", appPath],
            "RunAtLoad": true,
            "KeepAlive": false,
            "ProcessType": "Interactive",
            "StandardOutPath": logDirectory.appendingPathComponent("login.stdout.log").path,
            "StandardErrorPath": logDirectory.appendingPathComponent("login.stderr.log").path
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try data.write(to: plistURL, options: .atomic)
        try setPermissions(plistURL, mode: 0o644)

        try bootoutIfLoaded()
        do {
            try launchctl(["bootstrap", launchDomain(), plistURL.path])
        } catch {
            do {
                if fileManager.fileExists(atPath: plistURL.path) {
                    try removeItem(plistURL)
                }
            } catch let cleanupError {
                throw LoginItemError.bootstrapCleanupFailed(
                    bootstrap: String(describing: error),
                    cleanup: String(describing: cleanupError),
                    plistPath: plistURL.path
                )
            }
            throw error
        }
    }

    private func disable() throws {
        try bootoutIfLoaded()
        if fileManager.fileExists(atPath: plistURL.path) {
            try removeItem(plistURL)
        }
    }

    private func launchDomain() -> String {
        "gui/\(getuid())"
    }

    private func launchctl(_ arguments: [String]) throws {
        if let launchctlRunner {
            try launchctlRunner(arguments)
            return
        }
        let process = Process()
        let error = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardError = error
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let message = String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let detail = message?.isEmpty == false
                ? message!
                : "launchctl \(arguments.joined(separator: " ")) exited \(process.terminationStatus)"
            throw LoginItemError.launchctlFailed(detail)
        }
    }

    private func bootoutIfLoaded() throws {
        do {
            try launchctl(["bootout", launchDomain(), plistURL.path])
        } catch LoginItemError.launchctlFailed(let message) where Self.isBenignBootoutFailure(message) {
            return
        }
    }

    static func isBenignBootoutFailure(_ message: String) -> Bool {
        let lower = message.lowercased()
        return lower.contains("no such process")
            || lower.contains("no such file")
            || lower.contains("not found")
            || lower.contains("not loaded")
            || lower.contains("could not find specified service")
            || lower.contains("bootstrap failed: 3")
    }

    private func setPermissions(_ url: URL, mode: mode_t) throws {
        guard chmod(url.path, mode) == 0 else {
            throw LoginItemError.permissionFailed(errno)
        }
    }
}

enum LoginItemError: Error, LocalizedError {
    case appNotInstalled(String)
    case unsupportedAppPath(String)
    case launchctlFailed(String)
    case permissionFailed(Int32)
    case bootstrapCleanupFailed(bootstrap: String, cleanup: String, plistPath: String)

    var errorDescription: String? {
        switch self {
        case .appNotInstalled(let path):
            return "App is not installed at \(path). Run install_app.sh first."
        case .unsupportedAppPath(let path):
            return "Launch at Login is only supported for \(LoginItemManager.appPath), not \(path)."
        case .launchctlFailed(let message):
            return "launchctl failed: \(message)"
        case .permissionFailed(let code):
            return "Could not set LaunchAgent permissions: errno \(code)"
        case .bootstrapCleanupFailed(let bootstrap, let cleanup, let plistPath):
            return "launchctl bootstrap failed and cleanup also failed for \(plistPath). bootstrap=\(bootstrap); cleanup=\(cleanup)"
        }
    }
}
