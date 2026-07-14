import Foundation

struct AppRuntimeIdentity {
    static let dailyBundleIdentifier = "com.seishirot.zenwhisper"
    static let dailyAppPath = "/Applications/zen-whisper.app"

    static var currentBundlePath: String {
        Bundle.main.bundleURL.standardizedFileURL.path
    }

    static var buildChannel: String {
        Bundle.main.object(forInfoDictionaryKey: "ZWBuildChannel") as? String ?? ""
    }

    static var isDailyContext: Bool {
        currentBundlePath == dailyAppPath
            || buildChannel == "daily"
            || Bundle.main.bundleIdentifier == dailyBundleIdentifier
    }

    static var allowsDevBackendOverride: Bool {
        let environment = ProcessInfo.processInfo.environment
        guard !isDailyContext else {
            return false
        }
        guard environment["ZEN_WHISPER_ENABLE_DEV_MODE"] == "1" || buildChannel == "dev" else {
            return false
        }
        return environment["ZEN_WHISPER_BACKEND_PYTHON"]?.isEmpty == false
    }
}
