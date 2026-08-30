# tools/ — 検証・ベンチ用スクリプト

Qwen3-ASR や CPU 向け ASR の推論速度・精度を調査するための使い捨てツール群。
本体からは呼び出されず、アプリの動作には不要。一部は`src/`の実装を直接利用する。
リポジトリルートから実行する。

| ファイル | 用途 |
|---|---|
| `prepare_public_asr_corpus.py` | 固定Common Voice 8.0日本語test artifactを検証downloadし、30件以上の16kHz mono WAVとduration／境界compositeをlocal-onlyで生成 |
| `prepare_private_asr_corpus.py` | ユーザー実録音を上書きせず、長い無音で20件へ分割して原稿・変換条件・SHA-256を固定したlocal-only realwork-v1を生成 |
| `bench_reazon_production.py` | 現行Reazonのproduction-equivalent条件、単発入力長、thread、chunk境界、license/moduleをlocal-only・別process guard付きで測定 |
| `bench_asr_candidates.py` | 固定公開corpusでReazon、faster-whisper、Kotoba CTranslate2、Kotoba GGML＋whisper.cppを比較 |
| `bench_private_asr.py` | 固定realwork-v1でReazon threads 1/4、CPU競合、公式SenseVoice GGUF、実Transcriber経路を本文非保存で比較 |
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

# 固定Common Voice公開baselineをgitignore対象へ生成
mise exec -c 'uv sync --locked --group benchmark --extra reazon'
mise exec -c 'uv run --locked --no-sync python tools\prepare_public_asr_corpus.py'

# 固定Reazon int8-fp32 modelの必要3 ONNX＋tokensだけをhash検証付きで取得
mise exec -c 'uv run --locked --no-sync python tools\bench_reazon_production.py prepare-model'

# 任意のsynthetic corpusを生成（Windows SAPI、24 controlled prompts）
mise exec -c 'uv run --no-sync python tools\bench_reazon_production.py init-corpus'

# 準備済み公開artifactで測定。benchmark本体はdownloadしない
mise exec -c 'uv run --no-sync python tools\bench_reazon_production.py run --manifest tools\bench_outputs\reazon-production\corpus\public\manifest.json'

# 候補model/runtimeを明示的に準備し、同じ公開corpusで比較
mise exec -c 'uv run --locked --no-sync python tools\bench_asr_candidates.py prepare'
mise exec -c 'uv run --locked --no-sync python tools\bench_asr_candidates.py run'

# ユーザー実録音を固定private corpus化（再録前の検出3番を除外したrealwork-v1）
mise exec -c 'uv run --locked --no-sync python tools\prepare_private_asr_corpus.py --exclude-detected-index 3 --overwrite'

# 公式SenseVoice artifactの取得だけがnetworkを使う。runとverify-appはlocal artifactをhash検証
mise exec -c 'uv run --locked --no-sync python tools\bench_private_asr.py prepare-sensevoice'
mise exec -c 'uv run --locked --no-sync python tools\bench_private_asr.py run'
mise exec -c 'uv run --locked --no-sync python tools\bench_private_asr.py verify-app'
```

`bench_reazon_production.py` は `int8-fp32`、比較基準のinference thread 1、
25秒固定chunk、chunk末尾0.5秒silence、新規stream、空白連結をbaselineにする。
coldは別processで最低3回、warmは同一processで最低5回実行し、未分割
`10 / 20 / 25 / 30 / 45 / 60秒`、25／30秒のthread sweep、固定25秒と
24〜30秒silence-aware境界、local faster-whisperの
`condition_on_previous_text=False / True`を測定できる。各child processは
`--memory-ceiling-mb`と`--timeout-sec`で停止され、危険な長さで停止した後は
それより長いReazon入力を続行しない。

公開corpus準備はHugging Face
`japanese-asr/ja_asr.common_voice_8_0`のtest Parquetをfull revision、
size、SHA-256で固定し、30件をseed付きSHA-256順位と短／中／長duration binで
決定的に選ぶ。変換後WAVは16kHz mono PCM16で、manifestへsource row、
original／converted duration、reference、license evidence、source／WAV hash、
composition recipeを保存する。mirrorのdataset cardにlicense fieldがないため、
Common VoiceのCC0条件をupstream evidenceとして記録し、音声・referenceは
再配布せずlocal-onlyで扱う。

測定出力は`tools/bench_outputs/reazon-production/run-*/`のJSON、CSV、Markdown。
raw transcriptは既定では保存せずhashとCERだけを残す。必要な場合だけ
`--retain-private-transcripts`を付け、gitignore対象に保存する。実利用に近い
private自然dictationは将来の任意追加枠であり、公開baselineの完了条件ではない。
Reazonのlocked optional runtimeまたは固定snapshotがlocalに無ければ、
必要revisionとfilesを記録して`SKIP`し、benchmark本体は自動取得しない。

`bench_asr_candidates.py`の`prepare`だけがnetworkを使い、固定Kotoba
CTranslate2 model、公式Kotoba GGML q5_0、固定whisper.cpp Windows x64 runtimeを
hash検証付きで準備する。`run`はlocal-onlyで、CTranslate2系はmodelを保持したwarm
run、whisper.cppは配布可能な外部CLIとしてclipごとのcold起動を含めて測る。
summaryはprocess lifecycleを分けて表示し、transcript本文は保存せず、hash、CER、
latency、RAMだけをgitignore対象へ残す。

`bench_reazon_hotwords.py` は本体設定を変えず、現在の Reazon と同じ greedy、
ホットワードなしの modified beam、スコア別の動的ホットワードを比較する。既定では
実録音 `tools/bench_outputs/recording_16k.wav` を正例、一般文の
`tools/samples/bench_sample_ja.wav` を誤挿入確認用の負例として使う。モデルは指定精度に
必要なファイルだけ Hugging Face から取得し、結果を
`tools/bench_outputs/reazon_hotwords.json` に保存する。`--include-static` を付けると、
録音ごとの動的指定に加えて、認識器ロード時のホットワードファイル経路も比較できる。
private `--manifest`を渡すと各音声のreferenceからCERも計算する。transcript本文は
メモリ内でhash・文字数・CERへ変換し、JSONへ保存しない。

`prepare_private_asr_corpus.py`は元のM4A/WAVを変更せず、16kHz mono PCM16の
全体派生WAVとclip、source/clip hash、原稿、無音分割条件を
`tools/bench_outputs/private/realwork-v1/`へ保存する。このディレクトリは
Git対象外で、ユーザーの将来モデル比較用にlocal保持する。学習用途ではない。

`bench_private_asr.py`は通常時40試行ずつ、CPU競合下20試行ずつのReazon
threads 1/4、公式FunASR llama.cpp SenseVoiceSmall Q8、productionの
`Transcriber`経路を扱う。SenseVoiceはruntime、weight、FSMN-VADを完全な
revision/commitとSHA-256で固定する。公式GGUF cardのApache-2.0表記に加え、
元の公式SenseVoice weightのFunASR Model License v1.1義務も保守的に維持する。

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
