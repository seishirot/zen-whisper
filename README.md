# zen-whisper

Fully local voice-to-text input tool. Toggle recording with a hotkey, transcribe with Whisper, and paste into the active window. After dependencies and models are installed, audio stays on your machine.

完全ローカルの音声入力ツール。ホットキーでトグル録音 → Whisper で文字起こし → アクティブウィンドウにペースト。依存関係とモデルの取得後は、音声データを外部に送信せずローカルで処理します。

## Features

- **Hotkey toggle recording** — press to start, press again to stop (or auto-stop on silence via VAD)
- **Local ASR transcription** — no data leaves your machine after models are installed
- **Cross-platform** — Windows (CPU/Reazon K2 or faster-whisper, CUDA/faster-whisper) and macOS (Apple Silicon/mlx-whisper)
- **System tray** — runs in background with tray icon showing recording state
- **Microphone selection** — pick the recording input from the tray, including virtual mics like NVIDIA Broadcast
- **Floating overlay** — draggable microphone widget with real-time VAD visual feedback
- **Multi-language** — switch transcription language on the fly from tray menu
- **Sound feedback** — configurable start/stop tones or custom sound files

## Requirements

- **Python 3.11-3.13**
- **Windows**: CPU supported; NVIDIA GPU optional for faster-whisper CUDA
- **macOS**: Apple Silicon M1+ (for mlx-whisper / Metal)

## Installation

### 1. Install uv

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and install dependencies

```bash
git clone https://github.com/seishirot/zen-whisper.git
cd zen-whisper
uv sync
```

`uv sync` creates the local `.venv` environment and the launcher used by
`start.vbs` on Windows.

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

### 3. Create your config file

```bash
cp config.example.toml config.toml
```

Edit `config.toml` to customize hotkeys, model size, language, and other settings.

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
uv run zen-whisper
```

**Development** (with console output):
```bash
uv run python src/main.py
```

### Basic workflow

1. Launch zen-whisper — it appears in the system tray
2. Press the hotkey (default: `Shift+Space`) to **start recording**
3. Speak into your microphone
4. Press the hotkey again to **stop recording** (or wait for silence auto-stop)
5. Transcribed text is automatically pasted into the active window

### Tray menu

Right-click the tray icon to:
- Switch transcription language
- Select microphone input, or refresh the microphone list after devices change
- Select ASR engine: Whisper (GPU/CPU/MLX), Reazon K2, or Qwen3-ASR
- Toggle sound feedback
- Register/unregister startup
- Quit

## Configuration

All settings are in `config.toml`. See `config.example.toml` for defaults and descriptions.

| Section | Key settings |
|---|---|
| `[hotkey]` | `toggle` — recording hotkey (string or list), `switch_lang` — language switch hotkey |
| `[recognition]` | `engine` (`whisper`/`reazon-k2`/`qwen3-asr`), `language`, `model_size`, `compute_type`, `device` (`cuda`/`cpu`/`mlx`) |
| `[recording]` | `microphone`, `vad_silence_threshold_sec`, `min_audio_rms`, `min_audio_peak`, `max_recording_sec` |
| `[output]` | `restore_clipboard`, `paste_delay_ms` |
| `[feedback]` | `sound_enabled`, `sound_type` (`tone`/`custom`), `volume` |
| `[overlay]` | `enabled`, `position`, `size` |
| `[logging]` | `level`, `file` |

### ASR engines

- `engine = "whisper"` is the default. Select the engine and device explicitly from the tray menu.
- `engine = "whisper"` uses faster-whisper on Windows. When the resolved device is CPU, ZenWhisper forces `compute_type = "int8"` and passes `cpu_threads`.
- `device = "cuda"` is the Windows default, `device = "mlx"` is the macOS default. Select `Whisper > CPU (int8)` when you want the CPU Whisper path.
- `engine = "reazon-k2"` uses the fast Japanese CPU backend without PyTorch. Long audio is split into `reazon_chunk_sec` chunks with `reazon_trailing_silence_sec` silence appended to each chunk.
- `engine = "qwen3-asr"` keeps the existing Qwen3-ASR path for quality-focused experiments. The tray Qwen3-ASR entries target CUDA; use `--extra qwen3-cuda` for that path. `--extra qwen3` installs the CPU PyTorch variant for manual `config.toml` experiments with `device = "cpu"`.

The tray menu intentionally does not include an automatic ASR fallback mode. Unavailable backends such as Reazon K2 without the optional extra or CUDA without a visible GPU are disabled in the menu.

ZenWhisper does not run LLM cleanup or punctuation rewriting internally. It pastes the raw ASR text.

### Microphone selection

- `recording.microphone = ""` uses the current OS default input.
- Select `マイク` from the tray menu to save a specific microphone name to `config.toml`.
- If the selected microphone is unavailable at startup or recording time, ZenWhisper keeps the saved setting and falls back to the OS default input.
- The tray menu hides Windows low-level/pseudo inputs such as WDM-KS devices, Sound Mapper, and Primary Sound Capture Driver. On Windows, inactive capture endpoints are also filtered out when endpoint metadata is available.
- `recording.sample_rate` is the app-internal ASR/VAD processing rate and is currently fixed to 16kHz. Devices such as NVIDIA Broadcast may be opened at 48kHz and resampled before ASR.
- Recording start logs include `configured`, `actual_device`, `name`, `hostapi`, `stream_sr`, `target_sr`, `channels`, and `fallback_used`, so virtual inputs such as NVIDIA Broadcast can be verified in `zen-whisper.log`.
- Recording completion logs include `rms` and `peak`. Near-silent recordings below `[recording] min_audio_rms` and `min_audio_peak` are discarded before ASR to avoid silence hallucinations.

### Hotkey format

- Modifier keys: `win` (= `cmd` on macOS), `shift`, `ctrl`, `alt`
- Examples: `"shift+space"`, `"win+j"`, `"ctrl+alt+r"`
- Multiple hotkeys: `toggle = ["shift+space", "win+j"]`

### Custom sound files

To use custom start/stop sounds instead of generated tones:

1. Place your audio files (FLAC, WAV) in the `assets/` directory
2. Set `sound_type = "custom"` in `config.toml`
3. Update `custom_start_sound` and `custom_stop_sound` paths

## Troubleshooting

### Windows

- **CPU-only install**: plain `uv sync` does not install PyTorch or CUDA DLL packages. Select `Whisper > CPU (int8)` or set `device = "cpu"` for faster-whisper CPU, or install `uv sync --extra reazon` for the Torch-free Reazon K2 backend.
- **Torch DLL errors (`WinError 1114`)**: plain `uv sync --locked` should remove PyTorch from the base environment. If you install `--extra qwen3` or `--extra qwen3-cuda`, a broken PyTorch install can also break faster-whisper because CTranslate2 imports PyTorch when it is present.
- **CUDA errors**: install the CUDA extra with `uv sync --extra cuda` for faster-whisper CUDA, and ensure your NVIDIA GPU drivers are up to date.
- **Reazon K2 is disabled**: run `uv sync --extra reazon`, then restart ZenWhisper. The tray menu disables Reazon when the optional extra is unavailable.
- **ONNX Runtime API mismatch with Reazon**: use the locked dependencies from this repo. In particular, do not upgrade `sherpa-onnx` independently unless the Reazon path is re-tested on Windows.
- **No audio input**: Check that your microphone is set as the default recording device, or select it from the tray `マイク` menu.
- **NVIDIA Broadcast is not being used**: Select `マイク (NVIDIA Broadcast)` from the tray menu, then check the next recording start line in `zen-whisper.log` for the actual device name and host API.
- **Model loading timeout**: Large models may take time on first load. Increase `model_load_timeout_sec` if needed.

### macOS

- **Accessibility permission**: macOS requires accessibility permission for global hotkeys. Grant it in System Preferences > Privacy & Security > Accessibility.
- **Microphone permission**: Grant microphone access when prompted.
- **Input monitoring permission**: Required for keyboard event monitoring.

### General

- **Hallucination in silent recordings**: Near-silent recordings are discarded before ASR. Check `rms` and `peak` in `zen-whisper.log`; tune `min_audio_rms` and `min_audio_peak` in `[recording]`, plus `no_speech_threshold` and `hallucination_silence_threshold` in `[recognition]` if needed.
- **Logs**: Check `zen-whisper.log` for detailed error information.

## License

[MIT](LICENSE)
