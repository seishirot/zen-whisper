# Contributing to zen-whisper

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/seishirot/zen-whisper.git
cd zen-whisper

# Install dependencies (including dev)
uv sync --group dev

# Create your config
cp config.example.toml config.toml
```

## Running

```bash
# Development mode (with console output)
uv run python src/main.py

# Production mode (no console)
uv run zen-whisper
```

## Running Tests

```bash
uv run pytest tests/
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
4. Run the tests (`uv run pytest tests/`)
5. Commit your changes with a clear message
6. Push to your fork and open a Pull Request

## Reporting Issues

- Use GitHub Issues
- Include your OS, Python version, and GPU info (if relevant)
- Include relevant log output from `zen-whisper.log`
