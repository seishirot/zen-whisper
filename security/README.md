# Supply-chain maintenance

ZenWhisper uses two committed runtime uv lockfiles at the repository root and in
`macos/backend`, plus `security/build-audit/uv.lock` for the isolated PEP 517
toolchain. Normal installs, CI, and launch fallbacks use `--locked`, so a stale or
missing lockfile is an error instead of an implicit dependency resolution.

## Controls

- uv is fixed to 0.11.32 by `.mise.toml`, `pyproject.toml`, and CI.
- New registry resolutions exclude distribution artifacts uploaded within the
  previous week. The committed lockfile remains the authority for clones.
- uv's known-malware check runs before sync installation.
- PyPI artifacts in each lockfile carry hashes; the Reazon source dependency is
  fixed to a full Git commit.
- Built-in ASR model repositories resolve to reviewed full commits with explicit
  allowlists. Before loading, every artifact declared in the SHA-256/size
  manifest is verified; other allowlisted metadata remains commit-pinned but is
  not locally re-hashed. The Transformers backend also disables remote code.
- PEP 517 uses an exact Hatchling version. Distribution builds additionally use
  the fully hashed `build-constraints.txt` closure, whose package/version set is
  mirrored into a dedicated lock and vulnerability audit.
- The native macOS installer verifies its bundled wheel manifest, installs the
  exported external requirements with `--require-hashes`, and uses `--no-config`
  to ignore discovered `pyproject.toml`/`uv.toml` files before installing the
  verified wheel with `--no-deps`.
- CI audits each shipped project/platform lock and uploads the JSON evidence.
- External GitHub Actions are fixed to full commit SHAs and run with read-only
  repository permissions.
- Dependabot proposes weekly uv-lock updates after a seven-day cooldown and
  checks GitHub Actions updates weekly.

These controls reduce exposure to newly published or replaced dependencies.
They do not detect unknown malware, prove that upstream source is benign, or
replace review of dependency/model update pull requests. Existing local model
directories and the user's local Hugging Face cache remain within the user's
machine trust boundary.

## Updating Python dependencies

1. Run the resolver only in a dedicated branch:

   ```bash
   mise exec -- uv lock --upgrade
   mise exec -- uv lock --project macos/backend --upgrade
   ```

2. Review both `pyproject.toml` and lockfile diffs. Confirm package source,
   licenses, new build backends, Git/URL dependencies, and unexpected platform
   forks.
3. Run the locked tests and audits for every affected platform/extra.
4. Do not merge an untriaged advisory. Prefer a fixed version. If no compatible
   fix exists and the affected path is unreachable, add a time-bounded entry to
   `audit-exceptions.toml` and the matching uv audit setting. The policy tests
   fail after its expiry.

`uv audit` and its malware service rely on public advisory data and are preview
features in uv 0.11.32. Treat a successful audit as one signal, not a complete
security guarantee.

## Updating GitHub Actions

Dependabot may propose Action updates, but each `uses:` reference must remain a
reviewed full commit SHA. Resolve both the release tag and its peeled target:

```bash
git ls-remote https://github.com/OWNER/REPOSITORY.git \
  'refs/tags/vX.Y.Z' 'refs/tags/vX.Y.Z^{}'
```

For an annotated tag, pin the peeled `^{}` commit rather than the tag-object SHA;
the latter is 40 hexadecimal characters but is not an executable Action commit.
Update `APPROVED_ACTION_REFS` in `tests/test_supply_chain_policy.py` in the same
review after checking the official release notes and repository.

## Updating the build backend

Change the exact Hatchling requirement in the root `pyproject.toml`,
`macos/backend/pyproject.toml`, `security/build-audit/pyproject.toml`, and
`security/build-constraints.in`, then regenerate the hashed closure and audit
lock:

```bash
mise exec -- uv pip compile security/build-constraints.in \
  --generate-hashes \
  --output-file security/build-constraints.txt
mise exec -- uv lock --project security/build-audit
```

Review every changed build dependency and hash before committing. Validate a
clean distribution build with:

```bash
mise exec -- uv build --project . --build-constraint security/build-constraints.txt --require-hashes
mise exec -- uv build --project macos/backend --build-constraint security/build-constraints.txt --require-hashes
```

## Updating built-in models

Model source metadata is committed in `src/model_manifest.json` and, for the
native macOS app, in both synchronized `model_registry.json` resources. For each
update:

1. resolve the upstream repository to a full 40-character commit;
2. review the repository license and every allowed filename, excluding Python
   source and other unnecessary files;
3. record the SHA-256 and byte size of every weight/ONNX artifact that a loader
   needs;
4. update or add the corresponding provenance tests;
5. run the locked unit tests and a real first-download/load/transcription smoke
   on each affected OS/device before release.

Every declared weight/ONNX artifact is read and SHA-256 checked, including
content-addressed cache entries. This detects corrupt or replaced cache bytes at
load time, but it does not defend against an attacker who already controls the
running process or changes files after verification.

The current Reazon manifest approves only the Japanese (`ja`) snapshot. The
settings UI accepts only `ja`; an existing `ja-en` configuration is migrated to
the pinned `ja` snapshot at startup with a warning. Direct unsupported values are
rejected rather than loading an unapproved remote snapshot. Bilingual support
can return after an official repository is reviewed and added with immutable
artifact metadata.

## Release provenance

There is currently no public automated release workflow in this repository.
SBOM generation and artifact attestations must be added to the workflow that
actually produces public binaries/packages; attaching attestations to unrelated
test artifacts would not establish release provenance.
