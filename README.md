# zen-whisper

Local-first voice-to-text input tool. Toggle recording with a hotkey, transcribe with Whisper, and paste into the active window. ASR audio stays on your machine after dependencies and models are installed. Optional command postprocessing is off by default and may send the transcript/profile data to the configured destination when explicitly enabled.

ローカル優先の音声入力ツール。ホットキーでトグル録音 → Whisper で文字起こし → アクティブウィンドウにペースト。ASR の音声データはローカルで処理します。任意の CLI 後処理は既定でオフで、明示的に有効化した場合だけ認識結果・プロファイル情報を設定先へ渡します。

## Features

- **Hotkey toggle recording** — press to start, press again to stop (or auto-stop on silence via VAD)
- **Local ASR transcription** — no data leaves your machine after models are installed
- **Cross-platform** — Windows (CPU/Reazon K2 or faster-whisper, CUDA/faster-whisper) and macOS native menu bar app (Apple Silicon/mlx-whisper and MLX Qwen3-ASR)
- **Domain profiles** — reusable project context, preferred spellings, pronunciations, and exact error mappings
- **Optional CLI cleanup** — generic shell-free presets, including Codex (remote) and Ollama (loopback local)
- **Tray / menu bar** — runs in the background with a Windows tray icon or macOS menu bar item showing recording state
- **Microphone selection** — pick the recording input from the Windows tray or macOS menu bar, including virtual mics like NVIDIA Broadcast
- **Floating overlay** — Windows/Python draggable microphone widget with real-time VAD visual feedback
- **Multi-language** — switch transcription language on the fly from the Windows tray or macOS menu bar
- **Python CLI sound feedback** — configurable start/stop tones or custom sound files

## Requirements

- **Windows / Python CLI**: Python 3.11-3.13; CPU supported; NVIDIA GPU optional for faster-whisper CUDA
- **macOS native app**: Apple Silicon M1+, `mise`-managed Python 3.12 from `.mise.toml`, Xcode Command Line Tools or Xcode, and a local code signing identity

## Installation

### Windows / Python CLI

Install `uv`:

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Clone and install dependencies:

```bash
git clone https://github.com/seishirot/zen-whisper.git
cd zen-whisper
uv sync
```

`uv sync` creates the local `.venv` environment and the launcher used by
`start.vbs` on Windows.

### macOS Native App

Install `mise` with an official method for your environment, then use the
repo-managed Python/uv toolchain. If you use the shell installer, inspect it
before running it.

```bash
# Example only; review the installer first.
curl https://mise.run | sh
git clone https://github.com/seishirot/zen-whisper.git
cd zen-whisper
mise trust .mise.toml
mise install
macos/scripts/create_local_codesign_cert.sh
macos/scripts/install_app.sh
open /Applications/zen-whisper.app
```

`create_local_codesign_cert.sh` creates the local signing identity. If macOS
does not list the identity as valid, either use an existing identity with
`ZEN_WHISPER_CODE_SIGN_IDENTITY=...` or rerun the script with
`ZEN_WHISPER_TRUST_LOCAL_CERT=1` after reviewing the local Code Signing trust
change.

On Windows, plain `uv sync` does not install PyTorch. The default recording VAD
uses a bundled Silero ONNX model through `sherpa-onnx`, so Windows CPU-only
installs avoid Torch DLL initialization issues.

For Windows NVIDIA CUDA support with faster-whisper:

```bash
uv sync --extra cuda
```

For the Windows fast CPU backend powered by ReazonSpeech K2:

```bash
uv sync --extra reazon
```

This extra pins the tested ReazonSpeech K2 commit and a Windows-compatible
Sherpa ONNX ASR path. The first Reazon run downloads the ASR model from Hugging
Face.

For Qwen3-ASR experiments:

```bash
uv sync --extra qwen3       # CPU PyTorch for manual config experiments
uv sync --extra qwen3-cuda  # CUDA 12.6 PyTorch on Windows/Linux
```

### Python CLI Config File

```bash
cp config.example.toml config.toml
```

On Windows, open `設定...` from the tray for normal configuration. Copying and
editing `config.toml` directly remains available for unsupported or scripted
setups.

## Usage

### Starting zen-whisper

**Windows** (no console window):
- Double-click `start.vbs` (uses `.venv\Scripts\zen-whisper.exe` after
  `uv sync`, with `uv run zen-whisper` as a fallback), or:
  ```bash
  uv run zen-whisper
  ```

**macOS**:
```bash
mise trust .mise.toml
mise install
macos/scripts/create_local_codesign_cert.sh
macos/scripts/install_app.sh
open /Applications/zen-whisper.app
```

The macOS daily-use target is the native menu bar app. The Python CLI is kept
for development and does not auto-copy or auto-paste on macOS.

**Development** (with console output):
```bash
# Windows / Python CLI
uv run python src/main.py

# macOS development
mise exec -- uv run python src/main.py
```

### Basic workflow

1. Launch zen-whisper — it appears in the Windows tray or macOS menu bar
2. Press the hotkey (default: `Shift+Space`) to **start recording**
3. Speak into your microphone
4. Press the hotkey again to **stop recording** (or wait for silence auto-stop)
5. The selected profile supplies supported ASR hints; optional dictionary/CLI
   postprocessing runs if enabled
6. If paste is allowed, the resulting text is pasted into the active window. On
   macOS native, unsafe or unverifiable targets fall back to copy-only or skip
   copying.

For Python CLI, set `[hotkey] submit_toggle` in `config.toml` to add an alternate
toggle that presses Enter after pasting when it is used to stop recording. In
the macOS native app, choose `Submit Hotkey` from the menu bar item.

On macOS, it appears in the menu bar instead of the Python tray.

### Tray / Menu Bar

Use the Windows tray icon or macOS menu bar item to:
- Switch transcription language
- Select microphone input, or refresh the microphone list after devices change
- Select ASR engine/model. Windows Python supports Whisper/Reazon K2/Qwen3-ASR entries; macOS native supports MLX Whisper and MLX Qwen3-ASR entries.
- Select a domain profile independently from postprocessing (Windows/Python tray)
- Select postprocessing: off, dictionary only, or a configured CLI preset (Windows/Python tray). The menu and tooltip keep `ローカル` / `外部送信` / `送信先不明` visible.
- Open the structured settings window to edit app settings, profiles, and
  arbitrary CLI postprocessors (Windows/Python tray)
- Toggle sound feedback (Windows/Python tray only)
- Register/unregister startup or Launch at Login
- Quit

## Configuration

Windows/Python settings can be edited from the tray's `設定...` window and are
stored in `config.toml`; see `config.example.toml` for defaults and
descriptions. Profile files and local CLI presets are edited from their own
tabs and stored in `profiles/*.toml` and `postprocessors.toml`. Writes use a
temporary file plus atomic replacement. Hotkey and logging changes require an
app restart; other supported settings are applied after saving. Saving is
blocked while recording, transcribing, or postprocessing. If a tray action
changes settings after the window was opened, a stale save is rejected and the
window asks for a reload. Malformed existing TOML is not overwritten, and
unknown future fields in valid `config.toml` files are retained. The macOS
native app settings are changed from the menu bar item and stored in macOS app
settings. Engine and device selectors only show runtimes available in the
current installation; install the relevant extra and restart ZenWhisper before
selecting an optional backend.

| Section | Key settings |
|---|---|
| `[hotkey]` | `toggle` — recording hotkey (string or list), `submit_toggle` — stop, paste, then press Enter, `switch_lang` — language switch hotkey |
| `[recognition]` | `engine` (`whisper`/`reazon-k2`/`qwen3-asr`), `language`, `model_size`, `compute_type`, `device` (`cuda`/`cpu`/`mlx`) |
| `[recording]` | `microphone`, `vad_silence_threshold_sec`, `min_audio_rms`, `min_audio_peak`, `max_recording_sec` |
| `[output]` | `restore_clipboard`, `paste_delay_ms` |
| `[enhancement]` | `profile` (a filename from `profiles/`), `postprocessor` (`off`/`dictionary`/preset ID) |
| `[feedback]` | `sound_enabled`, `sound_type` (`tone`/`custom`), `volume` |
| `[overlay]` | `enabled`, `position`, `size` |
| `[logging]` | `level`, `file` |

### Python CLI ASR Engines

- `engine = "whisper"` is the default. Select the engine and device explicitly from the Windows tray.
- `engine = "whisper"` uses faster-whisper on Windows. When the resolved device is CPU, ZenWhisper forces `compute_type = "int8"` and passes `cpu_threads`.
- `device = "cuda"` is the Windows default for the Python CLI. Select `Whisper > CPU (int8)` when you want the CPU Whisper path.
- `engine = "reazon-k2"` uses the fast Japanese CPU backend without PyTorch. Long audio is split into `reazon_chunk_sec` chunks with `reazon_trailing_silence_sec` silence appended to each chunk.
- `engine = "qwen3-asr"` keeps the existing Python Qwen3-ASR path for quality-focused experiments. The Windows/Python tray Qwen3-ASR entries target CUDA; use `--extra qwen3-cuda` for that path. `--extra qwen3` installs the CPU PyTorch variant for manual `config.toml` experiments with `device = "cpu"`.

The Windows tray intentionally does not include an automatic ASR fallback mode.
Unavailable backends such as Reazon K2 without the optional extra or CUDA
without a visible GPU are disabled in the menu.

### macOS Native Recognition Model Menu

The macOS native app uses MLX models and stores its selection in macOS app
settings instead of `config.toml`. Choose `Recognition Model` from the menu bar
item to select MLX Whisper or MLX Qwen3-ASR. It intentionally does not include
the Windows/Python CPU, CUDA, or Reazon K2 menu entries.

### Profiles and optional postprocessing

Postprocessing is `off` by default. Profile selection is independent, so a
profile can supply recognition hints while the raw ASR result is still pasted:

- faster-whisper receives the profile's canonical/spoken terms as `hotwords`
- MLX Whisper receives the compact term list as an initial prompt
- Qwen3-ASR receives the profile context and terms through its `context`
- Reazon K2 does not consume recognition hints; use dictionary-only or CLI
  postprocessing when correcting its output

This menu/config path currently applies to the Python application (primarily
the Windows tray). The separate native macOS app has not yet been wired to
these profile and command files.

Create and edit one profile per scene or project from `設定... >
プロフィール`. The equivalent local file is `profiles/<id>.toml`; see
`profiles/zen-whisper.toml.example`. Profile files are ignored by Git. Explicit
`replace_from` entries are applied once, longest match first. `spoken` aliases
are ASR hints and are not silently rewritten.

The bundled `postprocessors.default.toml` contains:

- `codex`: remote Codex CLI cleanup
- `ollama`: local `qwen3.5:4b` cleanup, pinned to
  `127.0.0.1:11434`

The Ollama preset first runs `ollama show qwen3.5:4b`. If the model is missing,
ZenWhisper stops and asks you to run `ollama pull qwen3.5:4b`; it does not let
`ollama run` implicitly fetch a model during voice input.

Add or override arbitrary CLIs from `設定... > 後処理CLI`. The editor keeps the
command, input mode, model arguments, environment, destination declaration, and
prompt free-form. The equivalent ignored file is `postprocessors.toml`:

```toml
[postprocessors.claude]
display_name = "Claude 校正"
command = 'claude -p "{{prompt}}"'
input_mode = "argument"
output_mode = "stdout"
timeout_sec = 30
data_destination = "remote"
prompt_template = """
次の文字起こしを保守的に校正し、本文だけ返してください。
文脈: {{context}}
用語:
{{terms}}
文字起こし:
{{transcript}}
"""
```

`command` is parsed with the current OS quoting rules before placeholder
substitution and is always executed with `shell = false`. Pipes, redirects, and
`&&` are not interpreted; explicitly invoke a wrapper script for a complex
flow. Supported placeholders are `{{prompt}}`, `{{transcript}}`, `{{context}}`,
`{{terms}}`, `{{profile_name}}`, and `{{language}}`. With `input_mode =
"stdin"`, put placeholders in `prompt_template`; `command` itself must remain
static so transcript/profile data cannot leak through the process command line.
Argument mode exposes the prompt in the child process command line. On Windows,
argument mode rejects `.cmd` / `.bat` launchers because the OS may parse their
arguments through `cmd.exe`; invoke the underlying `.exe`, `node`, or `python`
entry point instead.

`postprocessors.toml` is trusted executable configuration; profiles are
data-only and cannot define commands. Custom presets default to
`data_destination = "unknown"`. Replacing a bundled preset's `command` without
also declaring command-specific fields resets its destination to `unknown` and
clears inherited preflight/environment settings. Enabling a remote/unknown
preset displays a warning that the transcript, selected profile context, and
dictionary data are passed to that CLI. In the settings UI, changing the
command or environment of a preset currently classified as local also forces
its destination to `unknown`; classify it as local again only after separately
verifying the edited command and host.

If a CLI is missing, times out, exits nonzero, or returns empty output,
ZenWhisper pastes the dictionary-corrected fallback. A submit-after-paste
hotkey cancels Enter in that failure case so unreviewed fallback text is not
sent automatically. Transcript and CLI output bodies are not written to the
ZenWhisper log. A timeout stops ZenWhisper waiting for the invoked process, but
cannot retract data already handed to a CLI or guarantee cancellation inside an
external/local model service.

### Microphone selection

- `recording.microphone = ""` uses the current OS default input.
- Select `マイク` from the Windows/Python tray menu to save a specific microphone name to `config.toml`. In the macOS native app, select `Microphone` from the menu bar item; the choice is stored in macOS app settings.
- If the selected microphone is unavailable at startup or recording time, ZenWhisper keeps the saved setting and falls back to the OS default input.
- The Windows/Python tray menu hides Windows low-level/pseudo inputs such as WDM-KS devices, Sound Mapper, and Primary Sound Capture Driver. On Windows, inactive capture endpoints are also filtered out when endpoint metadata is available.
- `recording.sample_rate` is the app-internal ASR/VAD processing rate and is currently fixed to 16kHz. Devices such as NVIDIA Broadcast may be opened at 48kHz and resampled before ASR.
- Recording start logs include `configured`, `actual_device`, `name`, `hostapi`, `stream_sr`, `target_sr`, `channels`, and `fallback_used`, so virtual inputs such as NVIDIA Broadcast can be verified in `zen-whisper.log`.
- Recording completion logs include `rms` and `peak`. Near-silent recordings below `[recording] min_audio_rms` and `min_audio_peak` are discarded before ASR to avoid silence hallucinations.

### Hotkey format

- Modifier keys: `win` (= `cmd` on macOS), `shift`, `ctrl`, `alt`
- Examples: `"shift+space"`, `"win+j"`, `"ctrl+alt+r"`
- Multiple hotkeys: `toggle = ["shift+space", "win+j"]`
- Submit-after-paste toggle: `submit_toggle = "ctrl+shift+space"`; it only sends Enter when the key press stops an active recording

### Python CLI Custom Sound Files

To use custom start/stop sounds instead of generated tones:

1. Place your audio files (FLAC, WAV) in the `assets/` directory
2. Set `sound_type = "custom"` in `config.toml`
3. Update `custom_start_sound` and `custom_stop_sound` paths

## Troubleshooting

### Windows

- **CPU-only install**: plain `uv sync` does not install PyTorch or CUDA DLL packages. Select `Whisper > CPU (int8)` or set `device = "cpu"` for faster-whisper CPU, or install `uv sync --extra reazon` for the Torch-free Reazon K2 backend.
- **Torch DLL errors (`WinError 1114`)**: plain `uv sync --locked` should remove PyTorch from the base environment. If you install `--extra qwen3` or `--extra qwen3-cuda`, a broken PyTorch install can also break faster-whisper because CTranslate2 imports PyTorch when it is present.
- **CUDA errors**: install the CUDA extra with `uv sync --extra cuda` for faster-whisper CUDA, and ensure your NVIDIA GPU drivers are up to date.
- **Reazon K2 is disabled**: run `uv sync --extra reazon`, then restart ZenWhisper. The Windows/Python tray menu disables Reazon when the optional extra is unavailable.
- **ONNX Runtime API mismatch with Reazon**: use the locked dependencies from this repo. In particular, do not upgrade `sherpa-onnx` independently unless the Reazon path is re-tested on Windows.
- **No audio input**: Check that your microphone is set as the default recording device, or select it from the tray `マイク` menu.
- **NVIDIA Broadcast is not being used**: Select `マイク (NVIDIA Broadcast)` from the Windows/Python tray menu, then check the next recording start line in `zen-whisper.log` for the actual device name and host API.
- **Model loading warning**: Large native model constructors cannot be cancelled safely. If loading exceeds `model_load_timeout_sec`, ZenWhisper warns but keeps waiting and serializes later model changes so multiple heavyweight loads do not overlap. Increase the value to delay that warning.

### macOS

- **Accessibility permission**: Required for automatic paste target inspection and paste event dispatch. Open it from `Troubleshooting > Open Accessibility Settings` if paste is unavailable.
- **Microphone permission**: Grant microphone access when prompted.

### General

- **Hallucination in silent recordings**: Near-silent recordings are discarded before ASR. Check `rms` and `peak` in `zen-whisper.log`; tune `min_audio_rms` and `min_audio_peak` in `[recording]`, plus `no_speech_threshold` and `hallucination_silence_threshold` in `[recognition]` if needed. Use `hallucination_silence_threshold = "off"` (or leave its settings field blank) to disable that optional threshold.
- **Logs**: Check `zen-whisper.log` for detailed error information.

## License

[MIT](LICENSE)
