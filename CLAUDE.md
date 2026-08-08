# CLAUDE.md

## What is zen-whisper

Windows / macOS 対応の完全ローカル音声入力ツール。ホットキーでトグル録音 → Whisper で文字起こし → クリップボード経由でアクティブウィンドウにペースト。

## Commands

```bash
uv sync --locked                    # 依存インストール（WindowsはTorchなし / Python 3.11-3.13）
uv sync --locked --extra cuda       # Windows CUDA DLL（faster-whisper CUDA用）
uv sync --locked --extra reazon     # Windows CPU高速モード（Reazon K2）を有効化
uv sync --locked --extra qwen3      # Qwen3-ASR Transformers 5.14 + CPU PyTorch（実験用）
uv sync --locked --extra qwen3-cuda # Qwen3-ASR Transformers 5.14 + CUDA PyTorch
uv run --locked zen-whisper         # 起動（コンソール非表示）
uv run --locked python src/main.py  # 起動（開発用・コンソール付き）
uv run --locked pytest tests/       # テスト
```

## Project Structure

```
src/
  main.py          # エントリポイント（App クラス）
  config.py        # TOML 設定（tomllib + tomli-w）
  hotkey.py        # グローバルホットキー
  recorder.py      # 録音 + VAD（sounddevice + sherpa-onnx Silero ONNX）
  transcriber.py   # ASRバックエンド選択と共通ログ
  asr/             # ASR実装（faster-whisper / mlx-whisper / Reazon K2 / Qwen3-ASR）
  paster.py        # クリップボード退避 → ペースト → 復元
  tray.py          # システムトレイ（pystray）
  overlay.py       # フローティングウィジェット（tkinter）
  sounds.py        # 音声フィードバック（トーン生成 / ファイル再生）
  settings.py      # 構造化設定画面（tkinter）
  toml_storage.py  # config/profile/後処理の原子的TOML保存
  startup.py       # スタートアップ登録ディスパッチャ
  platform/
    __init__.py    # OS 判定（is_windows / is_mac）
    windows.py     # Win32 固有実装
    darwin.py      # macOS 固有実装
tests/             # pytest テスト
tools/             # 検証・ベンチ用スクリプト（本体からは呼び出されず、一部は src/ を利用）。詳細は tools/README.md
config.example.toml # 設定テンプレート（config.toml にコピーして使用）
```

## Processing Flow

```
hotkey.py → sounds.py(開始音) → recorder.py(録音+VAD) → sounds.py(停止音) → transcriber.py → profiles.py(任意の辞書置換) → postprocessing.py(任意のCLI) → paster.py
```

## Critical Implementation Notes

- **hotkey.py の `suppress_event()`**: `SuppressException` を raise する仕組み。**try/except で捕捉してはならない**
- **プラットフォーム分岐**: `sys.platform` 直接分岐は禁止。必ず `src/platform/` パッケージ経由で OS 固有コードを呼ぶ
- **main.py**: `pythonw.exe` で `sys.stdout/stderr` が `None` になる問題を冒頭で対処済み。壊さないこと

## ASR エンジンと性能（調査メモ 2026-05 / CPU拡張 2026-06 / Qwen native移行 2026-07）

エンジンは `whisper`（既定）, `reazon-k2`, `qwen3-asr`。トレイメニューは
Whisper (GPU, CPU, MLX) / Reazon K2 / Qwen3-ASR (1.7B, 0.6B) の階層。

- 自動エンジン選択はUX上採用しない。ユーザーがトレイメニューでモデル/実行先を明示的に選ぶ。
- `reazon-k2`: Windows CPU高速モード。`uv sync --locked --extra reazon` が必要。
  既定precisionは `int8-fp32` で、比較用に `int8` / `fp32` も選択可能。
  長音声は `reazon_chunk_sec` ごとに分割し、末尾に
  `reazon_trailing_silence_sec` の無音を足す。
- `whisper`: Windows は faster-whisper、macOS は MLX。CPU解決時は `compute_type="int8"` と `cpu_threads` を明示する。
- `qwen3-asr`: Windowsネイティブの非ストリーミング経路。
  Transformers 5.14の `AutoProcessor` + `AutoModelForMultimodalLM` と
  末尾が `-hf` の公式checkpointを使う。
- Windows の通常 `uv sync --locked` は PyTorch を入れない。録音VADは同梱 Silero ONNX + `sherpa-onnx` を使う。macOS は MLX 経路の依存が PyTorch を持つ可能性がある。
- Torch が壊れている環境では、PyTorch が存在するだけで CTranslate2/faster-whisper CPU も巻き添えで失敗し得る。通常環境では Torch を入れず、Qwen3 extra のみに閉じ込める。
- `cuda` は faster-whisper CUDA DLL、`qwen3-cuda` はCUDA PyTorch +
  native Transformers、`qwen3` はCPU PyTorch実験用として分離する。
- トレイの Qwen3-ASR 項目は CUDA 向け。CPU で Qwen3 を試す場合は
  `uv sync --locked --extra qwen3` を導入し、設定画面または `config.toml` で
  `device="cpu"` を選ぶ実験経路として扱う。

Reazon extra は ReazonSpeech の `pkg/k2-asr` を commit
`2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f` に固定する。Windows/Python 3.13 の
ORT API不整合を避けるため、通常依存で `sherpa-onnx==1.13.1` と `sherpa-onnx-core==1.13.1` も固定する。
`sherpa-onnx` を単独で上げる場合は Windows 実機で Reazon の import/load/実音声を再検証すること。

プロファイルは `profiles/*.toml` のデータ専用設定。対応 ASR へのヒントと、
明示した `replace_from` の辞書置換で共用する。CLI 後処理は既定オフで、
`postprocessors.default.toml` とローカルの `postprocessors.toml` から汎用
`shell=False` コマンドとして読み込む。外部送信／送信先不明のプリセットを
有効にすると、認識結果・文脈・辞書データが指定 CLI に渡る旨を表示する。
組み込みは Codex／Claude Code／Kiro CLI（外部送信）と Ollama（ローカル）。Codex は
`gpt-5.6-luna`／low reasoning を使い、approval・extensions・agent delegation・
shell tools・web searchを無効化する。Claude Code はsafe mode・tools無効・
session非保存の stdin 一回実行にし、`haiku` を校正用の軽量既定として明示する。
Kiro CLI は一時 `KIRO_HOME` にツール／MCPなしのcustom agentを生成し、system
promptを置換して`gpt-5.6-luna`／low effortを指定したstdin一回実行にする。実行前に
一時agentを検証して利用可能モデル一覧の完全一致を確認し、既知のagent／model
fallback警告をstderrで検出した場合は結果を採用しない。
プロンプト内のプロファイルと文字起こしは未信頼データとして実行ごとの
ランダム識別子付きタグで区切り、CLIにはMarkdownなしの校正本文だけを返させる。
汎用プリセットのモデル指定はCLIコマンド引数、Kiroは専用modelフィールドで上書きする。
後処理失敗時は辞書置換までの結果へフォールバックし、送信付きホットキーの
Enter はキャンセルする。CLI後処理が成功した場合も生成／外部変換結果を
確認せず送信しないようEnterをキャンセルする（辞書置換のみは送信可）。
CLI出力の改行・C0/C1制御文字・Unicode行区切りは貼り付け前に空白へ畳み、
埋め込みEnter／端末制御として作用させない。

Windows/Python のトレイ `設定...` は `config.toml`、プロフィール、ローカル
CLIプリセットを構造化編集する。保存は同一ディレクトリの一時ファイルから
原子的に置換し、録音・文字起こし・後処理中は拒否する。ホットキーとログの
変更は再起動後、それ以外の対応項目は保存後に反映する。画面を開いた後に
トレイ側で設定が変わった場合は世代不一致で保存を拒否する。構文不正な既存
TOMLは上書きせず、正常な `config.toml` の未知フィールドは保持する。
画面は `config.toml` と既定値を合わせた現在の有効値を表示し、空の任意値は
「OS既定」「無効」と明示する。固定の内部値は読み取り専用にする。設定変更が
あるときだけ config 保存を有効にし、保存前にフィールド単位の差分を表示して、
フォームで変更した管理対象フィールドだけを読み込み済みスナップショットへ
反映する。プロフィール／CLIの定義編集と config 上の使用中選択は明確に分ける。
認識タブとトレイ親メニューには現在の言語・エンジン・実行先を明示する。
local扱いのCLIでcommand/environmentを変えた場合は送信先をunknownへ戻す。
認識エンジン／実行先の選択候補は現在導入済みの組合せだけにし、未導入の
既存値は候補へ混ぜず警告付きで表示する。長いタブは縦スクロール可能にする。
モデル読込の閾値超過は警告であり、停止不能な
ネイティブロードを裏に残して次の重量モデルを並行ロードしてはならない。

Windows RTX 3090、PyTorch 2.10.0+cu126、Transformers 5.14.1、bf16、
sdpa、19.6秒の合成音声でのnative `-hf`参考実測
（warm-up除外、steady 3回）:

| エンジン | median RTF | P95 RTF | peak VRAM |
|---|---:|---:|---:|
| Qwen3-ASR 1.7B-hf | 0.449 | 0.459 | 4051 MiB |
| Qwen3-ASR 0.6B-hf | 0.571 | 0.628 | 1657 MiB |

- 合成音声では旧`qwen-asr 0.0.6`経路とnative経路のtranscript hashが両モデルで一致した。
- 72.5秒の実音声は`max_new_tokens=128`で打ち切られたため既定を256へ変更。
  256では両モデルとも末尾まで到達したが、1サンプルの文字列差から一般的な精度優位は判断しない。
- 同じ実音声のnative単発参考値は1.7BがRTF 0.274 / 4442 MiB、
  0.6BがRTF 0.362 / 2048 MiB。
- 既存のfaster-whisper large-v3-turbo参考値はRTF約0.035で、速度重視では引き続きこちらを使う。
- **実音声（約72.5秒）で Reazon K2 の本体経路は確認済み**（`engine_label == "reazon-k2"`、チャンク処理、339文字）。
- `qwen3_attn_implementation="auto"` は FA2 が利用可能なら使用し、無ければ sdpa。
  Windows の検証環境では FA2 なしの sdpa を使用した。実際の SDPA カーネルは
  PyTorch / CUDA の実行時条件に依存する。
- `qwen3_torch_compile` はCUDAでのTransformers生成時に`CompileConfig`を使い、
  CPU実行またはtritonが無い環境では自動無効化する。
- ZenWhisperが対応するのはWindows nativeの録音完了後一括認識。上流のstreamingは
  vLLM/Linux系の別経路であり、fallbackや配布要件にはしない。

## Config

`config.example.toml` → `config.toml` にコピーして使用。`config.toml` は `.gitignore` 対象。

| セクション | 主な設定 |
|---|---|
| `[hotkey]` | `toggle`（文字列 or リスト）、`submit_toggle`（停止時に貼り付け後 Enter）、`switch_lang` |
| `[recognition]` | `engine` (`whisper`/`reazon-k2`/`qwen3-asr`), `language`, `model_size`, `device` (`cuda`/`cpu`/`mlx`), `cpu_threads`, Reazon/Qwen設定 |
| `[recording]` | `microphone`, `vad_silence_threshold_sec`, `max_recording_sec` |
| `[output]` | `restore_clipboard`, `paste_delay_ms` |
| `[enhancement]` | `profile`（`profiles/*.toml` のID）、`postprocessor`（`off`/`dictionary`/プリセットID） |
| `[feedback]` | `sound_enabled`, `sound_type` (`tone`/`custom`), `volume` |
| `[overlay]` | `enabled`（`position` / `size` は互換予約値で現状無視） |
| `[logging]` | `level`, `file` |
