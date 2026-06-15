# tools/ — 検証・ベンチ用スクリプト

Qwen3-ASR や CPU 向け ASR の推論速度・精度を調査するための使い捨てツール群。
本体（`src/`）からは独立していて、アプリの動作には不要。リポジトリルートから実行する。

| ファイル | 用途 |
|---|---|
| `bench_cpu_asr.py` | CPU 向け候補（faster-whisper int8 / Kotoba faster / whisper.cpp / ReazonSpeech K2）を同一音声で比較 |
| `bench_compare.py` | faster-whisper large-v3-turbo vs Qwen3-ASR 1.7B/0.6B を速度(RTF)＋精度(CER)で横断比較 |
| `bench_qwen.py` | Qwen3-ASR の attn 実装別ベンチ（sdpa / eager / flash_attention_2 / torch.compile） |
| `diag_qwen.py` | Qwen が GPU/bf16 に正しく載っているか・どこが遅いかの切り分け |
| `verify_qwen_integration.py` | `Transcriber` 経由のロード／推論が通るか、torch.compile が triton 不在でも安全に無効化されるかの確認 |
| `samples/*.wav` | 計測用の 16kHz mono 合成音声（Windows SAPI 生成） |

```bash
# 例（リポジトリルートで）
mise exec -- uv run python tools\bench_compare.py
mise exec -- uv run python tools\bench_qwen.py sdpa
mise exec -- uv run python tools\bench_cpu_asr.py --audio tools\samples\bench_sample_ja.wav
mise exec -- uv run python tools\bench_cpu_asr.py --targets faster-whisper,kotoba
```

`bench_cpu_asr.py` は文字起こし本文を `tools/bench_outputs/*.txt` に保存し、速度・RTF・
ピークメモリ（取得できる環境のみ）を標準出力の表に出す。既定は低メモリ寄りで
`beam_size=1`, `cpu_threads=4`, `vad_filter=True`, `condition_on_previous_text=False`。

whisper.cpp は Python ライブラリではなく外部実行ファイルを呼び出すため、実行するには
`whisper-cli.exe` と量子化モデルファイルを指定する。

```bash
mise exec -- uv run python tools\bench_cpu_asr.py ^
  --targets whisper-cpp ^
  --whisper-cpp-exe tools\bin\whisper-cli.exe ^
  --whisper-cpp-model tools\models\ggml-large-v3-turbo-q5_0.bin
```

Kotoba-Whisper v2.0 は `fast-kotoba` target で `faster-whisper` 経由の
`kotoba-tech/kotoba-whisper-v2.0-faster` を試す。ReazonSpeech K2 はパッケージが
入っている場合だけ実行し、未導入なら SKIP する。通常はリポジトリのロック済み依存で
`mise exec -- uv sync --extra reazon` を使う。

プロジェクト依存を変更せずに一時依存で試す場合は、検証済みの commit と
Windows 互換の sherpa 依存を合わせて指定する。

```bash
mise exec -- uv run ^
  --with "reazonspeech-k2-asr @ git+https://github.com/reazon-research/ReazonSpeech@2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f#subdirectory=pkg/k2-asr" ^
  --with "sherpa-onnx==1.13.1" ^
  --with "sherpa-onnx-core==1.13.1" ^
  python tools\bench_cpu_asr.py --targets reazon-k2
```

`bench_cpu_asr.py` の Reazon target はベンチ確認用にファイル全体をそのまま渡すため、
長い音声では Reazon/K2 側の long audio warning が出ることがある。本体の
`src.asr.reazon.ReazonK2Backend` は `reazon_chunk_sec` ごとに分割し、各チャンク末尾に
`reazon_trailing_silence_sec` の無音を足して処理する。

既存の Qwen 系実行結果は `tools/bench_result.txt`（gitignore 対象）に出力される運用。

> メモ: vLLM バックエンドの計測は Linux/WSL2 環境が必要（Windows では `vllm` が動かない）。
