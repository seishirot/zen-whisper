import Darwin
import Foundation

enum BackendRepairRunnerError: Error, CustomStringConvertible, Equatable {
    case installerMissing(String)
    case installerNotExecutable(String)
    case failed(Int32)
    case timedOut
    case didNotExitAfterTimeout

    var description: String {
        switch self {
        case .installerMissing(let path):
            return "backend installer missing: \(path)"
        case .installerNotExecutable(let path):
            return "backend installer is not executable: \(path)"
        case .failed(let status):
            return "backend repair failed with exit status \(status)"
        case .timedOut:
            return "backend repair timed out"
        case .didNotExitAfterTimeout:
            return "backend repair did not exit after timeout"
        }
    }
}

struct BackendRepairRunner {
    let paths: AppPaths
    let bundleURL: URL
    var timeoutSeconds: Int = 20 * 60

    func repair() throws {
        let installer = bundleURL
            .appendingPathComponent("Contents/Resources/backend/install_backend_from_app.sh")
        let fm = FileManager.default
        guard fm.fileExists(atPath: installer.path) else {
            throw BackendRepairRunnerError.installerMissing(installer.path)
        }
        guard fm.isExecutableFile(atPath: installer.path) else {
            throw BackendRepairRunnerError.installerNotExecutable(installer.path)
        }

        try paths.prepare()
        let logURL = paths.logs.appendingPathComponent("backend-repair.log")
        _ = fm.createFile(atPath: logURL.path, contents: nil)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: logURL.path)
        let logHandle = try FileHandle(forWritingTo: logURL)
        defer {
            do {
                try logHandle.close()
            } catch {
                NSLog("zen-whisper: could not close backend repair log %@: %@", logURL.path, String(describing: error))
            }
        }
        try logHandle.seekToEnd()
        writeRepairLog(
            "\n=== backend repair \(ISO8601DateFormatter().string(from: Date())) ===\n",
            to: logHandle,
            logURL: logURL
        )

        let process = Process()
        process.executableURL = installer
        process.arguments = [bundleURL.path]
        process.environment = ProcessEnvironment.backendRepair(appSupport: paths.appSupport)
        process.standardOutput = logHandle
        process.standardError = logHandle

        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            finished.signal()
        }
        try process.run()
        if finished.wait(timeout: .now() + .seconds(timeoutSeconds)) == .timedOut {
            writeRepairLog("backend repair timed out; terminating installer\n", to: logHandle, logURL: logURL)
            process.terminate()
            if finished.wait(timeout: .now() + .seconds(10)) == .timedOut {
                writeRepairLog("installer ignored terminate; sending SIGKILL\n", to: logHandle, logURL: logURL)
                kill(process.processIdentifier, SIGKILL)
                if finished.wait(timeout: .now() + .seconds(5)) == .timedOut {
                    writeRepairLog("installer did not exit after SIGKILL\n", to: logHandle, logURL: logURL)
                    throw BackendRepairRunnerError.didNotExitAfterTimeout
                }
            }
            throw BackendRepairRunnerError.timedOut
        }
        guard process.terminationStatus == 0 else {
            throw BackendRepairRunnerError.failed(process.terminationStatus)
        }
    }

    private func writeRepairLog(_ message: String, to handle: FileHandle, logURL: URL) {
        do {
            try handle.write(contentsOf: Data(message.utf8))
        } catch {
            NSLog("zen-whisper: could not write backend repair log %@: %@", logURL.path, String(describing: error))
        }
    }
}
