import Foundation

struct AppPaths {
    let appSupport: URL
    let logs: URL

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

    var socketPath: URL {
        appSupport.appendingPathComponent("run/b.sock")
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

    func prepare() throws {
        try createPrivateDirectory(appSupport)
        try createPrivateDirectory(logs)
        try createPrivateDirectory(appSupport.appendingPathComponent("run", isDirectory: true))
        try createPrivateDirectory(recordings)
        try createPrivateDirectory(huggingFaceHome)
        deleteStaleRecordings()
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

    func deleteStaleRecordings() {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: recordings,
            includingPropertiesForKeys: nil
        ) else {
            return
        }
        for file in files where file.lastPathComponent.hasPrefix("zw_tmp_") && file.pathExtension == "wav" {
            try? FileManager.default.removeItem(at: file)
        }
    }
}
