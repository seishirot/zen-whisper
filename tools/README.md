# tools/ — 検証・ベンチ用スクリプト

Qwen3-ASR や CPU 向け ASR の推論速度・精度を調査するための使い捨てツール群。
本体からは呼び出されず、アプリの動作には不要。一部は`src/`の実装を直接利用する。
リポジトリルートから実行する。

| ファイル | 用途 |
|---|---|
| `bench_cpu_asr.py` | CPU 向け候補（faster-whisper int8 / Kotoba faster / whisper.cpp / ReazonSpeech K2）を同一音声で比較 |
| `bench_reazon_hotwords.py` | Reazon K2 の greedy / modified beam / 動的ホットワードを正例・負例で比較 |
| `bench_compare.py` | faster-whisper large-v3-turbo vs native `-hf` Qwen3-ASR 1.7B/0.6B を速度(RTF)＋精度(CER)で横断比較 |
| `bench_qwen.py` | native `-hf` Qwen3-ASR の attn 実装別ベンチ（sdpa / eager / flash_attention_2 / generation compile） |
| `bench_qwen_backend.py` | 旧`qwen-asr`とnative Transformersを隔離venvで比較し、median/P95/VRAM/出力hashをJSON化 |
| `diag_qwen.py` | native QwenがGPU/bf16へ正しく載っているか・どこが遅いかの切り分け |
| `verify_qwen_integration.py` | `Transcriber` 経由のnativeロード／推論と、triton不在時のcompile自動無効化を確認 |
| `samples/*.wav` | 計測用の 16kHz mono 合成音声（Windows SAPI 生成） |

```powershell
# 例（リポジトリルートで）
mise exec -- uv run --locked --extra qwen3-cuda python tools\bench_compare.py
mise exec -- uv run --locked --extra qwen3-cuda python tools\bench_qwen.py sdpa
mise exec -- uv run --locked --extra qwen3-cuda python tools\diag_qwen.py
mise exec -- uv run --locked --extra qwen3-cuda python tools\verify_qwen_integration.py
mise exec -- uv run --locked python tools\bench_cpu_asr.py --audio tools\samples\bench_sample_ja.wav
mise exec -- uv run --locked --no-sync python tools\bench_reazon_hotwords.py --term mise --term uv --term Python
mise exec -- uv run --locked python tools\bench_cpu_asr.py --targets faster-whisper,kotoba
```

`bench_reazon_hotwords.py` は本体設定を変えず、現在の Reazon と同じ greedy、
ホットワードなしの modified beam、スコア別の動的ホットワードを比較する。既定では
実録音 `tools/bench_outputs/recording_16k.wav` を正例、一般文の
`tools/samples/bench_sample_ja.wav` を誤挿入確認用の負例として使う。モデルは指定精度に
必要なファイルだけ Hugging Face から取得し、結果を
`tools/bench_outputs/reazon_hotwords.json` に保存する。`--include-static` を付けると、
録音ごとの動的指定に加えて、認識器ロード時のホットワードファイル経路も比較できる。

`bench_cpu_asr.py` は文字起こし本文を `tools/bench_outputs/*.txt` に保存し、速度・RTF・
ピークメモリ（取得できる環境のみ）を標準出力の表に出す。既定は低メモリ寄りで
`beam_size=1`, `cpu_threads=4`, `vad_filter=True`, `condition_on_previous_text=False`。

whisper.cpp は Python ライブラリではなく外部実行ファイルを呼び出すため、実行するには
`whisper-cli.exe` と量子化モデルファイルを指定する。

```powershell
mise exec -- uv run --locked python tools\bench_cpu_asr.py `
  --targets whisper-cpp `
  --whisper-cpp-exe tools\bin\whisper-cli.exe `
  --whisper-cpp-model tools\models\ggml-large-v3-turbo-q5_0.bin
```

Kotoba-Whisper v2.0 は `fast-kotoba` target で `faster-whisper` 経由の
`kotoba-tech/kotoba-whisper-v2.0-faster` を試す。ReazonSpeech K2 はパッケージが
入っている場合だけ実行し、未導入なら SKIP する。通常はリポジトリのロック済み依存で
`mise exec -- uv sync --locked --extra reazon` を使う。`uv run --with` は、
プロジェクト設定が発見される場合でも追加依存をcommit済みlockfileの外で一時解決し、
結果をレビュー・再現できないため、このリポジトリの検証手順には使用しない。

`bench_cpu_asr.py` の Reazon target はベンチ確認用にファイル全体をそのまま渡すため、
長い音声では Reazon/K2 側の long audio warning が出ることがある。本体の
`src.asr.reazon.ReazonK2Backend` は `reazon_chunk_sec` ごとに分割し、各チャンク末尾に
`reazon_trailing_silence_sec` の無音を足して処理する。

Qwen系の実行結果を保存する場合は、gitignore対象の
`tools/bench_outputs/qwen-native-compare/<run-id>/`をrun別の保存先として
`bench_qwen_backend.py --output <path>`に明示する。`--output`省略時は標準出力のみ。
実音声、transcript、モデルcacheはcommitしない。

`bench_qwen_backend.py`のlegacy/nativeは同じ環境へ入れない。
`qwen-asr==0.0.6`がTransformers 4.57.6を固定する一方、native側は5.14.1を
使うため、mise管理Pythonから作った別venvでそれぞれ実行する。通常のJSONには
transcript本文を含めず、必要時だけ`--include-transcript`を指定する。

> メモ: vLLMは上流streaming検証用のLinux系別経路であり、ZenWhisperの
> Windows native非ストリーミング経路には不要。
