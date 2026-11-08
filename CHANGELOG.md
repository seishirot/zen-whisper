# Changelog

All notable changes to ZenWhisper are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The version is `[project].version` in `pyproject.toml`; releases are tagged
`vX.Y.Z`. The macOS backend package (`macos/backend`) is versioned separately.

## [Unreleased]

## [1.1.0] - 2026-11-08

### Added

- Experimental Windows CPU engine **Haas-ja (CPU)** using the Haas-ja-v1.0
  ONNX conversion with ONNX Runtime (`engine = "haas-ja"`, `int8` or
  `int4-onecomp`). The model is pinned to a full commit and every file is
  verified by SHA-256 on first load. It is not the default.
- Optional Windows CrispASR engine with explicit runtime/model setup, and a
  named NVIDIA GPU selector shared by the CUDA engines.
- Windows-native, non-streaming Qwen3-ASR through Transformers (`-hf` models).
- ReazonSpeech K2 fast CPU engine (optional `reazon` extra).
- Apple Silicon macOS menu bar app with native settings and postprocessing.
- Structured settings window, domain profiles with dictionary replacement and
  optional CLI postprocessors (Codex, Claude Code, Kiro CLI, Ollama).
- Submit-after-paste hotkey, recording elapsed-time overlay and RDP-safe
  external controls on Windows.
- Benchmark tooling that reports strict and lenient CER side by side, with a
  `rescore` command for retained runs.
- Pre-push privacy gate (`tools/check_publish_safety.py`) with gitleaks, and a
  Secret scan CI workflow.

### Changed

- Voice activity detection uses the bundled Silero ONNX model through
  sherpa-onnx instead of PyTorch; plain Windows installs no longer pull Torch.
- Python dependencies, build tooling and model sources are pinned and
  hash-verified (supply-chain hardening).
- Windows global hotkey lifecycle hardened; microphones are refreshed and
  recording onsets preserved.
- Distribution metadata declares `MIT AND Apache-2.0` and ships
  `LICENSES/` and `THIRD_PARTY_NOTICES.md`.

### Security

- Update locked dependencies to versions that fix published advisories:
  Pillow 13.0.0, certifi 2026.10.23, urllib3 2.8.0, anyio 4.14.2,
  filelock 4.2.6, fsspec 2026.9.0, msgpack 1.2.4, setuptools 84.2.1 and the
  benchmark-only pyarrow 23.0.1 (root and macOS backend locks). The minimum
  versions are enforced through `constraint-dependencies`.

### Fixed

- Model load failures are reported instead of failing silently.
- Windows launcher startup and macOS paste reliability.
- CrispASR startup issues found in review.

## [1.0.0] - 2026-06-15

- Initial public release.

[Unreleased]: https://github.com/seishirot/zen-whisper/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/seishirot/zen-whisper/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/seishirot/zen-whisper/releases/tag/v1.0.0
