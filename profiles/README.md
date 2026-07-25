# ZenWhisper profiles

Place one TOML file per scene/project in this directory. Files ending in
`.toml` are local user data and are ignored by Git. Restart ZenWhisper after
adding or editing a profile.

Copy `zen-whisper.toml.example` to `zen-whisper.toml` as a starting point.

- `canonical`: preferred spelling
- `spoken`: pronunciations/aliases supplied to supported ASR engines
- `replace_from`: exact ASR mistakes replaced before optional CLI processing
- `description`: context supplied to an optional CLI
- top-level `context`: project/person/scene background

Profile data never defines executable commands. Commands belong in the
separate trusted `postprocessors.toml` file.
