import Foundation
import CryptoKit
import Darwin

enum BackendInstallStatus: Equatable {
    case valid
    case invalid(String)
}

struct BackendBundleManifest: Decodable, Equatable {
    let protocolVersion: Int
    let backendVersion: String
    let backendCodeHash: String
    let wheelHash: String
    let registryHash: String
    let requirementsHash: String
    let venvManifestHash: String
    let pythonRuntimeManifestHash: String

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case backendVersion = "backend_version"
        case backendCodeHash = "backend_code_hash"
        case wheelHash = "wheel_hash"
        case registryHash = "registry_hash"
        case requirementsHash = "requirements_hash"
        case venvManifestHash = "venv_manifest_hash"
        case pythonRuntimeManifestHash = "python_runtime_manifest_hash"
    }
}

struct BackendInstallRecord: Decodable, Equatable {
    let protocolVersion: Int
    let backendVersion: String
    let pythonPath: String
    let pythonSource: String
    let pythonSourceSha256: String
    let pythonRuntimePrefix: String
    let pythonRuntimeManifestHash: String
    let backendCodeHash: String
    let wheelHash: String
    let registryHash: String
    let requirementsHash: String
    let venvManifestHash: String
    let pythonArch: String

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case backendVersion = "backend_version"
        case pythonPath = "python_path"
        case pythonSource = "python_source"
        case pythonSourceSha256 = "python_source_sha256"
        case pythonRuntimePrefix = "python_runtime_prefix"
        case pythonRuntimeManifestHash = "python_runtime_manifest_hash"
        case backendCodeHash = "backend_code_hash"
        case wheelHash = "wheel_hash"
        case registryHash = "registry_hash"
        case requirementsHash = "requirements_hash"
        case venvManifestHash = "venv_manifest_hash"
        case pythonArch = "python_arch"
    }
}

private struct BackendVenvManifest: Decodable, Equatable {
    let schemaVersion: Int
    let rootPath: String?
    let rootHash: String
    let files: [BackendVenvManifestEntry]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case rootPath = "root_path"
        case rootHash = "root_hash"
        case files
    }
}

struct BackendVenvManifestEntry: Decodable, Equatable {
    let path: String
    let kind: String
    let mode: String
    let sha256: String?
    let target: String?
}

private struct BackendPythonProbe: Decodable, Equatable {
    let protocolVersion: Int
    let backendVersion: String
    let pythonArch: String
    let registryHash: String
    let backendCodeHash: String

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case backendVersion = "backend_version"
        case pythonArch = "python_arch"
        case registryHash = "registry_hash"
        case backendCodeHash = "backend_code_hash"
    }
}

struct BackendInstallValidator {
    private static let perUserManifestHash = "per-user"

    let paths: AppPaths
    let bundleURL: URL

    var isDevBackendOverride: Bool {
        AppRuntimeIdentity.allowsDevBackendOverride
    }

    func validateInstallMetadata() -> BackendInstallStatus {
        if isDevBackendOverride {
            return .valid
        }
        do {
            let manifest = try loadManifest()
            let install = try loadInstallRecord()
            guard install.protocolVersion == manifest.protocolVersion else {
                return .invalid("protocol mismatch")
            }
            guard install.backendVersion == manifest.backendVersion else {
                return .invalid("backend version mismatch")
            }
            guard install.wheelHash == manifest.wheelHash else {
                return .invalid("backend wheel mismatch")
            }
            guard install.backendCodeHash == manifest.backendCodeHash else {
                return .invalid("backend code mismatch")
            }
            guard install.registryHash == manifest.registryHash else {
                return .invalid("registry mismatch")
            }
            guard install.requirementsHash == manifest.requirementsHash else {
                return .invalid("requirements mismatch")
            }
            guard install.pythonArch == "arm64" else {
                return .invalid("backend Python is not arm64")
            }
            guard URL(fileURLWithPath: install.pythonPath).standardizedFileURL.path
                == paths.backendPython.standardizedFileURL.path else {
                return .invalid("backend Python path mismatch")
            }
            guard FileManager.default.isExecutableFile(atPath: paths.backendPython.path) else {
                return .invalid("backend Python missing")
            }
            try validateStaticBackendInstall(manifest: manifest, install: install)
            let probe = try probeBackendPython()
            guard probe.protocolVersion == manifest.protocolVersion else {
                return .invalid("installed backend protocol mismatch")
            }
            guard probe.backendVersion == manifest.backendVersion else {
                return .invalid("installed backend version mismatch")
            }
            guard probe.pythonArch == "arm64" else {
                return .invalid("installed backend Python is not arm64")
            }
            guard probe.registryHash == manifest.registryHash else {
                return .invalid("installed backend registry mismatch")
            }
            guard probe.backendCodeHash == manifest.backendCodeHash else {
                return .invalid("installed backend code mismatch")
            }
            return .valid
        } catch {
            return .invalid(String(describing: error))
        }
    }

    func validateHealth(_ response: [String: Any]) -> BackendInstallStatus {
        if isDevBackendOverride {
            return .valid
        }
        do {
            let manifest = try loadManifest()
            guard response["protocol_version"] as? Int == manifest.protocolVersion else {
                return .invalid("backend protocol mismatch")
            }
            guard response["backend_version"] as? String == manifest.backendVersion else {
                return .invalid("backend version mismatch")
            }
            guard response["registry_hash"] as? String == manifest.registryHash else {
                return .invalid("backend registry mismatch")
            }
            return .valid
        } catch {
            return .invalid(String(describing: error))
        }
    }

    private func loadManifest() throws -> BackendBundleManifest {
        let url = bundleURL
            .appendingPathComponent("Contents/Resources/backend/BackendBundleManifest.json")
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(BackendBundleManifest.self, from: data)
    }

    private func loadInstallRecord() throws -> BackendInstallRecord {
        let url = paths.appSupport.appendingPathComponent("backend/install.json")
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(BackendInstallRecord.self, from: data)
    }

    private func validateStaticBackendInstall(
        manifest: BackendBundleManifest,
        install: BackendInstallRecord
    ) throws {
        let venvManifestURL = paths.appSupport.appendingPathComponent("backend/venv_manifest.json")
        let venvManifestData = try Data(contentsOf: venvManifestURL)
        let actualVenvManifestHash = sha256Hex(data: venvManifestData)
        if manifest.venvManifestHash == Self.perUserManifestHash {
            guard install.venvManifestHash == actualVenvManifestHash else {
                throw BackendInstallValidationError.staticValidationFailed("backend venv manifest record mismatch")
            }
        } else {
            guard install.venvManifestHash == manifest.venvManifestHash else {
                throw BackendInstallValidationError.staticValidationFailed("backend venv manifest is not sealed by signed app")
            }
        }
        guard actualVenvManifestHash == install.venvManifestHash else {
            throw BackendInstallValidationError.staticValidationFailed("backend venv manifest mismatch")
        }
        let venvManifest = try JSONDecoder().decode(BackendVenvManifest.self, from: venvManifestData)
        guard venvManifest.schemaVersion == 1, !venvManifest.rootHash.isEmpty else {
            throw BackendInstallValidationError.staticValidationFailed("unsupported backend venv manifest")
        }

        let venvRoot = paths.appSupport.appendingPathComponent("backend/.venv", isDirectory: true)
        let runtimeRoot = URL(fileURLWithPath: install.pythonRuntimePrefix)
            .resolvingSymlinksInPath()
            .standardizedFileURL
        let expectedEntries = try keyedVenvEntries(venvManifest.files)
        let actualEntries = try enumerateVenvEntries(
            root: venvRoot,
            allowedSymlinkRoots: [runtimeRoot],
            externalSymlinkMessage: "external backend venv symlink"
        )
        guard Set(expectedEntries.keys) == Set(actualEntries.keys) else {
            throw BackendInstallValidationError.staticValidationFailed("backend venv file set mismatch")
        }
        guard rootHash(entries: venvManifest.files) == venvManifest.rootHash,
              rootHash(entries: Array(actualEntries.values)) == venvManifest.rootHash else {
            throw BackendInstallValidationError.staticValidationFailed("backend venv root hash mismatch")
        }
        for (path, expected) in expectedEntries {
            guard isSafeRelativeManifestPath(path) else {
                throw BackendInstallValidationError.staticValidationFailed("unsafe backend venv manifest path")
            }
            guard let actual = actualEntries[path], expected == actual else {
                throw BackendInstallValidationError.staticValidationFailed("backend venv file hash mismatch")
            }
            try validatePythonStartupHook(path)
        }

        let expectedPythonSource = URL(fileURLWithPath: install.pythonSource)
            .resolvingSymlinksInPath()
            .standardizedFileURL
            .path
        let actualPythonSource = paths.backendPython
            .resolvingSymlinksInPath()
            .standardizedFileURL
            .path
        guard actualPythonSource == expectedPythonSource else {
            throw BackendInstallValidationError.staticValidationFailed("backend Python symlink target mismatch")
        }
        guard try sha256Hex(file: URL(fileURLWithPath: actualPythonSource)) == install.pythonSourceSha256 else {
            throw BackendInstallValidationError.staticValidationFailed("backend Python executable hash mismatch")
        }
        try validatePythonRuntimeInstall(manifest: manifest, install: install, actualPythonSource: actualPythonSource)

        let packageDir = try findBackendPackageDirectory(venvRoot: venvRoot)
        guard try backendCodeHash(packageDir: packageDir) == manifest.backendCodeHash else {
            throw BackendInstallValidationError.staticValidationFailed("static backend code hash mismatch")
        }
        let registryURL = packageDir
            .appendingPathComponent("resources", isDirectory: true)
            .appendingPathComponent("model_registry.json")
        guard try sha256Hex(file: registryURL) == manifest.registryHash else {
            throw BackendInstallValidationError.staticValidationFailed("static backend registry hash mismatch")
        }
    }

    private func validatePythonRuntimeInstall(
        manifest: BackendBundleManifest,
        install: BackendInstallRecord,
        actualPythonSource: String
    ) throws {
        let runtimeManifestURL = paths.appSupport.appendingPathComponent("backend/python_runtime_manifest.json")
        let runtimeManifestData = try Data(contentsOf: runtimeManifestURL)
        let actualRuntimeManifestHash = sha256Hex(data: runtimeManifestData)
        if manifest.pythonRuntimeManifestHash == Self.perUserManifestHash {
            guard install.pythonRuntimeManifestHash == actualRuntimeManifestHash else {
                throw BackendInstallValidationError.staticValidationFailed("Python runtime manifest record mismatch")
            }
        } else {
            guard install.pythonRuntimeManifestHash == manifest.pythonRuntimeManifestHash else {
                throw BackendInstallValidationError.staticValidationFailed("Python runtime manifest is not sealed by signed app")
            }
        }
        guard actualRuntimeManifestHash == install.pythonRuntimeManifestHash else {
            throw BackendInstallValidationError.staticValidationFailed("Python runtime manifest mismatch")
        }
        let runtimeManifest = try JSONDecoder().decode(BackendVenvManifest.self, from: runtimeManifestData)
        guard runtimeManifest.schemaVersion == 1,
              let rootPath = runtimeManifest.rootPath,
              !rootPath.isEmpty,
              !runtimeManifest.rootHash.isEmpty else {
            throw BackendInstallValidationError.staticValidationFailed("unsupported Python runtime manifest")
        }
        let runtimeRoot = URL(fileURLWithPath: install.pythonRuntimePrefix)
            .resolvingSymlinksInPath()
            .standardizedFileURL
        guard URL(fileURLWithPath: rootPath).resolvingSymlinksInPath().standardizedFileURL.path == runtimeRoot.path else {
            throw BackendInstallValidationError.staticValidationFailed("Python runtime root mismatch")
        }
        guard actualPythonSource == runtimeRoot.path || actualPythonSource.hasPrefix(runtimeRoot.path + "/") else {
            throw BackendInstallValidationError.staticValidationFailed("backend Python executable is outside sealed runtime")
        }

        let expectedEntries = try keyedVenvEntries(runtimeManifest.files)
        let actualEntries = try enumerateVenvEntries(
            root: runtimeRoot,
            rejectPythonBytecode: false,
            externalSymlinkMessage: "external Python runtime symlink"
        )
        guard Set(expectedEntries.keys) == Set(actualEntries.keys) else {
            throw BackendInstallValidationError.staticValidationFailed("Python runtime file set mismatch")
        }
        guard rootHash(entries: runtimeManifest.files) == runtimeManifest.rootHash,
              rootHash(entries: Array(actualEntries.values)) == runtimeManifest.rootHash else {
            throw BackendInstallValidationError.staticValidationFailed("Python runtime root hash mismatch")
        }
        for (path, expected) in expectedEntries {
            guard isSafeRelativeManifestPath(path) else {
                throw BackendInstallValidationError.staticValidationFailed("unsafe Python runtime manifest path")
            }
            guard let actual = actualEntries[path], expected == actual else {
                throw BackendInstallValidationError.staticValidationFailed("Python runtime file hash mismatch")
            }
        }
    }

    private func probeBackendPython() throws -> BackendPythonProbe {
        let process = Process()
        let output = Pipe()
        let error = Pipe()
        let finished = DispatchSemaphore(value: 0)
        process.executableURL = paths.backendPython
        process.arguments = [
            "-P",
            "-m",
            "zen_whisper_mac_backend.probe",
            "runtime-probe"
        ]
        process.environment = ProcessEnvironment.backendProbe(appSupport: paths.appSupport)
        process.standardOutput = output
        process.standardError = error
        process.terminationHandler = { _ in
            finished.signal()
        }
        try process.run()
        if finished.wait(timeout: .now() + .seconds(8)) == .timedOut {
            process.terminate()
            _ = finished.wait(timeout: .now() + .seconds(2))
            throw BackendInstallValidationError.probeTimedOut
        }
        guard process.terminationStatus == 0 else {
            let data = error.fileHandleForReading.readDataToEndOfFile()
            let message = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw BackendInstallValidationError.probeFailed(message ?? "unknown")
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        return try JSONDecoder().decode(BackendPythonProbe.self, from: data)
    }
}

enum BackendInstallValidationError: Error {
    case probeFailed(String)
    case probeTimedOut
    case staticValidationFailed(String)
}

private func enumerateVenvEntries(
    root: URL,
    rejectPythonBytecode: Bool = true,
    allowedSymlinkRoots: [URL] = [],
    externalSymlinkMessage: String = "external backend venv symlink"
) throws -> [String: BackendVenvManifestEntry] {
    let fileManager = FileManager.default
    guard let enumerator = fileManager.enumerator(
        at: root,
        includingPropertiesForKeys: [.isDirectoryKey],
        options: []
    ) else {
        throw BackendInstallValidationError.staticValidationFailed("backend venv is not readable")
    }

    var entries: [String: BackendVenvManifestEntry] = [:]
    for case let url as URL in enumerator {
        let path = relativePath(url, root: root)
        if rejectPythonBytecode, isPythonBytecodePath(path) {
            throw BackendInstallValidationError.staticValidationFailed("unexpected Python bytecode in backend venv")
        }
        let kind = try fileKind(url)
        if kind == .directory {
            continue
        }
        if kind == .symlink,
           let target = try? fileManager.destinationOfSymbolicLink(atPath: url.path) {
            if !symlinkTargetIsInsideAllowedRoots(
                target,
                link: url,
                root: root,
                allowedRoots: allowedSymlinkRoots
            ) {
                throw BackendInstallValidationError.staticValidationFailed(externalSymlinkMessage)
            }
            entries[path] = BackendVenvManifestEntry(
                path: path,
                kind: "symlink",
                mode: try posixModeString(url),
                sha256: nil,
                target: target
            )
            continue
        }
        guard kind == .file else {
            throw BackendInstallValidationError.staticValidationFailed("unsupported backend venv entry")
        }
        guard fileManager.fileExists(atPath: url.path) else {
            throw BackendInstallValidationError.staticValidationFailed("backend venv entry missing")
        }
        entries[path] = BackendVenvManifestEntry(
            path: path,
            kind: "file",
            mode: try posixModeString(url),
            sha256: try sha256Hex(file: url),
            target: nil
        )
    }
    return entries
}

private func keyedVenvEntries(
    _ entries: [BackendVenvManifestEntry]
) throws -> [String: BackendVenvManifestEntry] {
    var keyed: [String: BackendVenvManifestEntry] = [:]
    for entry in entries {
        if keyed[entry.path] != nil {
            throw BackendInstallValidationError.staticValidationFailed("duplicate backend venv manifest path")
        }
        keyed[entry.path] = entry
    }
    return keyed
}

private enum FilesystemEntryKind {
    case file
    case directory
    case symlink
    case other
}

private func fileKind(_ url: URL) throws -> FilesystemEntryKind {
    var info = stat()
    guard lstat(url.path, &info) == 0 else {
        throw BackendInstallValidationError.staticValidationFailed("backend venv entry stat failed")
    }
    let type = info.st_mode & S_IFMT
    if type == S_IFLNK {
        return .symlink
    }
    if type == S_IFDIR {
        return .directory
    }
    if type == S_IFREG {
        return .file
    }
    return .other
}

private func posixModeString(_ url: URL) throws -> String {
    var info = stat()
    guard lstat(url.path, &info) == 0 else {
        throw BackendInstallValidationError.staticValidationFailed("backend venv entry mode failed")
    }
    return String(format: "0o%o", info.st_mode & 0o7777)
}

func rootHash(entries: [BackendVenvManifestEntry]) -> String {
    var digest = SHA256()
    for entry in entries.sorted(by: { $0.path < $1.path }) {
        digest.update(data: Data(canonicalJSONString(entry).utf8))
        digest.update(data: Data("\n".utf8))
    }
    return digest.finalize().map { String(format: "%02x", $0) }.joined()
}

private func canonicalJSONString(_ entry: BackendVenvManifestEntry) -> String {
    if entry.kind == "symlink" {
        return "{\"kind\":\"\(jsonEscaped(entry.kind))\",\"mode\":\"\(jsonEscaped(entry.mode))\",\"path\":\"\(jsonEscaped(entry.path))\",\"target\":\"\(jsonEscaped(entry.target ?? ""))\"}"
    }
    return "{\"kind\":\"\(jsonEscaped(entry.kind))\",\"mode\":\"\(jsonEscaped(entry.mode))\",\"path\":\"\(jsonEscaped(entry.path))\",\"sha256\":\"\(jsonEscaped(entry.sha256 ?? ""))\"}"
}

func jsonEscaped(_ value: String) -> String {
    var output = ""
    for scalar in value.unicodeScalars {
        switch scalar {
        case "\"":
            output += "\\\""
        case "\\":
            output += "\\\\"
        case "\u{08}":
            output += "\\b"
        case "\u{0C}":
            output += "\\f"
        case "\n":
            output += "\\n"
        case "\r":
            output += "\\r"
        case "\t":
            output += "\\t"
        default:
            if scalar.value < 0x20 {
                output += String(format: "\\u%04x", scalar.value)
            } else if scalar.value > 0xFFFF {
                let value = scalar.value - 0x1_0000
                let high = 0xD800 + ((value >> 10) & 0x3FF)
                let low = 0xDC00 + (value & 0x3FF)
                output += String(format: "\\u%04x\\u%04x", high, low)
            } else if scalar.value >= 0x7F {
                output += String(format: "\\u%04x", scalar.value)
            } else {
                output.unicodeScalars.append(scalar)
            }
        }
    }
    return output
}

private func findBackendPackageDirectory(venvRoot: URL) throws -> URL {
    let lib = venvRoot.appendingPathComponent("lib", isDirectory: true)
    let fileManager = FileManager.default
    let pythonDirs = try fileManager.contentsOfDirectory(
        at: lib,
        includingPropertiesForKeys: [.isDirectoryKey],
        options: []
    ).filter { url in
        url.lastPathComponent.hasPrefix("python")
    }
    for pythonDir in pythonDirs {
        let package = pythonDir
            .appendingPathComponent("site-packages", isDirectory: true)
            .appendingPathComponent("zen_whisper_mac_backend", isDirectory: true)
        if fileManager.fileExists(atPath: package.path) {
            return package
        }
    }
    throw BackendInstallValidationError.staticValidationFailed("backend package missing")
}

private func backendCodeHash(packageDir: URL) throws -> String {
    let fileManager = FileManager.default
    guard let enumerator = fileManager.enumerator(
        at: packageDir,
        includingPropertiesForKeys: [.isDirectoryKey],
        options: []
    ) else {
        throw BackendInstallValidationError.staticValidationFailed("backend package is not readable")
    }

    var entries: [(String, String)] = []
    for case let url as URL in enumerator {
        let path = relativePath(url, root: packageDir)
        if shouldSkipBackendCodeHashPath(path) {
            if (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
                enumerator.skipDescendants()
            }
            continue
        }
        if (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
            continue
        }
        let relativeName = "zen_whisper_mac_backend/" + path
        entries.append((relativeName, try sha256Hex(file: url)))
    }

    var digest = SHA256()
    for (name, fileHash) in entries.sorted(by: { $0.0 < $1.0 }) {
        digest.update(data: Data("\(name)\0\(fileHash)\n".utf8))
    }
    return digest.finalize().map { String(format: "%02x", $0) }.joined()
}

private func sha256Hex(file url: URL) throws -> String {
    let data = try Data(contentsOf: url)
    return sha256Hex(data: data)
}

private func sha256Hex(data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func relativePath(_ url: URL, root: URL) -> String {
    let rootPath = root.standardizedFileURL.path
    let path = url.standardizedFileURL.path
    let prefix = rootPath.hasSuffix("/") ? rootPath : rootPath + "/"
    if path.hasPrefix(prefix) {
        return String(path.dropFirst(prefix.count))
    }
    return url.lastPathComponent
}

private func shouldSkipBackendCodeHashPath(_ path: String) -> Bool {
    path.split(separator: "/").contains("__pycache__") || path.hasSuffix(".pyc")
}

private func isPythonBytecodePath(_ path: String) -> Bool {
    path.split(separator: "/").contains("__pycache__") || path.hasSuffix(".pyc")
}

private func isSafeRelativeManifestPath(_ path: String) -> Bool {
    !path.isEmpty
        && !path.hasPrefix("/")
        && !path.split(separator: "/").contains("..")
}

private func symlinkTargetIsInsideAllowedRoots(
    _ target: String,
    link: URL,
    root: URL,
    allowedRoots: [URL]
) -> Bool {
    let targetURL: URL
    if target.hasPrefix("/") {
        targetURL = URL(fileURLWithPath: target)
    } else {
        targetURL = link.deletingLastPathComponent().appendingPathComponent(target)
    }
    let targetPath = targetURL
        .resolvingSymlinksInPath()
        .standardizedFileURL
        .path
    guard FileManager.default.fileExists(atPath: targetPath) else {
        return false
    }
    for allowedRoot in [root] + allowedRoots {
        let rootPath = allowedRoot
            .resolvingSymlinksInPath()
            .standardizedFileURL
            .path
        if targetPath == rootPath || targetPath.hasPrefix(rootPath + "/") {
            return true
        }
    }
    return false
}

private func validatePythonStartupHook(_ path: String) throws {
    let fileName = String(path.split(separator: "/").last ?? "")
    if fileName == "sitecustomize.py" || fileName == "usercustomize.py" {
        throw BackendInstallValidationError.staticValidationFailed("unexpected Python startup hook")
    }
    let allowedPthFiles: Set<String> = [
        "_virtualenv.pth",
        "distutils-precedence.pth"
    ]
    if fileName.hasSuffix(".pth") && !allowedPthFiles.contains(fileName) {
        throw BackendInstallValidationError.staticValidationFailed("unexpected Python .pth startup hook")
    }
}
