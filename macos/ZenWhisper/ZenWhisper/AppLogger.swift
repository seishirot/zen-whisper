import Foundation

final class AppLogger {
    private static let maxBytes: UInt64 = 1_000_000
    private static let backupCount = 3
    private let logURL: URL
    private let queue = DispatchQueue(label: "com.seishirot.zenwhisper.app-log")
    private let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    init(logsDirectory: URL) {
        self.logURL = logsDirectory.appendingPathComponent("app.log")
    }

    func info(_ message: String) {
        write("INFO", message)
    }

    private func write(_ level: String, _ message: String) {
        let line = "\(formatter.string(from: Date())) \(level) \(message)\n"
        queue.async { [logURL] in
            guard let data = line.data(using: .utf8) else {
                Self.reportLogFailure("could not encode app log line", nil)
                return
            }
            do {
                try Self.rotateIfNeeded(logURL: logURL)
                if FileManager.default.fileExists(atPath: logURL.path) {
                    let handle = try FileHandle(forWritingTo: logURL)
                    defer { try? handle.close() }
                    try FileManager.default.setAttributes(
                        [.posixPermissions: 0o600],
                        ofItemAtPath: logURL.path
                    )
                    _ = try handle.seekToEnd()
                    try handle.write(contentsOf: data)
                } else {
                    try data.write(to: logURL, options: .atomic)
                    try FileManager.default.setAttributes(
                        [.posixPermissions: 0o600],
                        ofItemAtPath: logURL.path
                    )
                }
            } catch {
                Self.reportLogFailure("app log write failed at \(logURL.path)", error)
            }
        }
    }

    private static func rotateIfNeeded(logURL: URL) throws {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: logURL.path),
              let size = attrs[.size] as? UInt64,
              size >= maxBytes else {
            return
        }
        for index in stride(from: backupCount - 1, through: 1, by: -1) {
            let source = rotatedURL(logURL, index: index)
            let destination = rotatedURL(logURL, index: index + 1)
            if fm.fileExists(atPath: source.path) {
                if fm.fileExists(atPath: destination.path) {
                    try fm.removeItem(at: destination)
                }
                try fm.moveItem(at: source, to: destination)
                try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
            }
        }
        let first = rotatedURL(logURL, index: 1)
        if fm.fileExists(atPath: first.path) {
            try fm.removeItem(at: first)
        }
        try fm.moveItem(at: logURL, to: first)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: first.path)
    }

    private static func rotatedURL(_ logURL: URL, index: Int) -> URL {
        logURL.deletingLastPathComponent()
            .appendingPathComponent("\(logURL.lastPathComponent).\(index)")
    }

    private static func reportLogFailure(_ message: String, _ error: Error?) {
        if let error {
            NSLog("zen-whisper app log failure: %@: %@", message, String(describing: error))
        } else {
            NSLog("zen-whisper app log failure: %@", message)
        }
    }
}
