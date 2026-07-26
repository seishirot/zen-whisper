import CryptoKit
import Foundation

enum EnhancementCatalogLimits {
    static let maximumProfileTermCount = 256
    static let maximumAliasesPerTerm = 128
    static let maximumPresetArgumentCount = 256
    static let maximumPresetEnvironmentEntryCount = 128
    static let maximumContextUTF8Bytes = 64 * 1024
    static let maximumStringUTF8Bytes = 8 * 1024
    static let maximumSerializedPayloadBytes = 256 * 1024
    static let maximumEnhancementValueCount = 4_096
    static let maximumCatalogDocumentBytes = 1024 * 1024
    static let maximumPresetTimeoutSeconds: Double = 300

    static func enhancementPayloadFitsBudget(
        profile: EnhancementProfile?,
        preset: EnhancementPostprocessorPreset?,
        includesDictionaryPostprocessor: Bool = false
    ) -> Bool {
        var payload: [String: Any] = [:]
        if let profile {
            payload["profile"] = profile.backendPayload
        }
        if let preset {
            payload["postprocessor"] = [
                "mode": "preset",
                "preset": preset.backendPayload
            ]
        } else if includesDictionaryPostprocessor {
            payload["postprocessor"] = ["mode": "dictionary"]
        }
        return jsonPayloadFitsBudget(payload)
    }

    static func jsonPayloadFitsBudget(_ payload: Any) -> Bool {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(
                  withJSONObject: payload,
                  options: [.sortedKeys]
              ),
              data.count <= maximumSerializedPayloadBytes else {
            return false
        }

        var count = 0
        var stack: [Any] = [payload]
        while let current = stack.popLast() {
            count += 1
            guard count <= maximumEnhancementValueCount else {
                return false
            }
            if let dictionary = current as? NSDictionary {
                count += dictionary.count
                guard count <= maximumEnhancementValueCount else {
                    return false
                }
                stack.append(contentsOf: dictionary.allValues)
            } else if let array = current as? NSArray {
                count += array.count
                guard count <= maximumEnhancementValueCount else {
                    return false
                }
                stack.append(contentsOf: array)
            }
        }
        return true
    }
}

enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else {
            return nil
        }
        return value
    }

    var stringValue: String? {
        guard case .string(let value) = self else {
            return nil
        }
        return value
    }

    var arrayValue: [JSONValue]? {
        guard case .array(let value) = self else {
            return nil
        }
        return value
    }

    var boolValue: Bool? {
        guard case .bool(let value) = self else {
            return nil
        }
        return value
    }

    var numberValue: Double? {
        guard case .number(let value) = self else {
            return nil
        }
        return value
    }

    var foundationValue: Any {
        switch self {
        case .object(let value):
            return value.mapValues(\.foundationValue)
        case .array(let value):
            return value.map(\.foundationValue)
        case .string(let value):
            return value
        case .number(let value):
            return value
        case .bool(let value):
            return value
        case .null:
            return NSNull()
        }
    }
}

enum PostprocessingSelection: Equatable, Codable, Sendable {
    case off
    case dictionary
    case preset(String)

    init(storageValue: String?) {
        let normalized = storageValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        switch normalized {
        case "", "off":
            self = .off
        case "dictionary":
            self = .dictionary
        default:
            self = .preset(normalized)
        }
    }

    var storageValue: String {
        switch self {
        case .off:
            return "off"
        case .dictionary:
            return "dictionary"
        case .preset(let id):
            return id
        }
    }

    var isCLI: Bool {
        if case .preset = self {
            return true
        }
        return false
    }

    var presetID: String? {
        guard case .preset(let id) = self else {
            return nil
        }
        return id
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(storageValue: try container.decode(String.self))
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(storageValue)
    }
}

struct EnhancementSelection: Equatable, Codable, Sendable {
    var profileID: String?
    var postprocessing: PostprocessingSelection
    var approvedPostprocessorRevision: String?

    init(
        profileID: String? = nil,
        postprocessing: PostprocessingSelection = .off,
        approvedPostprocessorRevision: String? = nil
    ) {
        let normalizedProfileID = profileID?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.profileID = normalizedProfileID?.isEmpty == false ? normalizedProfileID : nil
        self.postprocessing = postprocessing
        let normalizedRevision = approvedPostprocessorRevision?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        self.approvedPostprocessorRevision =
            postprocessing.isCLI && normalizedRevision?.isEmpty == false
            ? normalizedRevision
            : nil
    }

    static let off = EnhancementSelection()
}

struct EnhancementTerm: Equatable, Sendable {
    var canonical: String
    var spoken: [String]
    var replaceFrom: [String]
    var description: String
    var extraFields: [String: JSONValue]

    init(
        canonical: String,
        spoken: [String] = [],
        replaceFrom: [String] = [],
        description: String = "",
        extraFields: [String: JSONValue] = [:]
    ) {
        self.canonical = canonical
        self.spoken = spoken
        self.replaceFrom = replaceFrom
        self.description = description
        self.extraFields = extraFields
    }

    var backendPayload: [String: Any] {
        [
            "canonical": canonical,
            "spoken": spoken,
            "replace_from": replaceFrom,
            "description": description
        ]
    }
}

struct EnhancementProfile: Equatable, Sendable {
    var id: String
    var name: String
    var context: String
    var terms: [EnhancementTerm]
    var extraFields: [String: JSONValue]

    init(
        id: String,
        name: String,
        context: String = "",
        terms: [EnhancementTerm] = [],
        extraFields: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.name = name
        self.context = context
        self.terms = terms
        self.extraFields = extraFields
    }

    var backendPayload: [String: Any] {
        [
            "id": id,
            "name": name,
            "context": context,
            "terms": terms.map(\.backendPayload)
        ]
    }
}

enum EnhancementPostprocessorInputMode: String, CaseIterable, Codable, Sendable {
    case stdin
    case argument
}

enum EnhancementDataDestination: String, CaseIterable, Codable, Sendable {
    case local
    case remote
    case unknown
}

struct EnhancementPostprocessorPreset: Equatable, Sendable {
    var id: String
    var displayName: String
    var executable: String
    var arguments: [String]
    var preflightExecutable: String
    var preflightArguments: [String]
    var preflightFailureMessage: String
    var inputMode: EnhancementPostprocessorInputMode
    var destination: EnhancementDataDestination
    var timeoutSeconds: Double
    var promptTemplate: String
    var environment: [String: String]
    var destinationReviewRevision: String?
    var extraFields: [String: JSONValue]

    init(
        id: String,
        displayName: String,
        executable: String,
        arguments: [String] = [],
        preflightExecutable: String = "",
        preflightArguments: [String] = [],
        preflightFailureMessage: String = "",
        inputMode: EnhancementPostprocessorInputMode = .stdin,
        destination: EnhancementDataDestination = .unknown,
        timeoutSeconds: Double = 30,
        promptTemplate: String = "{{transcript}}",
        environment: [String: String] = [:],
        destinationReviewRevision: String? = nil,
        extraFields: [String: JSONValue] = [:]
    ) {
        self.id = id
        self.displayName = displayName
        self.executable = executable
        self.arguments = arguments
        self.preflightExecutable = preflightExecutable
        self.preflightArguments = preflightArguments
        self.preflightFailureMessage = preflightFailureMessage
        self.inputMode = inputMode
        self.destination = destination
        self.timeoutSeconds = timeoutSeconds
        self.promptTemplate = promptTemplate
        self.environment = environment
        let normalizedDestinationReviewRevision = destinationReviewRevision?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        self.destinationReviewRevision =
            normalizedDestinationReviewRevision?.isEmpty == false
            ? normalizedDestinationReviewRevision
            : nil
        self.extraFields = extraFields
    }

    var backendPayload: [String: Any] {
        var payload: [String: Any] = [
            "id": id,
            "display_name": displayName,
            "executable": executable,
            "arguments": arguments,
            "preflight_arguments": preflightArguments,
            "preflight_failure_message": preflightFailureMessage,
            "input_mode": inputMode.rawValue,
            "output_mode": "stdout",
            "timeout_sec": timeoutSeconds,
            "data_destination": destination.rawValue,
            "prompt_template": promptTemplate,
            "environment": environment
        ]
        if !preflightExecutable.isEmpty {
            payload["preflight_executable"] = preflightExecutable
        }
        return payload
    }

    var reviewRevision: String {
        Self.revision(for: backendPayload)
    }

    /// Identifies every field that can change which process receives profile
    /// data or how that process is launched. A local destination
    /// reclassification is valid only for this exact normalized definition.
    var commandRevision: String {
        Self.revision(
            for: [
                "executable": executable,
                "arguments": arguments,
                "preflight_executable": preflightExecutable,
                "preflight_arguments": preflightArguments,
                "input_mode": inputMode.rawValue,
                "environment": environment
            ]
        )
    }

    private static func revision(for value: Any) -> String {
        let data = try? JSONSerialization.data(
            withJSONObject: value,
            options: [.sortedKeys]
        )
        let digest = SHA256.hash(data: data ?? Data())
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }
}

struct EnhancementFileFingerprint: Equatable, Codable, Sendable {
    let exists: Bool
    let byteCount: UInt64
    let contentSHA256: String?

    static let missing = EnhancementFileFingerprint(
        exists: false,
        byteCount: 0,
        contentSHA256: nil
    )
}

struct EnhancementCatalogFingerprints: Equatable, Sendable {
    var profiles: [String: EnhancementFileFingerprint]
    var postprocessors: EnhancementFileFingerprint

    init(
        profiles: [String: EnhancementFileFingerprint] = [:],
        postprocessors: EnhancementFileFingerprint = .missing
    ) {
        self.profiles = profiles
        self.postprocessors = postprocessors
    }
}

struct EnhancementCatalogIssue: Equatable, Sendable {
    enum Source: Equatable, Sendable {
        case bundledPostprocessors
        case profileFile(String)
        case localPostprocessors
        case postprocessor(String)
    }

    enum Reason: String, Equatable, Sendable {
        case unreadable
        case malformedJSON
        case unsupportedVersion
        case invalidIdentifier
        case invalidField
        case duplicateReplacement
        case disabled
    }

    let source: Source
    let reason: Reason
    let field: String?

    init(source: Source, reason: Reason, field: String? = nil) {
        self.source = source
        self.reason = reason
        self.field = field
    }
}

struct EnhancementCatalogSnapshot: Equatable, Sendable {
    var profiles: [String: EnhancementProfile]
    var postprocessors: [String: EnhancementPostprocessorPreset]
    var localPostprocessorIDs: Set<String>
    var blockedPostprocessorIDs: Set<String>
    var blocksAllPostprocessors: Bool
    var issues: [EnhancementCatalogIssue]
    var fingerprints: EnhancementCatalogFingerprints

    init(
        profiles: [String: EnhancementProfile] = [:],
        postprocessors: [String: EnhancementPostprocessorPreset] = [:],
        localPostprocessorIDs: Set<String> = [],
        blockedPostprocessorIDs: Set<String> = [],
        blocksAllPostprocessors: Bool = false,
        issues: [EnhancementCatalogIssue] = [],
        fingerprints: EnhancementCatalogFingerprints = EnhancementCatalogFingerprints()
    ) {
        self.profiles = profiles
        self.postprocessors = postprocessors
        self.localPostprocessorIDs = localPostprocessorIDs
        self.blockedPostprocessorIDs = blockedPostprocessorIDs
        self.blocksAllPostprocessors = blocksAllPostprocessors
        self.issues = issues
        self.fingerprints = fingerprints
    }
}

struct EnhancementPostprocessorSaveResult: Equatable, Sendable {
    let preset: EnhancementPostprocessorPreset
    let fingerprint: EnhancementFileFingerprint
}

enum EnhancementCatalogError: Error, Equatable, LocalizedError, Sendable {
    case invalidProfile(field: String)
    case invalidPostprocessor(field: String)
    case malformedExistingFile(String)
    case fingerprintConflict(String)
    case missingBundledCatalog
    case fileOperation(String)

    var errorDescription: String? {
        switch self {
        case .invalidProfile(let field):
            return "The profile has an invalid \(field) field."
        case .invalidPostprocessor(let field):
            return "The postprocessor has an invalid \(field) field."
        case .malformedExistingFile(let file):
            return "\(file) is malformed and was not overwritten."
        case .fingerprintConflict(let file):
            return "\(file) changed outside the settings window. Reload it before saving."
        case .missingBundledCatalog:
            return "The bundled postprocessor catalog is missing."
        case .fileOperation(let operation):
            return "The enhancement catalog could not complete \(operation)."
        }
    }
}
