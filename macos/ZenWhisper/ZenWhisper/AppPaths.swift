import Darwin
import Foundation

enum AppPathsError: Error, LocalizedError {
    case socketPathTooLong(String, Int, Int)
    case runtimePathNotDirectory(String)
    case runtimePathWrongOwner(String, uid_t, uid_t)
    case runtimePathStatFailed(String, Int32)

    var errorDescription: String? {
        switch self {
        case .socketPathTooLong(let path, let actual, let limit):
            return "Backend socket path is too long (\(actual) > \(limit)): \(path)"
        case .runtimePathNotDirectory(let path):
            return "Backend runtime path is not a directory: \(path)"
        case .runtimePathWrongOwner(let path, let actual, let expected):
            return "Backend runtime path owner is \(actual), expected \(expected): \(path)"
        case .runtimePathStatFailed(let path, let code):
            return "Could not inspect backend runtime path: \(path) errno \(code)"
        }
    }
}

struct AppPaths {
    let appSupport: URL
    let logs: URL
    let runtimeDirectoryOverride: URL?

    init(appSupport: URL, logs: URL, runtimeDirectoryOverride: URL? = nil) {
        self.appSupport = appSupport
        self.logs = logs
        self.runtimeDirectoryOverride = runtimeDirectoryOverride
    }

    static var live: AppPaths {
        let fm = FileManager.default
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("zen-whisper", isDirectory: true)
        let logs = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/zen-whisper", isDirectory: true)
        return AppPaths(appSupport: support, logs: logs)
    }

    var backendPython: URL {
        appSupport.appendingPathComponent("backend/.venv/bin/python")
    }

    var runtimeDirectory: URL {
        if let runtimeDirectoryOverride {
            return runtimeDirectoryOverride
        }
        return URL(fileURLWithPath: "/tmp", isDirectory: true)
            .appendingPathComponent("zen-whisper-\(getuid())", isDirectory: true)
    }

    var socketPath: URL {
        runtimeDirectory.appendingPathComponent("b.sock")
    }

    var backendStartupLog: URL {
        logs.appendingPathComponent("backend-startup.log")
    }

    var recordings: URL {
        appSupport.appendingPathComponent("recordings", isDirectory: true)
    }

    var huggingFaceHome: URL {
        appSupport.appendingPathComponent("models/huggingface", isDirectory: true)
    }

    @discardableResult
    func prepare() throws -> [String] {
        try createPrivateDirectory(appSupport)
        try createPrivateDirectory(logs)
        try createPrivateRuntimeDirectory(runtimeDirectory)
        try validateSocketPathLength()
        try createPrivateDirectory(recordings)
        try createPrivateDirectory(huggingFaceHome)
        return deleteStaleRecordings()
    }

    func createPrivateDirectory(_ url: URL) throws {
        try FileManager.default.createDirectory(
            at: url,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: url.path
        )
    }

    func createPrivateRuntimeDirectory(_ url: URL) throws {
        let path = url.path
        var metadata = stat()
        if lstat(path, &metadata) == 0 {
            guard metadata.st_mode & S_IFMT == S_IFDIR else {
                throw AppPathsError.runtimePathNotDirectory(path)
            }
            guard metadata.st_uid == getuid() else {
                throw AppPathsError.runtimePathWrongOwner(path, metadata.st_uid, getuid())
            }
        } else if errno == ENOENT {
            try FileManager.default.createDirectory(
                at: url,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
        } else {
            throw AppPathsError.runtimePathStatFailed(path, errno)
        }

        if lstat(path, &metadata) != 0 {
            throw AppPathsError.runtimePathStatFailed(path, errno)
        }
        guard metadata.st_mode & S_IFMT == S_IFDIR else {
            throw AppPathsError.runtimePathNotDirectory(path)
        }
        guard metadata.st_uid == getuid() else {
            throw AppPathsError.runtimePathWrongOwner(path, metadata.st_uid, getuid())
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: path)
    }

    func validateSocketPathLength() throws {
        let limit = MemoryLayout.size(ofValue: sockaddr_un().sun_path)
        let actual = socketPath.path.utf8CString.count
        guard actual <= limit else {
            throw AppPathsError.socketPathTooLong(socketPath.path, actual, limit)
        }
    }

    func deleteStaleRecordings() -> [String] {
        let files: [URL]
        do {
            files = try FileManager.default.contentsOfDirectory(
                at: recordings,
                includingPropertiesForKeys: nil
            )
        } catch {
            return ["recording cleanup failed listing stale files: \(error.localizedDescription)"]
        }
        var warnings: [String] = []
        for file in files where file.lastPathComponent.hasPrefix("zw_tmp_") && file.pathExtension == "wav" {
            if let warning = removeRecording(file, context: "startup stale recording cleanup") {
                warnings.append(warning)
            }
        }
        return warnings
    }

    func removeRecording(_ url: URL, context: String) -> String? {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        do {
            try FileManager.default.removeItem(at: url)
            return nil
        } catch {
            return "\(context) failed: \(url.path): \(error.localizedDescription)"
        }
    }
}
