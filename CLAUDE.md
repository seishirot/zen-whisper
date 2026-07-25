# CLAUDE.md

## What is zen-whisper

Windows / macOS 対応の完全ローカル音声入力ツール。ホットキーでトグル録音 → Whisper で文字起こし → クリップボード経由でアクティブウィンドウにペースト。

## Commands

```bash
uv sync                    # 依存インストール（WindowsはTorchなし / Python 3.11-3.13）
uv sync --extra cuda       # Windows CUDA DLL（faster-whisper CUDA用）
uv sync --extra reazon     # Windows CPU高速モード（Reazon K2）を有効化
uv sync --extra qwen3      # Qwen3-ASR CPU PyTorch（手動 config 実験用）
uv sync --extra qwen3-cuda # Qwen3-ASR CUDA PyTorch
uv run zen-whisper         # 起動（コンソール非表示）
uv run python src/main.py  # 起動（開発用・コンソール付き）
uv run pytest tests/       # テスト
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
tools/             # 検証・ベンチ用スクリプト（本体非依存）。詳細は tools/README.md
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

## ASR エンジンと性能（調査メモ 2026-05 / CPU拡張 2026-06）

エンジンは `whisper`（既定）, `reazon-k2`, `qwen3-asr`。トレイメニューは
Whisper (GPU, CPU, MLX) / Reazon K2 / Qwen3-ASR (1.7B, 0.6B) の階層。

- 自動エンジン選択はUX上採用しない。ユーザーがトレイメニューでモデル/実行先を明示的に選ぶ。
- `reazon-k2`: Windows CPU高速モード。`uv sync --extra reazon` が必要。長音声は `reazon_chunk_sec` ごとに分割し、末尾に `reazon_trailing_silence_sec` の無音を足す。
- `whisper`: Windows は faster-whisper、macOS は MLX。CPU解決時は `compute_type="int8"` と `cpu_threads` を明示する。
- `qwen3-asr`: 精度・文脈プロンプト実験用。Windows では Transformers バックエンドで頭打ち。
- Windows の通常 `uv sync` は PyTorch を入れない。録音VADは同梱 Silero ONNX + `sherpa-onnx` を使う。macOS は MLX 経路の依存が PyTorch を持つ可能性がある。
- Torch が壊れている環境では、PyTorch が存在するだけで CTranslate2/faster-whisper CPU も巻き添えで失敗し得る。通常環境では Torch を入れず、Qwen3 extra のみに閉じ込める。
- CUDA 依存は `cuda` / `qwen3-cuda` extra で明示する。
- トレイの Qwen3-ASR 項目は CUDA 向け。CPU で Qwen3 を試す場合は `uv sync --extra qwen3` と `device="cpu"` の手動 config 実験として扱う。

Reazon extra は ReazonSpeech の `pkg/k2-asr` を commit
`2d4d4762e7ee294ac8e47a177ac2e9b0e8d0d43f` に固定する。Windows/Python 3.13 の
ORT API不整合を避けるため、通常依存で `sherpa-onnx==1.13.1` と `sherpa-onnx-core==1.13.1` も固定する。
`sherpa-onnx` を単独で上げる場合は Windows 実機で Reazon の import/load/実音声を再検証すること。

プロファイルは `profiles/*.toml` のデータ専用設定。対応 ASR へのヒントと、
明示した `replace_from` の辞書置換で共用する。CLI 後処理は既定オフで、
`postprocessors.default.toml` とローカルの `postprocessors.toml` から汎用
`shell=False` コマンドとして読み込む。外部送信／送信先不明のプリセットを
有効にすると、認識結果・文脈・辞書データが指定 CLI に渡る旨を表示する。
後処理失敗時は辞書置換までの結果へフォールバックし、送信付きホットキーの
Enter はキャンセルする。

Windows/Python のトレイ `設定...` は `config.toml`、プロフィール、ローカル
CLIプリセットを構造化編集する。保存は同一ディレクトリの一時ファイルから
原子的に置換し、録音・文字起こし・後処理中は拒否する。ホットキーとログの
変更は再起動後、それ以外の対応項目は保存後に反映する。画面を開いた後に
トレイ側で設定が変わった場合は世代不一致で保存を拒否する。構文不正な既存
TOMLは上書きせず、正常な `config.toml` の未知フィールドは保持する。
local扱いのCLIでcommand/environmentを変えた場合は送信先をunknownへ戻す。
認識エンジン／実行先は現在導入済みの組合せだけを表示し、長いタブは縦
スクロール可能にする。モデル読込の閾値超過は警告であり、停止不能な
ネイティブロードを裏に残して次の重量モデルを並行ロードしてはならない。

Windows CUDA GPU 環境・19.6秒の合成音声での参考実測:

| エンジン | RTF(min) | 備考 |
|---|---|---|
| faster-whisper large-v3-turbo | ~0.035 | **速度最良（Qwen の約9倍速）。速度重視はこれ** |
| Qwen3-ASR 1.7B | ~0.30 | 精度特化用途（文脈プロンプト等）向け。既定 |
| Qwen3-ASR 0.6B | ~0.28 | 1.7B とほぼ同速（差は数%）。期待した高速化は得られない |

- **精度はこの合成音声では3者とも CER 0.9% で差が出なかった**（クリーンすぎて判別不能。実音声での精度比較は別途必要）。
- **実音声（約72.5秒）で Reazon K2 の本体経路は確認済み**（`engine_label == "reazon-k2"`、チャンク処理、339文字）。
- **0.6B が 1.7B とほぼ同速なのは、音声エンコーダ＋prefill が支配的で LLM 縮小の効きが小さいため**。「0.6B なら数倍速」は今回の尺では当てはまらない。
- **Qwen が遅い真因は LLM の自己回帰デコード（メモリ帯域律速）＋音声エンコード**。FlashAttention2 を入れても効果は限定的（prefill 側にしか効かない）。
- `qwen3_attn_implementation="auto"` は FA2 があれば使用、無ければ sdpa。Windows は sdpa（内部で flash カーネル使用）。
- `qwen3_torch_compile` は triton 必須のため **Windows では自動無効化**（`_is_triton_available()` ガード）。
- **劇的な高速化には vLLM バックエンドが必要だが Linux/WSL2/Docker のみ**（`qwen_asr` に `Qwen3ASRModel.LLM` / `qwen-asr-serve` あり）。Windows では Transformers バックエンドで頭打ち。

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
| `[overlay]` | `enabled`, `position`, `size` |
| `[logging]` | `level`, `file` |
