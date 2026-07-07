import Foundation

struct ModelRegistry: Decodable, Equatable {
    struct Language: Decodable, Equatable {
        let label: String
        let engines: [String: String]
    }

    struct Engine: Decodable, Equatable {
        struct Model: Decodable, Equatable {
            let id: String
            let label: String
        }

        let id: String
        let label: String
        let defaultModel: String
        let models: [Model]

        enum CodingKeys: String, CodingKey {
            case id
            case label
            case defaultModel = "default_model"
            case models
        }
    }

    let version: Int
    let defaultEngine: String
    let defaultLanguage: String
    let languages: [String: Language]
    let engines: [Engine]

    enum CodingKeys: String, CodingKey {
        case version
        case defaultEngine = "default_engine"
        case defaultLanguage = "default_language"
        case languages
        case engines
    }

    static func loadDefault() throws -> ModelRegistry {
        let url = Bundle.main.url(forResource: "model_registry", withExtension: "json")
            ?? Bundle.module.url(forResource: "model_registry", withExtension: "json")
        guard let url else {
            throw RegistryError.missingResource
        }
        return try load(from: url)
    }

    static func load(from url: URL) throws -> ModelRegistry {
        let data = try Data(contentsOf: url)
        let registry = try JSONDecoder().decode(ModelRegistry.self, from: data)
        try registry.validate()
        return registry
    }

    func validate() throws {
        guard version == 1 else {
            throw RegistryError.unsupportedVersion(version)
        }
        try requireUniqueIDs(engines.map(\.id), label: "engine")
        guard engines.contains(where: { $0.id == defaultEngine }) else {
            throw RegistryError.invalidDefaultEngine(defaultEngine)
        }
        guard languages[defaultLanguage] != nil else {
            throw RegistryError.invalidDefaultLanguage(defaultLanguage)
        }
        let engineIDs = Set(engines.map(\.id))
        for engine in engines {
            guard !engine.label.isEmpty else {
                throw RegistryError.invalidLabel("engine", engine.id)
            }
            try requireUniqueIDs(engine.models.map(\.id), label: "\(engine.id) model")
            guard engine.models.contains(where: { $0.id == engine.defaultModel }) else {
                throw RegistryError.invalidDefaultModel(engine.id)
            }
            for model in engine.models where model.label.isEmpty {
                throw RegistryError.invalidLabel("\(engine.id) model", model.id)
            }
        }
        for (languageID, language) in languages {
            guard !language.label.isEmpty else {
                throw RegistryError.invalidLabel("language", languageID)
            }
            for (engineID, backendLanguage) in language.engines {
                guard engineIDs.contains(engineID) else {
                    throw RegistryError.invalidLanguageEngine(languageID, engineID)
                }
                guard !backendLanguage.isEmpty else {
                    throw RegistryError.invalidLanguageEngine(languageID, engineID)
                }
            }
        }
    }

    private func requireUniqueIDs(_ ids: [String], label: String) throws {
        var seen = Set<String>()
        for id in ids {
            guard !id.isEmpty else {
                throw RegistryError.duplicateID(label, id)
            }
            guard seen.insert(id).inserted else {
                throw RegistryError.duplicateID(label, id)
            }
        }
    }

    func validLanguage(_ value: String?) -> String {
        guard let value, languages[value] != nil else {
            return defaultLanguage
        }
        return value
    }

    func validLanguage(_ value: String?, for engineID: String) -> String {
        let engineID = validEngine(engineID)
        if let value,
           let language = languages[value],
           language.engines[engineID] != nil {
            return value
        }
        if let defaultLanguage = languages[defaultLanguage],
           defaultLanguage.engines[engineID] != nil {
            return self.defaultLanguage
        }
        return supportedLanguages(for: engineID).first?.id ?? self.defaultLanguage
    }

    func supportedLanguages(for engineID: String) -> [(id: String, label: String)] {
        let engineID = validEngine(engineID)
        return languages
            .compactMap { id, language in
                language.engines[engineID] == nil ? nil : (id: id, label: language.label)
            }
            .sorted { $0.label < $1.label }
    }

    func validEngine(_ value: String?) -> String {
        guard let value, engines.contains(where: { $0.id == value }) else {
            return defaultEngine
        }
        return value
    }

    func engine(_ id: String) -> Engine {
        engines.first(where: { $0.id == id }) ?? engines.first(where: { $0.id == defaultEngine })!
    }

    func defaultModel(for engineID: String) -> String {
        engine(engineID).defaultModel
    }

    func validModel(_ model: String?, for engineID: String) -> String {
        let engine = engine(engineID)
        guard let model, engine.models.contains(where: { $0.id == model }) else {
            return engine.defaultModel
        }
        return model
    }

    func coerceModels(_ values: [String: String]) -> [String: String] {
        var result: [String: String] = [:]
        for engine in engines {
            result[engine.id] = validModel(values[engine.id], for: engine.id)
        }
        return result
    }

    func backendLanguage(_ language: String, engineID: String) -> String {
        let engineID = validEngine(engineID)
        let language = validLanguage(language, for: engineID)
        return languages[language]?.engines[engineID] ?? language
    }
}

enum RegistryError: Error, Equatable {
    case missingResource
    case unsupportedVersion(Int)
    case invalidDefaultEngine(String)
    case invalidDefaultLanguage(String)
    case invalidDefaultModel(String)
    case duplicateID(String, String)
    case invalidLanguageEngine(String, String)
    case invalidLabel(String, String)
}
