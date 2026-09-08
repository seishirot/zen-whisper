# Repository instructions

## Local configuration and evaluation data

- Track reusable source code, public documentation and synthetic test fixtures.
- Keep machine-specific settings in the ignored `config.toml`. Use placeholders
  in `config.example.toml`; do not copy actual settings, identifiers or secrets
  into tracked files, including this document.
- Store all local evaluation inputs and outputs under `tools/bench_outputs/`,
  which is Git-ignored. Use `private/` below it for personal inputs and reports,
  and separate run directories for reproducible local comparisons.
- This includes audio, reference text, transcripts, manifests, audio hashes,
  logs, aggregate metrics, comparison tables, plots and evaluation conclusions.
  Removing raw text or aggregating results does not make private-derived data
  suitable for publication. Keep it out of source comments, tests, tracked
  documentation, commit messages and public PR/release descriptions as well.
- Keep local reports in the ignored directory. Public documentation should
  explain setup and reproduction with generic paths and synthetic examples.
  Do not publish local results without explicit authorization covering the
  particular artifact and destination.
- Do not use `git add -f` for local artifacts or remove their ignore rules to
  make them trackable. Preserve local inputs and results during Git cleanup.

## Before committing or pushing

- Inspect staged paths and content with `git diff --cached --name-status` and
  `git diff --cached`. Confirm local artifacts are ignored with `git check-ignore`
  and absent from `git ls-files`; ignore rules do not untrack existing files.
- Inspect every outgoing commit, not only the current working tree. If personal
  data entered unpublished commits, remove it from the outgoing history before
  pushing. A later deletion commit does not remove it from earlier commits.
- Keep the repository's existing synthetic audio fixtures distinct from personal
  recordings. Never replace a public fixture with a user's recording.
