# Local ASR comparisons

Track reusable runners, public setup instructions and synthetic tests in Git.
Keep all execution artifacts under `tools/bench_outputs/`, which is ignored:
audio, references, transcripts, manifests, hashes, logs, aggregate JSON, tables,
charts and adoption notes. Aggregation or removal of transcript text does not
make a private evaluation suitable for publication. Do not copy run results
into tracked documentation or use `git add -f` to include them.

Suggested local layout:

```text
tools/bench_outputs/
  reazon-production/corpus/public/  # prepared public inputs
  private/realwork-v1/             # optional personal inputs and references
  private/reports/                 # personal comparison reports and decisions
  crispasr/local-eval/             # frozen inputs, run summaries and logs
```

Prepare public inputs with the workflow in [tools/README.md](README.md).
Personal inputs are optional; prepare them locally only with their owner's
permission. Model downloads are explicit setup steps; inference runners do
not download missing models or change the live application configuration.
Use the declared mise environment from the repository root.

```powershell
mise exec -- uv run --no-sync python -X utf8 tools/bench_asr_compare.py freeze tools/bench_outputs/crispasr/local-eval/frozen.local.json
mise exec -- uv run --no-sync python -X utf8 tools/bench_asr_compare.py run --manifest tools/bench_outputs/crispasr/local-eval/frozen.local.json --profile reazon-k2 --device cpu --corpora public --output tools/bench_outputs/crispasr/local-eval/reazon-cpu
mise exec -- uv run --no-sync python -X utf8 tools/bench_asr_compare.py run --manifest tools/bench_outputs/crispasr/local-eval/frozen.local.json --profile qwen-1.7b-q8 --device cuda --gpu-device 0 --corpora public --output tools/bench_outputs/crispasr/local-eval/qwen-cuda
```

Choose a GPU with sufficient free VRAM before running the CUDA command. Do not
assume index 0 identifies a particular card. The comparison runner records and
checks the selected GPU UUID and stops when resource admission fails. Use fresh
output directories. Add `private` to `--corpora` only when evaluating the local
personal corpus. A frozen manifest verifies waveform hashes across runs.

Available profiles include Whisper Turbo, Reazon K2, CrispASR Parakeet/Qwen,
and `qwen-hf-1.7b` / `qwen-hf-0.6b` through the existing Transformers adapter.
See `run --help` for profile names, repeats and duration selections. Prepare
optional runtime dependencies in an isolated environment when the comparison
requires a different build; preserve the application's environment and lock.

Report corpus CER (total edits / total reference characters) separately from
mean clip CER, and distinguish cold loading from warm inference. Retain failed
and omitted outputs in quality accounting. Exact Latin/numeric spelling is a
separate metric. Record model revisions, precision, decoder/chunk settings,
runtime versions, CPU/GPU identity, timing and memory measurement scope locally.
Different checkpoints or decoding settings prevent a runtime-only comparison.
Equal aggregate CER does not establish identical text. Sampled VRAM counters
may miss brief peaks and do not necessarily include cold loading.

The comparison runner restricts output to `tools/bench_outputs/`; the CrispASR
smoke runner enforces the same boundary. Keep redirected console logs there as
well. A publication requires a separately prepared public-only artifact review;
private-derived scores and conclusions remain local even when no raw text is
included.
