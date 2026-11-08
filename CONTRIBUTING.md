# Contributing to zen-whisper

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/seishirot/zen-whisper.git
cd zen-whisper

# Install dependencies (including dev)
uv sync --locked --group dev

# Create your config
cp config.example.toml config.toml
```

### Before your first push (required for contributors who push)

Install the pre-push gate once per clone. It is not needed to run the app or the
tests, only before you push commits:

```bash
mise install   # provides the pinned uv and gitleaks from .mise.toml
mise exec -- python -I tools/check_publish_safety.py install-hook
```

On every `git push`, the hook scans only the outgoing commits. It blocks
local-only paths (evaluation outputs, models, `config.toml`, logs, recordings),
e-mail addresses, user-profile paths, host/user names, GPU UUIDs and text copied
from your local private evaluation references, and then runs gitleaks for
secrets. Run the same check manually with
`mise exec -- python -I tools/check_publish_safety.py check`. If it blocks, remove
the data from the unpublished commits instead of using `git push --no-verify`.
See [AGENTS.md](AGENTS.md) for the local-data rules.

## Running

```bash
# Development mode (with console output)
uv run --locked python src/main.py

# Production mode (no console)
uv run --locked zen-whisper
```

## Running Tests

```bash
uv run --locked pytest tests/
```

## Project Structure

- `src/` — Main application code
- `src/platform/` — OS-specific implementations (Windows / macOS)
- `tests/` — pytest test suite
- `assets/` — Sound files

## Coding Style

- Follow existing code conventions in the project
- Use type hints where practical
- Platform-specific code goes in `src/platform/` — do not use `sys.platform` checks in other modules
- Keep thread safety in mind — the app is multi-threaded

## Submitting Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run the tests (`uv run --locked pytest tests/`)
5. Add a user-facing entry under `## [Unreleased]` in `CHANGELOG.md`
6. Commit your changes with a clear message
7. Push to your fork and open a Pull Request

## Releasing (maintainers)

Versions follow [Semantic Versioning](https://semver.org/); notes live in
[CHANGELOG.md](CHANGELOG.md).

```bash
mise exec -- python -I tools/release.py prepare 1.2.0   # moves [Unreleased], bumps pyproject.toml and uv.lock
uv run --locked pytest tests/
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore(release): v1.2.0"
git tag -a v1.2.0 -m "v1.2.0"
git push origin main v1.2.0
```

Optionally create a GitHub Release from the tag with the CHANGELOG section.

## Reporting Issues

- Use GitHub Issues
- Include your OS, Python version, and GPU info (if relevant)
- Include relevant log output from `zen-whisper.log`
