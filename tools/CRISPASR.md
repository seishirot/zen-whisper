# Optional Windows CrispASR backend

CrispASR is an additional Windows x64 ASR engine. It keeps one model in a
resident native process and returns the final transcript after recording stops.
Whisper remains the default. Installing these artifacts does not change the
active engine or install Torch, Transformers, a CUDA toolkit, or a driver.

## Explicit setup

Run commands from the repository root with the declared mise toolchain and an
existing project environment. The CPU binary needs AVX2/FMA and the Microsoft
Visual C++ runtime listed in its import inventory. The CUDA binary additionally
needs a compatible NVIDIA driver. PowerShell 7 (`pwsh`) is recommended for the
read-only Authenticode check; setup fails if that check cannot report a status.

Start with the CPU runtime and Japanese Parakeet Q8:

```powershell
mise exec -- uv run --no-sync python tools/setup_crispasr.py --runtime cpu --model parakeet-ja-0.6b-q8
```

To explicitly add the bundled CUDA 12.8 runtime and Qwen3 1.7B Q8:

```powershell
mise exec -- uv run --no-sync python tools/setup_crispasr.py --runtime cuda --model qwen3-1.7b-q8
```

`--runtime` and `--model` can be repeated, or used individually to add only the
missing selection. With neither option, the command refuses to download.
`--root "tools/bin/crispasr/日本語 runtime"` selects another install directory.
Use the same root in the application settings. Models have fixed ASCII file
names; parent directories may contain Japanese characters and spaces.

Setup downloads only the selected public files, verifies their pinned SHA-256
and size, validates ZIP member paths, and records executable/DLL hashes,
normal and delay imports, Authenticode status, and actual `--version` output.
An existing file with different contents is preserved and reported as an
error; select a new root rather than mixing versions. Application startup and
recognition do not invoke setup. No system-wide PATH or security policy is
changed.

Default layout (all native assets are Git-ignored):

```text
tools/bin/crispasr/v0.8.32/
  cpu/crispasr-windows-x86_64-cpu/crispasr.exe
  cpu/receipt.json
  cuda/crispasr-windows-x86_64-cuda/crispasr.exe
  cuda/receipt.json
  models/<fixed-profile-file>.gguf
tools/downloads/crispasr-v0.8.32/<selected-runtime>.zip
```

## Select and switch back

For a direct tray switch, choose **エンジン → CrispASR → Qwen3 1.7B Q8
(or another model) → GPU (CUDA) / CPU**. The current engine label includes the
selected CrispASR model. Entries marked 未導入 need explicit setup for that
runtime and model. The separate **Qwen3-ASR (Transformers)** menu controls the
older Transformers engine; its availability does not control CrispASR Qwen.
Changing CrispASR profiles resets its decoder to auto and preserves the
Transformers model setting. The same profile's device switch preserves its
decoder selection.

In the Windows tray, open **設定 → 認識**, select `crispasr`, and explicitly
choose `cpu` or `cuda` in the common execution-target field. In the CrispASR
section, select the installed model and leave Decoder at `auto` unless testing
the Parakeet CTC head. Select **CUDAで使うGPU** by its card name in the common
section, or use the tray **GPU** submenu. This choice is shared by Whisper,
CrispASR and Qwen Transformers and is ignored for CPU. The GPU UUID is saved,
so index changes cannot silently redirect inference to another card. Save
applies the change and reloads the model. These values
persist in `config.toml` and are restored on the next application start.

Equivalent fields in an existing `[recognition]` section:

```toml
engine = "crispasr"
device = "cpu" # or "cuda"; never auto-fallback
language = "ja"
crispasr_root = "" # empty means the default layout above
crispasr_model = "parakeet-ja-0.6b-q8"
crispasr_decoder = "auto"
cuda_gpu_uuid = "" # Saved by the named GPU selector; shared across CUDA engines.
crispasr_gpu_device = 0 # Legacy fallback only when cuda_gpu_uuid is empty.
crispasr_timeout_sec = 120
crispasr_max_tokens = 512
cpu_threads = 4
model_load_timeout_sec = 300
```

To roll back, select the previous engine, device and model in the same settings
screen and save. The owned CrispASR worker is stopped when the model changes or
the app exits. Its files may remain for later use. Missing or invalid CrispASR
files produce a configuration/load error only when CrispASR is selected.
Existing Whisper, Reazon and Transformers settings retain their meaning.

## Verified model profiles

Sizes are decimal GB for the model file alone. Every entry has an immutable
Hugging Face revision, size, hash, source checkpoint and license in
[`src/crispasr_manifest.json`](../src/crispasr_manifest.json).

| Profile | Size | Native backend | Decoder | Language |
| --- | ---: | --- | --- | --- |
| `parakeet-ja-0.6b-q8` | 0.674 | `parakeet` | auto=TDT, TDT or CTC | ja |
| `parakeet-ja-0.6b-f16` | 1.247 | `parakeet` | auto=TDT, TDT or CTC | ja |
| `qwen3-1.7b-q8` | 2.507 | `qwen3-1.7b` | auto=greedy | ja, en |
| `qwen3-1.7b-f16` | 4.705 | `qwen3-1.7b` | auto=greedy | ja, en |
| `qwen3-0.6b-q8` | 1.007 | `qwen3` | auto=greedy | ja, en |
| `parakeet-ctc-ja-1.1b-q8` | 1.137 | `fastconformer-ctc` | auto=CTC | ja |

The 0.6B Japanese Parakeet checkpoint is NVIDIA's
`nvidia/parakeet-tdt_ctc-0.6b-ja`, converted by CrispStrobe (`cstr`), under
CC-BY-4.0. Qwen's source checkpoints and conversions are Apache-2.0. The 1.1B
Japanese CTC profile is the GAL Japanese fine-tune (Apache-2.0), based on
NVIDIA Parakeet CTC 1.1B (CC-BY-4.0). Keep the source attribution and applicable
notices if redistributing any model.

## Runtime contract and limitations

- One hidden process, one model, one server worker. The application uses
  authenticated HTTP on `127.0.0.1`; each load creates a new bearer secret
  passed in the child environment. There is no remote URL or proxy path.
- Audio is carried in memory as 16 kHz mono IEEE float32 WAV. No temporary
  recording or transcript file is written by the adapter. Empty and all-zero
  input returns empty text without decoding. Input is limited to 300 seconds.
- The child gets its own DLL search PATH and explicit device selection. CPU
  uses the CPU-only ZIP plus `--no-gpu`; CUDA requires actual CUDA backend
  initialization. Requested Parakeet CTC must be confirmed by the native log.
  Unexpected fallback, build/model mismatch or decode failure is an error.
- v0.8.32's Windows model loader uses a narrow filename API. The adapter starts
  it in the model's Unicode directory and passes an ASCII relative filename.
  A closed stdin pipe makes missing-model resolution non-interactive and
  download-refusing; Windows NUL must not be substituted for that pipe.
- SHA-256 verification happens at model load. Its time is included in reported
  cold load. Subsequent recordings reuse the process and loaded model.
- A Windows Job Object with kill-on-close owns the process tree. Startup
  timeout, request timeout, crash, unload and parent exit discard failed output
  and reap the worker. After a failure, reload the model or restart the app.
- Recognition context and hotwords are **unsupported** by this adapter. The
  settings screen and first hint-bearing request disclose this; hints are not
  sent or logged. Existing optional postprocessing is configured separately.
- Parakeet uses 10-second chunks; Qwen uses 30-second chunks and a 512-token
  default generation limit. Long-form quality and production adoption remain
  benchmark questions. Nonempty output does not mean error-free transcription.

## Reproduce the smoke and subsequent benchmark

The benchmark uses the normal `Transcriber` entry point. It downloads nothing
and never pastes text. It expects a pre-existing corpus manifest with `items`
containing `id`, `audio` (relative WAV path), and `reference`. Audio must already
be 16 kHz mono. The default is the fixed public corpus prepared by the existing
`tools/bench_reazon_production.py` workflow at
`tools/bench_outputs/reazon-production/corpus/public/manifest.json`.

```powershell
mise exec -- uv run --no-sync python tools/bench_crispasr.py --runtime cpu --model parakeet-ja-0.6b-q8 --decoder tdt --limit 5 --boundaries 12 25 60
mise exec -- uv run --no-sync python tools/bench_crispasr.py --runtime cuda --model qwen3-1.7b-q8 --limit 5 --boundaries 12 25 60 150
```

Check current free GPU capacity before running CUDA and allow headroom for
inference buffers. Do not stop another application to obtain benchmark capacity.

Use `--corpus`, `--root`, `--gpu-device`, `--timeout`, and `--output` when
needed. `--limit 30` selects the prepared 30 public clips; any private corpus
remains a separate local input. Report cold load, first
recognition warmup and subsequent recognition separately. The benchmark
records per-PID memory and loaded NVIDIA modules; optional `pwsh` GPU counters
are sampled outside ASR timing. Missing counter samples mean unavailable
evidence, not zero GPU use. CUDA graph-capture logging is another independent
signal and is not present for every valid CUDA graph.

Each run writes an ignored `summary.json` with timings, CER, IDs and runtime
evidence, plus `transcripts.local.json` with references and raw outputs. Keep
all run artifacts out of Git, including aggregate scores, derived charts,
recommendations, audio, raw outputs, native logs, model files and binaries. For long
cases it uses existing duration fixtures, or concatenates complete public
utterances and pads with silence. The exact last-sentence metric can fail on
numeral spelling alone; inspect the local outputs when interpreting it.

## Local validation workflow

See [local ASR benchmark instructions](ASR_BENCHMARKS.md) for corpus setup,
comparison profiles, output handling and measurement limitations. Source code,
public usage documentation and synthetic unit tests are tracked; individual
benchmark runs and reports stay in `tools/bench_outputs/` (Git-ignored).
Runtime verification receipts are generated locally by setup.

## GPU selection and VRAM failures

With multiple detected NVIDIA GPUs, the Windows tray has a **GPU** submenu showing each card's name, total VRAM
and current free VRAM. Use **GPU一覧・空き容量を更新** to refresh these values and
**モデルを再読み込み** to retry after freeing resources. The settings window
provides the same selection under **認識 → 共通 → CUDAで使うGPU**. Selecting a GPU
does not change the model or switch a CPU engine to CUDA. A missing selected
card or a failed identity check stops loading; there is no automatic switch to
another GPU. Legacy configurations retain their original behavior until a named
selection is saved.

CrispASR checks current free dedicated VRAM before creating its native process.
For v0.8.32 the conservative free-memory budgets include inference buffers and
headroom: Qwen 1.7B Q8 7 GiB, F16 12 GiB, Qwen 0.6B Q8 3.5 GiB, Parakeet 0.6B
Q8 2 GiB / F16 3 GiB, and Parakeet 1.1B Q8 4 GiB. These budgets are
conservative implementation estimates, not universal model requirements. They
do not reserve VRAM against other applications or
prove that every input will fit. Other backends use their native allocation
errors; the budget table applies specifically to CrispASR.

CrispASR startup, decode failure and detected CUDA OOM release the owned native
process tree. The application reports the free-memory shortfall or allocation
failure, blocks recording with the unavailable model, and allows an explicit
retry. A failed cleanup retains ownership and blocks the next heavyweight model
load rather than silently continuing. No model is automatically quantized,
moved to CPU or sent to another GPU. A failed model switch leaves recognition
unavailable until a valid selection loads successfully.

The GPU name selector maps the saved UUID to the current process CUDA ordinal
for Whisper and Qwen Transformers using the
[NVIDIA device APIs](https://docs.nvidia.com/cuda/cuda-driver-api/group__CUDA__DEVICE.html).
Windows CUDA capability detection runs in a short-lived child process so the
tray does not initialize the inference runtime before its model-loading thread.
