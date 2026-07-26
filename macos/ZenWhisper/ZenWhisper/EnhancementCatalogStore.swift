import CryptoKit
import Foundation

private enum EnhancementValidationFailure: Error {
    case unsupportedVersion
    case invalidIdentifier(String)
    case invalidField(String)
    case duplicateReplacement(String)
}

private struct RawPostprocessorDocument {
    var root: [String: JSONValue]
    var postprocessors: [String: [String: JSONValue]]
    var invalidEntryIDs: [String] = []
}

final class EnhancementCatalogStore {
    private static let supportedPlaceholders: Set<String> = [
        "prompt",
        "transcript",
        "context",
        "terms",
        "profile_name",
        "language"
    ]

    private static let profileKnownFields: Set<String> = [
        "version",
        "profile_id",
        "name",
        "context",
        "terms"
    ]

    private static let termKnownFields: Set<String> = [
        "canonical",
        "spoken",
        "replace_from",
        "description"
    ]

    private static let postprocessorKnownFields: Set<String> = [
        "display_name",
        "executable",
        "arguments",
        "preflight_executable",
        "preflight_arguments",
        "preflight_failure_message",
        "input_mode",
        "data_destination",
        "timeout_sec",
        "prompt_template",
        "environment",
        "destination_review_revision",
        "enabled"
    ]

    let paths: AppPaths
    private let bundledPostprocessorsURL: URL?
    private let fileManager: FileManager

    init(
        paths: AppPaths,
        bundledPostprocessorsURL: URL? = nil,
        fileManager: FileManager = .default
    ) {
        self.paths = paths
        self.bundledPostprocessorsURL = bundledPostprocessorsURL
            ?? Self.defaultBundledPostprocessorsURL()
        self.fileManager = fileManager
    }

    func load() -> EnhancementCatalogSnapshot {
        var issues: [EnhancementCatalogIssue] = []
        var profiles: [String: EnhancementProfile] = [:]
        var profileFingerprints: [String: EnhancementFileFingerprint] = [:]

        if fileManager.fileExists(atPath: paths.profilesDirectory.path) {
            do {
                let files = try fileManager.contentsOfDirectory(
                    at: paths.profilesDirectory,
                    includingPropertiesForKeys: nil,
                    options: [.skipsHiddenFiles]
                )
                for url in files
                    .filter({ $0.pathExtension.lowercased() == "json" })
                    .sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                    let profileID = url.deletingPathExtension().lastPathComponent
                    let profileFingerprint = (try? fingerprint(at: url)) ?? .missing
                    profileFingerprints[profileID] = profileFingerprint
                    if profileFingerprint.byteCount
                        > UInt64(
                            EnhancementCatalogLimits.maximumCatalogDocumentBytes
                        ) {
                        issues.append(
                            EnhancementCatalogIssue(
                                source: .profileFile(url.lastPathComponent),
                                reason: .invalidField,
                                field: "payload"
                            )
                        )
                        continue
                    }
                    do {
                        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
                        let profile = try Self.parseProfile(
                            data: data,
                            expectedID: profileID
                        )
                        profiles[profile.id] = profile
                    } catch {
                        issues.append(Self.profileIssue(for: error, filename: url.lastPathComponent))
                    }
                }
            } catch {
                issues.append(
                    EnhancementCatalogIssue(
                        source: .profileFile(paths.profilesDirectory.lastPathComponent),
                        reason: .unreadable
                    )
                )
            }
        }

        var bundledEntries: [String: [String: JSONValue]] = [:]
        if let bundledPostprocessorsURL {
            do {
                let data = try Data(contentsOf: bundledPostprocessorsURL, options: [.mappedIfSafe])
                let document = try Self.parsePostprocessorDocument(
                    data: data,
                    skipInvalidEntries: true
                )
                bundledEntries = document.postprocessors
                issues.append(
                    contentsOf: document.invalidEntryIDs.map {
                        EnhancementCatalogIssue(
                            source: .postprocessor($0),
                            reason: .invalidField,
                            field: "postprocessors.\($0)"
                        )
                    }
                )
            } catch {
                issues.append(
                    Self.catalogIssue(
                        for: error,
                        source: .bundledPostprocessors
                    )
                )
            }
        } else {
            issues.append(
                EnhancementCatalogIssue(
                    source: .bundledPostprocessors,
                    reason: .unreadable
                )
            )
        }

        var localEntries: [String: [String: JSONValue]] = [:]
        var localPostprocessorIDs: Set<String> = []
        var blockedPostprocessorIDs: Set<String> = []
        var blocksAllPostprocessors = false
        let localCatalogExists =
            fileManager.fileExists(atPath: paths.postprocessorsFile.path)
            || (try? fileManager.destinationOfSymbolicLink(
                atPath: paths.postprocessorsFile.path
            )) != nil
        let postprocessorsFingerprint = (try? fingerprint(at: paths.postprocessorsFile))
            ?? .missing
        if localCatalogExists,
           postprocessorsFingerprint.byteCount
            > UInt64(EnhancementCatalogLimits.maximumCatalogDocumentBytes) {
            issues.append(
                EnhancementCatalogIssue(
                    source: .localPostprocessors,
                    reason: .invalidField,
                    field: "payload"
                )
            )
            blocksAllPostprocessors = true
        } else if localCatalogExists {
            do {
                let data = try Data(contentsOf: paths.postprocessorsFile, options: [.mappedIfSafe])
                let document = try Self.parsePostprocessorDocument(
                    data: data,
                    skipInvalidEntries: true
                )
                localEntries = document.postprocessors
                localPostprocessorIDs.formUnion(document.postprocessors.keys)
                localPostprocessorIDs.formUnion(document.invalidEntryIDs)
                blockedPostprocessorIDs.formUnion(document.invalidEntryIDs)
                issues.append(
                    contentsOf: document.invalidEntryIDs.map {
                        EnhancementCatalogIssue(
                            source: .postprocessor($0),
                            reason: .invalidField,
                            field: "postprocessors.\($0)"
                        )
                    }
                )
            } catch {
                issues.append(
                    Self.catalogIssue(
                        for: error,
                        source: .localPostprocessors
                    )
                )
                // A malformed or unreadable local document may contain
                // overrides that cannot be enumerated safely. Do not silently
                // reactivate any bundled executable definitions.
                blocksAllPostprocessors = true
            }
        }

        var postprocessors: [String: EnhancementPostprocessorPreset] = [:]
        if !blocksAllPostprocessors {
            let allIDs = Set(bundledEntries.keys)
                .union(localEntries.keys)
                .union(blockedPostprocessorIDs)
            for id in allIDs.sorted() {
                guard !blockedPostprocessorIDs.contains(id) else {
                    continue
                }
                do {
                    guard let raw = try Self.effectivePostprocessorRaw(
                        id: id,
                        bundled: bundledEntries[id],
                        local: localEntries[id]
                    ) else {
                        continue
                    }
                    let parsed = try Self.parsePostprocessor(id: id, raw: raw)
                    postprocessors[id] = Self.enforcingLocalDestinationTrust(
                        parsed,
                        bundled: bundledEntries[id],
                        local: localEntries[id]
                    )
                } catch {
                    if localEntries[id] != nil {
                        blockedPostprocessorIDs.insert(id)
                    }
                    issues.append(
                        Self.catalogIssue(
                            for: error,
                            source: .postprocessor(id)
                        )
                    )
                }
            }
        }

        return EnhancementCatalogSnapshot(
            profiles: profiles,
            postprocessors: postprocessors,
            localPostprocessorIDs: localPostprocessorIDs,
            blockedPostprocessorIDs: blockedPostprocessorIDs,
            blocksAllPostprocessors: blocksAllPostprocessors,
            issues: issues,
            fingerprints: EnhancementCatalogFingerprints(
                profiles: profileFingerprints,
                postprocessors: postprocessorsFingerprint
            )
        )
    }

    @discardableResult
    func saveProfile(
        _ profile: EnhancementProfile,
        expectedFingerprint: EnhancementFileFingerprint? = nil
    ) throws -> EnhancementFileFingerprint {
        let normalized: EnhancementProfile
        do {
            normalized = try Self.validate(profile: profile)
        } catch let failure as EnhancementValidationFailure {
            throw Self.profileSaveError(for: failure)
        }

        let destination = paths.profilesDirectory
            .appendingPathComponent("\(normalized.id).json")
        try verifyFingerprint(expectedFingerprint, at: destination)

        var preservedExtraFields: [String: JSONValue] = [:]
        if fileManager.fileExists(atPath: destination.path) {
            do {
                guard try fingerprint(at: destination).byteCount
                        <= UInt64(
                            EnhancementCatalogLimits.maximumCatalogDocumentBytes
                        ) else {
                    throw EnhancementValidationFailure.invalidField("payload")
                }
                let existingData = try Data(contentsOf: destination, options: [.mappedIfSafe])
                let existing = try Self.parseProfile(
                    data: existingData,
                    expectedID: normalized.id
                )
                preservedExtraFields = existing.extraFields
            } catch {
                throw EnhancementCatalogError.malformedExistingFile(destination.lastPathComponent)
            }
        }

        var value = normalized
        preservedExtraFields.merge(value.extraFields) { _, new in new }
        value.extraFields = preservedExtraFields
        let data = try Self.encodeJSON(.object(Self.profileRaw(value)))
        try atomicPrivateWrite(
            data,
            to: destination,
            expectedFingerprint: expectedFingerprint
        )
        return try fingerprint(at: destination)
    }

    @discardableResult
    func savePostprocessor(
        _ preset: EnhancementPostprocessorPreset,
        expectedFingerprint: EnhancementFileFingerprint? = nil,
        destinationWasExplicitlyReclassified: Bool = false
    ) throws -> EnhancementPostprocessorSaveResult {
        let normalized: EnhancementPostprocessorPreset
        do {
            normalized = try Self.validate(preset: preset)
        } catch let failure as EnhancementValidationFailure {
            throw Self.postprocessorSaveError(for: failure)
        }

        let destination = paths.postprocessorsFile
        try verifyFingerprint(expectedFingerprint, at: destination)

        let bundledEntries = try loadBundledEntriesForSave()
        var document: RawPostprocessorDocument
        if fileManager.fileExists(atPath: destination.path) {
            do {
                guard try fingerprint(at: destination).byteCount
                        <= UInt64(
                            EnhancementCatalogLimits.maximumCatalogDocumentBytes
                        ) else {
                    throw EnhancementValidationFailure.invalidField("payload")
                }
                let existingData = try Data(contentsOf: destination, options: [.mappedIfSafe])
                document = try Self.parsePostprocessorDocument(data: existingData)
                try Self.validateLocalEntriesForSave(
                    document.postprocessors,
                    bundledEntries: bundledEntries
                )
            } catch {
                throw EnhancementCatalogError.malformedExistingFile(destination.lastPathComponent)
            }
        } else {
            document = RawPostprocessorDocument(
                root: ["version": .number(1)],
                postprocessors: [:],
                invalidEntryIDs: []
            )
        }

        var presetToSave = normalized
        if destinationWasExplicitlyReclassified {
            presetToSave.destinationReviewRevision =
                presetToSave.destination == .local
                ? presetToSave.commandRevision
                : nil
        } else {
            let previous: EnhancementPostprocessorPreset? = try Self
                .effectivePostprocessorRaw(
                    id: normalized.id,
                    bundled: bundledEntries[normalized.id],
                    local: document.postprocessors[normalized.id]
                )
                .flatMap { try? Self.parsePostprocessor(id: normalized.id, raw: $0) }
            let bundled = bundledEntries[normalized.id].flatMap {
                try? Self.parsePostprocessor(id: normalized.id, raw: $0)
            }
            let commandChanged =
                previous.map { $0.commandRevision != normalized.commandRevision }
                ?? (bundled?.commandRevision != normalized.commandRevision)
            let matchesTrustedBundledCommand =
                bundled?.destination == .local
                && bundled?.commandRevision == normalized.commandRevision
            let hasMatchingReview =
                normalized.destinationReviewRevision == normalized.commandRevision

            if commandChanged {
                presetToSave.destination = .unknown
                presetToSave.destinationReviewRevision = nil
            } else if presetToSave.destination == .local,
                      !matchesTrustedBundledCommand,
                      !hasMatchingReview {
                presetToSave.destination = .unknown
                presetToSave.destinationReviewRevision = nil
            } else if presetToSave.destination != .local {
                presetToSave.destinationReviewRevision = nil
            }
        }

        var raw = document.postprocessors[presetToSave.id] ?? [:]
        let existingExtras = raw.filter { !Self.postprocessorKnownFields.contains($0.key) }
        raw = Self.postprocessorRaw(presetToSave)
        raw.merge(existingExtras) { current, _ in current }
        raw.merge(presetToSave.extraFields) { _, new in new }
        document.postprocessors[presetToSave.id] = raw

        var root = document.root
        root["version"] = .number(1)
        root["postprocessors"] = .object(
            document.postprocessors.mapValues { .object($0) }
        )
        let data = try Self.encodeJSON(.object(root))
        try atomicPrivateWrite(
            data,
            to: destination,
            expectedFingerprint: expectedFingerprint
        )
        return EnhancementPostprocessorSaveResult(
            preset: presetToSave,
            fingerprint: try fingerprint(at: destination)
        )
    }

    func profileFingerprint(id: String) throws -> EnhancementFileFingerprint {
        do {
            try Self.validateIdentifier(id, field: "profile_id")
        } catch {
            throw EnhancementCatalogError.invalidProfile(field: "profile_id")
        }
        return try fingerprint(
            at: paths.profilesDirectory.appendingPathComponent("\(id).json")
        )
    }

    func postprocessorsFingerprint() throws -> EnhancementFileFingerprint {
        try fingerprint(at: paths.postprocessorsFile)
    }

    private func loadBundledEntriesForSave() throws -> [String: [String: JSONValue]] {
        guard let bundledPostprocessorsURL else {
            throw EnhancementCatalogError.missingBundledCatalog
        }
        do {
            let data = try Data(contentsOf: bundledPostprocessorsURL, options: [.mappedIfSafe])
            let document = try Self.parsePostprocessorDocument(data: data)
            try Self.validateBundledEntriesForSave(document.postprocessors)
            return document.postprocessors
        } catch let error as EnhancementCatalogError {
            throw error
        } catch {
            throw EnhancementCatalogError.malformedExistingFile(
                bundledPostprocessorsURL.lastPathComponent
            )
        }
    }

    private func verifyFingerprint(
        _ expected: EnhancementFileFingerprint?,
        at url: URL
    ) throws {
        guard let expected else {
            return
        }
        let current = try fingerprint(at: url)
        guard current == expected else {
            throw EnhancementCatalogError.fingerprintConflict(url.lastPathComponent)
        }
    }

    private func fingerprint(at url: URL) throws -> EnhancementFileFingerprint {
        guard fileManager.fileExists(atPath: url.path) else {
            return .missing
        }
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            var hasher = SHA256()
            var byteCount: UInt64 = 0
            while let chunk = try handle.read(upToCount: 64 * 1024),
                  !chunk.isEmpty {
                byteCount += UInt64(chunk.count)
                if byteCount
                    > UInt64(
                        EnhancementCatalogLimits.maximumCatalogDocumentBytes
                    ) {
                    return EnhancementFileFingerprint(
                        exists: true,
                        byteCount: byteCount,
                        contentSHA256: nil
                    )
                }
                hasher.update(data: chunk)
            }
            let digest = hasher.finalize()
            return EnhancementFileFingerprint(
                exists: true,
                byteCount: byteCount,
                contentSHA256: digest.map { String(format: "%02x", $0) }.joined()
            )
        } catch {
            throw EnhancementCatalogError.fileOperation("fingerprinting")
        }
    }

    private func atomicPrivateWrite(
        _ data: Data,
        to destination: URL,
        expectedFingerprint: EnhancementFileFingerprint?
    ) throws {
        let directory = destination.deletingLastPathComponent()
        do {
            try paths.createPrivateDirectory(directory)
        } catch {
            throw EnhancementCatalogError.fileOperation("directory preparation")
        }

        let temporary = directory.appendingPathComponent(
            ".\(destination.lastPathComponent).\(UUID().uuidString).tmp"
        )
        guard fileManager.createFile(
            atPath: temporary.path,
            contents: nil,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw EnhancementCatalogError.fileOperation("temporary file creation")
        }
        defer {
            try? fileManager.removeItem(at: temporary)
        }

        do {
            let handle = try FileHandle(forWritingTo: temporary)
            do {
                try handle.write(contentsOf: data)
                try handle.synchronize()
                try handle.close()
            } catch {
                try? handle.close()
                throw error
            }
            try fileManager.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: temporary.path
            )

            // Recheck immediately before replacement so the expected identity
            // covers edits made while validation and encoding were in progress.
            try verifyFingerprint(expectedFingerprint, at: destination)
            if fileManager.fileExists(atPath: destination.path) {
                _ = try fileManager.replaceItemAt(
                    destination,
                    withItemAt: temporary,
                    backupItemName: nil,
                    options: []
                )
            } else {
                try fileManager.moveItem(at: temporary, to: destination)
            }
            try fileManager.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: destination.path
            )
        } catch let error as EnhancementCatalogError {
            throw error
        } catch {
            throw EnhancementCatalogError.fileOperation("atomic save")
        }
    }

    private static func defaultBundledPostprocessorsURL() -> URL? {
        if let mainResource = postprocessorsURL(in: Bundle.main) {
            return mainResource
        }

        // Bundle.module's generated accessor fatalErrors when its resource
        // bundle is absent. Probe the known SwiftPM bundle locations
        // optionally instead, which supports dev/tests while allowing a
        // packaged app with a missing direct resource to fail closed.
        let resourceBundleName = "ZenWhisper_ZenWhisper.bundle"
        var candidateRoots = [
            Bundle.main.bundleURL,
            Bundle.main.bundleURL.deletingLastPathComponent()
        ]
        for loadedBundle in Bundle.allBundles + Bundle.allFrameworks {
            candidateRoots.append(loadedBundle.bundleURL)
            candidateRoots.append(
                loadedBundle.bundleURL.deletingLastPathComponent()
            )
        }
        if let executableDirectory = Bundle.main.executableURL?
            .deletingLastPathComponent() {
            candidateRoots.append(executableDirectory)
            candidateRoots.append(
                executableDirectory
                    .deletingLastPathComponent()
                    .deletingLastPathComponent()
                    .deletingLastPathComponent()
            )
        }
        if let argumentZero = CommandLine.arguments.first,
           !argumentZero.isEmpty {
            var commandDirectory = URL(fileURLWithPath: argumentZero)
                .deletingLastPathComponent()
            for _ in 0..<6 {
                candidateRoots.append(commandDirectory)
                commandDirectory.deleteLastPathComponent()
            }
        }
        for root in candidateRoots {
            let candidate = root.appendingPathComponent(
                resourceBundleName,
                isDirectory: true
            )
            if let bundle = Bundle(url: candidate),
               let resource = postprocessorsURL(in: bundle) {
                return resource
            }
        }
        return nil
    }

    private static func postprocessorsURL(in bundle: Bundle) -> URL? {
        if let direct = bundle.url(
            forResource: "postprocessors.default",
            withExtension: "json"
        ) {
            return direct
        }
        return bundle.url(
            forResource: "postprocessors.default",
            withExtension: "json",
            subdirectory: "Resources"
        )
    }

    private static func parseProfile(
        data: Data,
        expectedID: String
    ) throws -> EnhancementProfile {
        let root = try decodeJSONObject(data)
        try validateVersion(root)
        let profileID: String
        if let rawID = root["profile_id"] {
            guard let id = rawID.stringValue else {
                throw EnhancementValidationFailure.invalidField("profile_id")
            }
            profileID = id
        } else {
            profileID = expectedID
        }
        try validateIdentifier(profileID, field: "profile_id")
        guard profileID == expectedID else {
            throw EnhancementValidationFailure.invalidIdentifier("profile_id")
        }
        guard let name = root["name"]?.stringValue else {
            throw EnhancementValidationFailure.invalidField("name")
        }
        let context: String
        if let rawContext = root["context"] {
            guard let value = rawContext.stringValue else {
                throw EnhancementValidationFailure.invalidField("context")
            }
            context = value
        } else {
            context = ""
        }

        let rawTerms: [JSONValue]
        if let rawValue = root["terms"] {
            guard let values = rawValue.arrayValue else {
                throw EnhancementValidationFailure.invalidField("terms")
            }
            rawTerms = values
        } else {
            rawTerms = []
        }

        var terms: [EnhancementTerm] = []
        var replacements: [String: String] = [:]
        for (index, rawValue) in rawTerms.enumerated() {
            guard let raw = rawValue.objectValue else {
                throw EnhancementValidationFailure.invalidField("terms[\(index)]")
            }
            guard let canonical = raw["canonical"]?.stringValue else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].canonical"
                )
            }
            let spoken = try normalizedStringArray(
                raw["spoken"],
                field: "terms[\(index)].spoken"
            )
            let replaceFrom = try normalizedStringArray(
                raw["replace_from"],
                field: "terms[\(index)].replace_from"
            )
            let description: String
            if let rawDescription = raw["description"] {
                guard let value = rawDescription.stringValue else {
                    throw EnhancementValidationFailure.invalidField(
                        "terms[\(index)].description"
                    )
                }
                description = value
            } else {
                description = ""
            }
            let normalizedCanonical = canonical.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            guard !normalizedCanonical.isEmpty else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].canonical"
                )
            }
            for (sourceIndex, source) in replaceFrom.enumerated() {
                if let previous = replacements[source],
                   previous != normalizedCanonical {
                    throw EnhancementValidationFailure.duplicateReplacement(
                        "terms[\(index)].replace_from[\(sourceIndex)]"
                    )
                }
                replacements[source] = normalizedCanonical
            }
            terms.append(
                EnhancementTerm(
                    canonical: normalizedCanonical,
                    spoken: spoken,
                    replaceFrom: replaceFrom,
                    description: description.trimmingCharacters(
                        in: .whitespacesAndNewlines
                    ),
                    extraFields: raw.filter { !termKnownFields.contains($0.key) }
                )
            )
        }

        return try validate(
            profile: EnhancementProfile(
                id: profileID,
                name: name,
                context: context,
                terms: terms,
                extraFields: root.filter { !profileKnownFields.contains($0.key) }
            )
        )
    }

    private static func validate(profile: EnhancementProfile) throws -> EnhancementProfile {
        try validateIdentifier(profile.id, field: "profile_id")
        let name = profile.name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty,
              hasAcceptableStringSize(name) else {
            throw EnhancementValidationFailure.invalidField("name")
        }
        guard profile.terms.count <= EnhancementCatalogLimits.maximumProfileTermCount else {
            throw EnhancementValidationFailure.invalidField("terms")
        }
        let context = profile.context.trimmingCharacters(in: .whitespacesAndNewlines)
        guard context.utf8.count <= EnhancementCatalogLimits.maximumContextUTF8Bytes else {
            throw EnhancementValidationFailure.invalidField("context")
        }

        var replacements: [String: String] = [:]
        var normalizedTerms: [EnhancementTerm] = []
        for (index, term) in profile.terms.enumerated() {
            let canonical = term.canonical.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !canonical.isEmpty,
                  hasAcceptableStringSize(canonical) else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].canonical"
                )
            }
            guard term.spoken.count <= EnhancementCatalogLimits.maximumAliasesPerTerm else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].spoken"
                )
            }
            guard term.replaceFrom.count <= EnhancementCatalogLimits.maximumAliasesPerTerm else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].replace_from"
                )
            }
            let spoken = try validateStringValues(
                term.spoken,
                field: "terms[\(index)].spoken"
            )
            let replaceFrom = try validateStringValues(
                term.replaceFrom,
                field: "terms[\(index)].replace_from"
            )
            for (sourceIndex, source) in replaceFrom.enumerated() {
                if let previous = replacements[source], previous != canonical {
                    throw EnhancementValidationFailure.duplicateReplacement(
                        "terms[\(index)].replace_from[\(sourceIndex)]"
                    )
                }
                replacements[source] = canonical
            }
            let description = term.description.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            guard hasAcceptableStringSize(description) else {
                throw EnhancementValidationFailure.invalidField(
                    "terms[\(index)].description"
                )
            }
            normalizedTerms.append(
                EnhancementTerm(
                    canonical: canonical,
                    spoken: spoken,
                    replaceFrom: replaceFrom,
                    description: description,
                    extraFields: term.extraFields
                )
            )
        }
        let normalized = EnhancementProfile(
            id: profile.id,
            name: name,
            context: context,
            terms: normalizedTerms,
            extraFields: profile.extraFields
        )
        guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
            profile: normalized,
            preset: nil
        ) else {
            throw EnhancementValidationFailure.invalidField("payload")
        }
        return normalized
    }

    private static func profileRaw(
        _ profile: EnhancementProfile
    ) -> [String: JSONValue] {
        var root = profile.extraFields
        root["version"] = .number(1)
        root["profile_id"] = .string(profile.id)
        root["name"] = .string(profile.name)
        root["context"] = .string(profile.context)
        root["terms"] = .array(
            profile.terms.map { term in
                var raw = term.extraFields
                raw["canonical"] = .string(term.canonical)
                raw["spoken"] = .array(term.spoken.map(JSONValue.string))
                raw["replace_from"] = .array(term.replaceFrom.map(JSONValue.string))
                raw["description"] = .string(term.description)
                return .object(raw)
            }
        )
        return root
    }

    private static func parsePostprocessorDocument(
        data: Data,
        skipInvalidEntries: Bool = false
    ) throws -> RawPostprocessorDocument {
        let root = try decodeJSONObject(data)
        try validateVersion(root)
        guard let rawPostprocessors = root["postprocessors"]?.objectValue else {
            throw EnhancementValidationFailure.invalidField("postprocessors")
        }
        var postprocessors: [String: [String: JSONValue]] = [:]
        var invalidEntryIDs: [String] = []
        for (id, value) in rawPostprocessors {
            guard let raw = value.objectValue else {
                if skipInvalidEntries {
                    invalidEntryIDs.append(id)
                    continue
                }
                throw EnhancementValidationFailure.invalidField("postprocessors.\(id)")
            }
            postprocessors[id] = raw
        }
        return RawPostprocessorDocument(
            root: root,
            postprocessors: postprocessors,
            invalidEntryIDs: invalidEntryIDs.sorted()
        )
    }

    private static func effectivePostprocessorRaw(
        id: String,
        bundled: [String: JSONValue]?,
        local: [String: JSONValue]?
    ) throws -> [String: JSONValue]? {
        try validateIdentifier(id, field: "preset_id")
        guard id != "off", id != "dictionary" else {
            throw EnhancementValidationFailure.invalidIdentifier("preset_id")
        }
        if let enabled = local?["enabled"] {
            guard let isEnabled = enabled.boolValue else {
                throw EnhancementValidationFailure.invalidField("enabled")
            }
            if !isEnabled {
                return nil
            }
        }

        guard bundled != nil || local != nil else {
            return nil
        }
        var effective = bundled ?? [:]
        if var local {
            let executableChanged = local["executable"].map {
                $0 != bundled?["executable"]
            } ?? false
            let argumentsChanged = local["arguments"].map {
                $0 != bundled?["arguments"]
            } ?? false
            let environmentChanged = local["environment"].map {
                $0 != bundled?["environment"]
            } ?? false
            let preflightExecutableChanged = local["preflight_executable"].map {
                $0 != bundled?["preflight_executable"]
            } ?? false
            let preflightArgumentsChanged = local["preflight_arguments"].map {
                $0 != bundled?["preflight_arguments"]
            } ?? false
            let inputModeChanged = local["input_mode"].map {
                $0 != bundled?["input_mode"]
            } ?? false
            let commandDefinitionChanged =
                executableChanged
                || argumentsChanged
                || environmentChanged
                || preflightExecutableChanged
                || preflightArgumentsChanged
                || inputModeChanged

            if executableChanged, bundled != nil {
                if local["arguments"] == nil {
                    local["arguments"] = .array([])
                }
                if local["input_mode"] == nil {
                    local["input_mode"] = .string("stdin")
                }
                if local["preflight_executable"] == nil {
                    local["preflight_executable"] = .string("")
                }
                if local["preflight_arguments"] == nil {
                    local["preflight_arguments"] = .array([])
                }
                if local["preflight_failure_message"] == nil {
                    local["preflight_failure_message"] = .string("")
                }
                if local["environment"] == nil {
                    local["environment"] = .object([:])
                }
            }
            if bundled != nil,
               commandDefinitionChanged,
               local["data_destination"] == nil {
                local["data_destination"] = .string(
                    EnhancementDataDestination.unknown.rawValue
                )
            }
            if bundled == nil, local["data_destination"] == nil {
                local["data_destination"] = .string(
                    EnhancementDataDestination.unknown.rawValue
                )
            }
            effective.merge(local) { _, override in override }
        }
        effective.removeValue(forKey: "enabled")
        return effective
    }

    private static func enforcingLocalDestinationTrust(
        _ preset: EnhancementPostprocessorPreset,
        bundled: [String: JSONValue]?,
        local: [String: JSONValue]?
    ) -> EnhancementPostprocessorPreset {
        guard preset.destination == .local, local != nil else {
            return preset
        }

        let bundledPreset = bundled.flatMap {
            try? parsePostprocessor(id: preset.id, raw: $0)
        }
        if bundledPreset?.destination == .local,
           bundledPreset?.commandRevision == preset.commandRevision {
            return preset
        }

        let localReviewRevision = local?["destination_review_revision"]?
            .stringValue?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard localReviewRevision == preset.commandRevision else {
            var untrusted = preset
            untrusted.destination = .unknown
            return untrusted
        }
        return preset
    }

    private static func parsePostprocessor(
        id: String,
        raw: [String: JSONValue]
    ) throws -> EnhancementPostprocessorPreset {
        try validateIdentifier(id, field: "preset_id")
        guard id != "off", id != "dictionary" else {
            throw EnhancementValidationFailure.invalidIdentifier("preset_id")
        }
        guard let displayName = raw["display_name"]?.stringValue else {
            throw EnhancementValidationFailure.invalidField("display_name")
        }
        guard let executable = raw["executable"]?.stringValue else {
            throw EnhancementValidationFailure.invalidField("executable")
        }
        let arguments = try requiredStringArray(raw["arguments"], field: "arguments")
        let preflightExecutable: String
        if let value = raw["preflight_executable"] {
            guard let parsed = value.stringValue else {
                throw EnhancementValidationFailure.invalidField("preflight_executable")
            }
            preflightExecutable = parsed
        } else {
            preflightExecutable = ""
        }
        let preflightArguments = try normalizedStringArray(
            raw["preflight_arguments"],
            field: "preflight_arguments",
            dropEmpty: false
        )
        let preflightFailureMessage: String
        if let value = raw["preflight_failure_message"] {
            guard let parsed = value.stringValue else {
                throw EnhancementValidationFailure.invalidField(
                    "preflight_failure_message"
                )
            }
            preflightFailureMessage = parsed
        } else {
            preflightFailureMessage = ""
        }
        guard let rawInputMode = raw["input_mode"]?.stringValue,
              let inputMode = EnhancementPostprocessorInputMode(rawValue: rawInputMode) else {
            throw EnhancementValidationFailure.invalidField("input_mode")
        }
        guard let rawDestination = raw["data_destination"]?.stringValue,
              let destination = EnhancementDataDestination(rawValue: rawDestination) else {
            throw EnhancementValidationFailure.invalidField("data_destination")
        }
        guard let timeoutSeconds = raw["timeout_sec"]?.numberValue else {
            throw EnhancementValidationFailure.invalidField("timeout_sec")
        }
        guard let promptTemplate = raw["prompt_template"]?.stringValue else {
            throw EnhancementValidationFailure.invalidField("prompt_template")
        }
        let environment = try stringDictionary(
            raw["environment"],
            field: "environment"
        )
        let destinationReviewRevision: String?
        if let value = raw["destination_review_revision"] {
            guard let revision = value.stringValue else {
                throw EnhancementValidationFailure.invalidField(
                    "destination_review_revision"
                )
            }
            destinationReviewRevision = revision
        } else {
            destinationReviewRevision = nil
        }

        return try validate(
            preset: EnhancementPostprocessorPreset(
                id: id,
                displayName: displayName,
                executable: executable,
                arguments: arguments,
                preflightExecutable: preflightExecutable,
                preflightArguments: preflightArguments,
                preflightFailureMessage: preflightFailureMessage,
                inputMode: inputMode,
                destination: destination,
                timeoutSeconds: timeoutSeconds,
                promptTemplate: promptTemplate,
                environment: environment,
                destinationReviewRevision: destinationReviewRevision,
                extraFields: raw.filter {
                    !postprocessorKnownFields.contains($0.key)
                }
            )
        )
    }

    private static func validate(
        preset: EnhancementPostprocessorPreset
    ) throws -> EnhancementPostprocessorPreset {
        try validateIdentifier(preset.id, field: "preset_id")
        guard preset.id != "off", preset.id != "dictionary" else {
            throw EnhancementValidationFailure.invalidIdentifier("preset_id")
        }
        let displayName = preset.displayName.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        let executable = preset.executable.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard !displayName.isEmpty,
              hasAcceptableStringSize(displayName) else {
            throw EnhancementValidationFailure.invalidField("display_name")
        }
        guard !executable.isEmpty,
              hasAcceptableStringSize(executable),
              !containsNUL(executable) else {
            throw EnhancementValidationFailure.invalidField("executable")
        }
        guard placeholders(in: executable).isEmpty,
              !containsTemplateSyntax(executable) else {
            throw EnhancementValidationFailure.invalidField("executable")
        }
        guard preset.timeoutSeconds.isFinite,
              preset.timeoutSeconds > 0,
              preset.timeoutSeconds
                <= EnhancementCatalogLimits.maximumPresetTimeoutSeconds else {
            throw EnhancementValidationFailure.invalidField("timeout_sec")
        }
        guard hasAcceptableStringSize(preset.promptTemplate),
              !containsNUL(preset.promptTemplate) else {
            throw EnhancementValidationFailure.invalidField("prompt_template")
        }
        let promptPlaceholders = Set(placeholders(in: preset.promptTemplate))
        guard !containsUnknownPlaceholders(preset.promptTemplate),
              promptPlaceholders.isSubset(of: supportedPlaceholders),
              promptPlaceholders.contains("transcript") else {
            throw EnhancementValidationFailure.invalidField("prompt_template")
        }
        guard preset.arguments.count
                <= EnhancementCatalogLimits.maximumPresetArgumentCount,
              preset.arguments.allSatisfy({
                  hasAcceptableStringSize($0) && !containsNUL($0)
              }) else {
            throw EnhancementValidationFailure.invalidField("arguments")
        }
        let argumentPlaceholders = Set(preset.arguments.flatMap(placeholders))
        guard preset.arguments.allSatisfy({ !containsUnknownPlaceholders($0) }),
              argumentPlaceholders.isSubset(of: supportedPlaceholders) else {
            throw EnhancementValidationFailure.invalidField("arguments")
        }
        switch preset.inputMode {
        case .stdin:
            guard argumentPlaceholders.isEmpty else {
                throw EnhancementValidationFailure.invalidField("arguments")
            }
        case .argument:
            guard argumentPlaceholders.contains("prompt") else {
                throw EnhancementValidationFailure.invalidField("arguments")
            }
        }

        let preflightExecutable = preset.preflightExecutable.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard hasAcceptableStringSize(preflightExecutable),
              !containsNUL(preflightExecutable),
              !containsTemplateSyntax(preflightExecutable),
              preset.preflightArguments.count
                <= EnhancementCatalogLimits.maximumPresetArgumentCount,
              preset.preflightArguments.allSatisfy({
                  hasAcceptableStringSize($0)
                  && !containsNUL($0)
                  && !containsTemplateSyntax($0)
              }) else {
            throw EnhancementValidationFailure.invalidField("preflight")
        }
        guard !preflightExecutable.isEmpty || preset.preflightArguments.isEmpty else {
            throw EnhancementValidationFailure.invalidField("preflight_arguments")
        }
        guard hasAcceptableStringSize(preset.preflightFailureMessage),
              !containsNUL(preset.preflightFailureMessage) else {
            throw EnhancementValidationFailure.invalidField(
                "preflight_failure_message"
            )
        }
        guard preset.environment.count
                <= EnhancementCatalogLimits.maximumPresetEnvironmentEntryCount else {
            throw EnhancementValidationFailure.invalidField("environment")
        }
        for (key, value) in preset.environment {
            guard !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  hasAcceptableStringSize(key),
                  hasAcceptableStringSize(value),
                  !key.contains("="),
                  !containsNUL(key),
                  !containsNUL(value) else {
                throw EnhancementValidationFailure.invalidField("environment")
            }
        }

        if let destinationReviewRevision = preset.destinationReviewRevision {
            guard hasAcceptableStringSize(destinationReviewRevision) else {
                throw EnhancementValidationFailure.invalidField(
                    "destination_review_revision"
                )
            }
        }

        let normalized = EnhancementPostprocessorPreset(
            id: preset.id,
            displayName: displayName,
            executable: executable,
            arguments: preset.arguments,
            preflightExecutable: preflightExecutable,
            preflightArguments: preset.preflightArguments,
            preflightFailureMessage: preset.preflightFailureMessage
                .trimmingCharacters(in: .whitespacesAndNewlines),
            inputMode: preset.inputMode,
            destination: preset.destination,
            timeoutSeconds: preset.timeoutSeconds,
            promptTemplate: preset.promptTemplate,
            environment: preset.environment,
            destinationReviewRevision: preset.destinationReviewRevision,
            extraFields: preset.extraFields
        )
        guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
            profile: nil,
            preset: normalized
        ) else {
            throw EnhancementValidationFailure.invalidField("payload")
        }
        return normalized
    }

    private static func postprocessorRaw(
        _ preset: EnhancementPostprocessorPreset
    ) -> [String: JSONValue] {
        var raw = preset.extraFields
        raw["display_name"] = .string(preset.displayName)
        raw["executable"] = .string(preset.executable)
        raw["arguments"] = .array(preset.arguments.map(JSONValue.string))
        raw["preflight_executable"] = .string(preset.preflightExecutable)
        raw["preflight_arguments"] = .array(
            preset.preflightArguments.map(JSONValue.string)
        )
        raw["preflight_failure_message"] = .string(
            preset.preflightFailureMessage
        )
        raw["input_mode"] = .string(preset.inputMode.rawValue)
        raw["data_destination"] = .string(preset.destination.rawValue)
        raw["timeout_sec"] = .number(preset.timeoutSeconds)
        raw["prompt_template"] = .string(preset.promptTemplate)
        raw["environment"] = .object(
            preset.environment.mapValues(JSONValue.string)
        )
        if let destinationReviewRevision = preset.destinationReviewRevision {
            raw["destination_review_revision"] = .string(
                destinationReviewRevision
            )
        } else {
            raw.removeValue(forKey: "destination_review_revision")
        }
        return raw
    }

    private static func validateLocalEntriesForSave(
        _ localEntries: [String: [String: JSONValue]],
        bundledEntries: [String: [String: JSONValue]]
    ) throws {
        for (id, local) in localEntries {
            if let effective = try effectivePostprocessorRaw(
                id: id,
                bundled: bundledEntries[id],
                local: local
            ) {
                _ = try parsePostprocessor(id: id, raw: effective)
            }
        }
    }

    private static func validateBundledEntriesForSave(
        _ bundledEntries: [String: [String: JSONValue]]
    ) throws {
        for (id, raw) in bundledEntries {
            guard let effective = try effectivePostprocessorRaw(
                id: id,
                bundled: raw,
                local: nil
            ) else {
                continue
            }
            _ = try parsePostprocessor(id: id, raw: effective)
        }
    }

    private static func decodeJSONObject(
        _ data: Data
    ) throws -> [String: JSONValue] {
        guard data.count <= EnhancementCatalogLimits.maximumCatalogDocumentBytes else {
            throw EnhancementValidationFailure.invalidField("payload")
        }
        let value = try JSONDecoder().decode(JSONValue.self, from: data)
        guard let object = value.objectValue else {
            throw EnhancementValidationFailure.invalidField("root")
        }
        return object
    }

    private static func encodeJSON(_ value: JSONValue) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        do {
            var data = try encoder.encode(value)
            data.append(0x0A)
            guard data.count
                    <= EnhancementCatalogLimits.maximumCatalogDocumentBytes else {
                throw EnhancementCatalogError.fileOperation("JSON encoding")
            }
            return data
        } catch let error as EnhancementCatalogError {
            throw error
        } catch {
            throw EnhancementCatalogError.fileOperation("JSON encoding")
        }
    }

    private static func validateVersion(
        _ root: [String: JSONValue]
    ) throws {
        guard let version = root["version"]?.numberValue,
              version == 1 else {
            throw EnhancementValidationFailure.unsupportedVersion
        }
    }

    private static func validateIdentifier(
        _ id: String,
        field: String
    ) throws {
        guard id.range(
            of: #"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"#,
            options: .regularExpression
        ) != nil else {
            throw EnhancementValidationFailure.invalidIdentifier(field)
        }
    }

    private static func normalizedStringArray(
        _ value: JSONValue?,
        field: String,
        dropEmpty: Bool = true
    ) throws -> [String] {
        guard let value else {
            return []
        }
        guard let array = value.arrayValue else {
            throw EnhancementValidationFailure.invalidField(field)
        }
        var result: [String] = []
        for item in array {
            guard let string = item.stringValue else {
                throw EnhancementValidationFailure.invalidField(field)
            }
            let normalized = string.trimmingCharacters(in: .whitespacesAndNewlines)
            if !dropEmpty || !normalized.isEmpty {
                result.append(dropEmpty ? normalized : string)
            }
        }
        return result
    }

    private static func requiredStringArray(
        _ value: JSONValue?,
        field: String
    ) throws -> [String] {
        guard let value, let array = value.arrayValue else {
            throw EnhancementValidationFailure.invalidField(field)
        }
        var result: [String] = []
        for item in array {
            guard let string = item.stringValue else {
                throw EnhancementValidationFailure.invalidField(field)
            }
            result.append(string)
        }
        return result
    }

    private static func validateStringValues(
        _ values: [String],
        field: String
    ) throws -> [String] {
        var result: [String] = []
        for value in values {
            let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty,
                  hasAcceptableStringSize(normalized) else {
                throw EnhancementValidationFailure.invalidField(field)
            }
            result.append(normalized)
        }
        return result
    }

    private static func stringDictionary(
        _ value: JSONValue?,
        field: String
    ) throws -> [String: String] {
        guard let value else {
            return [:]
        }
        guard let object = value.objectValue else {
            throw EnhancementValidationFailure.invalidField(field)
        }
        var result: [String: String] = [:]
        for (key, value) in object {
            guard let string = value.stringValue else {
                throw EnhancementValidationFailure.invalidField(field)
            }
            result[key] = string
        }
        return result
    }

    private static func placeholders(in text: String) -> [String] {
        guard let regex = try? NSRegularExpression(
            pattern: #"\{\{([a-z_]+)\}\}"#
        ) else {
            return []
        }
        let range = NSRange(text.startIndex..., in: text)
        return regex.matches(in: text, range: range).compactMap { match in
            guard let capture = Range(match.range(at: 1), in: text) else {
                return nil
            }
            return String(text[capture])
        }
    }

    private static func containsUnknownPlaceholders(_ text: String) -> Bool {
        guard let regex = try? NSRegularExpression(pattern: #"\{\{([^{}]+)\}\}"#) else {
            return false
        }
        let range = NSRange(text.startIndex..., in: text)
        for match in regex.matches(in: text, range: range) {
            guard let capture = Range(match.range(at: 1), in: text) else {
                continue
            }
            if !supportedPlaceholders.contains(String(text[capture])) {
                return true
            }
        }
        return false
    }

    private static func containsTemplateSyntax(_ text: String) -> Bool {
        text.contains("{{") || text.contains("}}")
    }

    private static func containsNUL(_ text: String) -> Bool {
        text.unicodeScalars.contains(where: { $0.value == 0 })
    }

    private static func hasAcceptableStringSize(_ text: String) -> Bool {
        text.utf8.count <= EnhancementCatalogLimits.maximumStringUTF8Bytes
    }

    private static func profileIssue(
        for error: Error,
        filename: String
    ) -> EnhancementCatalogIssue {
        catalogIssue(for: error, source: .profileFile(filename))
    }

    private static func catalogIssue(
        for error: Error,
        source: EnhancementCatalogIssue.Source
    ) -> EnhancementCatalogIssue {
        guard let failure = error as? EnhancementValidationFailure else {
            if error is DecodingError {
                return EnhancementCatalogIssue(
                    source: source,
                    reason: .malformedJSON
                )
            }
            return EnhancementCatalogIssue(source: source, reason: .unreadable)
        }
        switch failure {
        case .unsupportedVersion:
            return EnhancementCatalogIssue(
                source: source,
                reason: .unsupportedVersion,
                field: "version"
            )
        case .invalidIdentifier(let field):
            return EnhancementCatalogIssue(
                source: source,
                reason: .invalidIdentifier,
                field: field
            )
        case .invalidField(let field):
            return EnhancementCatalogIssue(
                source: source,
                reason: .invalidField,
                field: field
            )
        case .duplicateReplacement(let field):
            return EnhancementCatalogIssue(
                source: source,
                reason: .duplicateReplacement,
                field: field
            )
        }
    }

    private static func profileSaveError(
        for failure: EnhancementValidationFailure
    ) -> EnhancementCatalogError {
        switch failure {
        case .unsupportedVersion:
            return .invalidProfile(field: "version")
        case .invalidIdentifier(let field),
             .invalidField(let field),
             .duplicateReplacement(let field):
            return .invalidProfile(field: field)
        }
    }

    private static func postprocessorSaveError(
        for failure: EnhancementValidationFailure
    ) -> EnhancementCatalogError {
        switch failure {
        case .unsupportedVersion:
            return .invalidPostprocessor(field: "version")
        case .invalidIdentifier(let field),
             .invalidField(let field),
             .duplicateReplacement(let field):
            return .invalidPostprocessor(field: field)
        }
    }
}
