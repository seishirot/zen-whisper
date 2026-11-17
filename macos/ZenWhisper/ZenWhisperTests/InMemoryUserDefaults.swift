import Foundation

/// A `UserDefaults` that keeps values in memory only.
///
/// `UserDefaults(suiteName:)` goes through cfprefsd, which leaves an empty
/// `~/Library/Preferences/<suite>.plist` behind even after
/// `removePersistentDomain(forName:)`, so tests use this instead.
final class InMemoryUserDefaults: UserDefaults {
    private var storage: [String: Any] = [:]

    init() {
        // A nil suite name refers to the process's own domain; every accessor
        // used by the code under test is overridden below, so it is never
        // read or written.
        super.init(suiteName: nil)!
    }

    override func object(forKey defaultName: String) -> Any? {
        storage[defaultName]
    }

    override func set(_ value: Any?, forKey defaultName: String) {
        if let value {
            storage[defaultName] = value
        } else {
            storage.removeValue(forKey: defaultName)
        }
    }

    override func removeObject(forKey defaultName: String) {
        storage.removeValue(forKey: defaultName)
    }

    override func set(_ value: Bool, forKey defaultName: String) {
        storage[defaultName] = value
    }

    override func set(_ value: Int, forKey defaultName: String) {
        storage[defaultName] = value
    }

    override func set(_ value: Double, forKey defaultName: String) {
        storage[defaultName] = value
    }

    override func set(_ value: Float, forKey defaultName: String) {
        storage[defaultName] = value
    }

    override func set(_ url: URL?, forKey defaultName: String) {
        set(url as Any?, forKey: defaultName)
    }

    override func string(forKey defaultName: String) -> String? {
        storage[defaultName] as? String
    }

    override func array(forKey defaultName: String) -> [Any]? {
        storage[defaultName] as? [Any]
    }

    override func dictionary(forKey defaultName: String) -> [String: Any]? {
        storage[defaultName] as? [String: Any]
    }

    override func data(forKey defaultName: String) -> Data? {
        storage[defaultName] as? Data
    }

    override func stringArray(forKey defaultName: String) -> [String]? {
        storage[defaultName] as? [String]
    }

    override func bool(forKey defaultName: String) -> Bool {
        (storage[defaultName] as? NSNumber)?.boolValue ?? false
    }

    override func integer(forKey defaultName: String) -> Int {
        (storage[defaultName] as? NSNumber)?.intValue ?? 0
    }

    override func double(forKey defaultName: String) -> Double {
        (storage[defaultName] as? NSNumber)?.doubleValue ?? 0
    }

    override func float(forKey defaultName: String) -> Float {
        (storage[defaultName] as? NSNumber)?.floatValue ?? 0
    }

    override func url(forKey defaultName: String) -> URL? {
        storage[defaultName] as? URL
    }

    override func dictionaryRepresentation() -> [String: Any] {
        storage
    }
}
