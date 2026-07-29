import Foundation

enum ProcessEnvironment {
    private static let systemPath = "/usr/bin:/bin:/usr/sbin:/sbin"

    static func base(appSupport: URL) -> [String: String] {
        var environment: [String: String] = [
            "HOME": NSHomeDirectory(),
            "PATH": systemPath,
            "ZEN_WHISPER_APP_SUPPORT": appSupport.path
        ]
        if let tmpdir = ProcessInfo.processInfo.environment["TMPDIR"], !tmpdir.isEmpty {
            environment["TMPDIR"] = tmpdir
        }
        return environment
    }

    static func backend(appSupport: URL, huggingFaceHome: URL) -> [String: String] {
        var environment = base(appSupport: appSupport)
        environment["HF_HOME"] = huggingFaceHome.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONSAFEPATH"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["ZEN_WHISPER_CLI_PATH"] = cliExecutablePath()
        return environment
    }

    static func backendProbe(appSupport: URL) -> [String: String] {
        var environment = base(appSupport: appSupport)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONSAFEPATH"] = "1"
        return environment
    }

    static func backendRepair(appSupport: URL) -> [String: String] {
        var environment = backendProbe(appSupport: appSupport)
        environment["PATH"] = repairPath()
        environment["ZEN_WHISPER_ALLOW_BUNDLED_TOOLCHAIN"] = "1"
        environment["ZEN_WHISPER_ALLOW_RECORDED_TOOLCHAIN"] = "1"
        return environment
    }

    private static func repairPath() -> String {
        [
            systemPath,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "\(NSHomeDirectory())/.local/bin",
            "\(NSHomeDirectory())/.local/share/mise/bin"
        ].joined(separator: ":")
    }

    static func cliExecutablePath(
        homeDirectory: String = NSHomeDirectory()
    ) -> String {
        [
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/local/sbin",
            "\(homeDirectory)/.local/bin",
            "\(homeDirectory)/bin",
            "\(homeDirectory)/.local/share/mise/shims",
            "\(homeDirectory)/.local/share/mise/bin"
        ].joined(separator: ":")
    }
}
