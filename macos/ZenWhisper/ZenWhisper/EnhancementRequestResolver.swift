import Foundation

struct ResolvedEnhancementRequest: Sendable {
    let profile: EnhancementProfile?
    let postprocessor: BackendPostprocessorRequest
    let cliWasSelected: Bool
    let warningCode: String?
}

enum EnhancementRequestResolver {
    static func resolve(
        selection: EnhancementSelection,
        catalog: EnhancementCatalogSnapshot
    ) -> ResolvedEnhancementRequest {
        let profile = selection.profileID.flatMap { catalog.profiles[$0] }
        let profileWarning =
            selection.profileID != nil && profile == nil
            ? "PROFILE_UNAVAILABLE"
            : nil

        switch selection.postprocessing {
        case .off:
            guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: nil
            ) else {
                return oversizedFallback(
                    profile: profile,
                    cliWasSelected: false
                )
            }
            return ResolvedEnhancementRequest(
                profile: profile,
                postprocessor: .off,
                cliWasSelected: false,
                warningCode: profileWarning
            )
        case .dictionary:
            guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: nil,
                includesDictionaryPostprocessor: true
            ) else {
                return oversizedFallback(
                    profile: profile,
                    cliWasSelected: false
                )
            }
            return ResolvedEnhancementRequest(
                profile: profile,
                postprocessor: .dictionary,
                cliWasSelected: false,
                warningCode: profileWarning
            )
        case .preset(let presetID):
            guard let preset = catalog.postprocessors[presetID] else {
                return dictionaryFallback(
                    profile: profile,
                    cliWasSelected: true,
                    warningCode: "POSTPROCESSOR_UNAVAILABLE"
                )
            }
            guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
                profile: profile,
                preset: preset
            ) else {
                return oversizedFallback(
                    profile: profile,
                    cliWasSelected: true
                )
            }
            if preset.destination != .local,
               selection.approvedPostprocessorRevision != preset.reviewRevision {
                return dictionaryFallback(
                    profile: profile,
                    cliWasSelected: true,
                    warningCode: "POSTPROCESSOR_CONSENT_REQUIRED"
                )
            }
            return ResolvedEnhancementRequest(
                profile: profile,
                postprocessor: .preset(preset),
                cliWasSelected: true,
                warningCode: profileWarning
            )
        }
    }

    private static func dictionaryFallback(
        profile: EnhancementProfile?,
        cliWasSelected: Bool,
        warningCode: String
    ) -> ResolvedEnhancementRequest {
        guard EnhancementCatalogLimits.enhancementPayloadFitsBudget(
            profile: profile,
            preset: nil,
            includesDictionaryPostprocessor: true
        ) else {
            return oversizedFallback(
                profile: profile,
                cliWasSelected: cliWasSelected
            )
        }
        return ResolvedEnhancementRequest(
            profile: profile,
            postprocessor: .dictionary,
            cliWasSelected: cliWasSelected,
            warningCode: warningCode
        )
    }

    private static func oversizedFallback(
        profile: EnhancementProfile?,
        cliWasSelected: Bool
    ) -> ResolvedEnhancementRequest {
        if EnhancementCatalogLimits.enhancementPayloadFitsBudget(
            profile: profile,
            preset: nil,
            includesDictionaryPostprocessor: true
        ) {
            return ResolvedEnhancementRequest(
                profile: profile,
                postprocessor: .dictionary,
                cliWasSelected: cliWasSelected,
                warningCode: "ENHANCEMENT_CONFIGURATION_TOO_LARGE"
            )
        }
        if EnhancementCatalogLimits.enhancementPayloadFitsBudget(
            profile: profile,
            preset: nil
        ) {
            return ResolvedEnhancementRequest(
                profile: profile,
                postprocessor: .off,
                cliWasSelected: cliWasSelected,
                warningCode: "ENHANCEMENT_CONFIGURATION_TOO_LARGE"
            )
        }
        return ResolvedEnhancementRequest(
            profile: nil,
            postprocessor: cliWasSelected ? .dictionary : .off,
            cliWasSelected: cliWasSelected,
            warningCode: "ENHANCEMENT_CONFIGURATION_TOO_LARGE"
        )
    }
}
