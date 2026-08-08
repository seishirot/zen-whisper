"""Structured tkinter settings window for the Python desktop application."""

from __future__ import annotations

import copy
import logging
import math
import threading
import tkinter as tk
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from tkinter import messagebox, ttk

from src.config import (
    ENGINE_QWEN3_ASR,
    ENGINE_REAZON_K2,
    ENGINE_WHISPER,
    POSTPROCESSOR_DICTIONARY,
    POSTPROCESSOR_OFF,
    QWEN3_MODEL_LARGE,
    QWEN3_MODEL_SMALL,
    AppConfig,
)
from src.hotkey import validate_hotkey_config
from src.postprocessing import (
    ADAPTER_GENERIC,
    ADAPTER_KIRO,
    DATA_DESTINATION_LOCAL,
    DATA_DESTINATION_REMOTE,
    DATA_DESTINATION_UNKNOWN,
    PostprocessorPreset,
    SUPPORTED_TEMPLATE_PLACEHOLDERS,
)
from src.profiles import Profile, ProfileTerm
from src.toml_storage import (
    FileFingerprint,
    MISSING_FILE_FINGERPRINT,
)
from src.transcriber import (
    available_recognition_devices,
    available_recognition_engines,
)

logger = logging.getLogger(__name__)

DISPLAY_DISABLED = "（無効）"
DISPLAY_DEFAULT_MICROPHONE = "（OS既定）"
POSTPROCESSOR_PLACEHOLDER_HELP = (
    (
        "transcript",
        "prompt",
        "辞書置換後の文字起こし本文（プロンプト内に必須）",
    ),
    (
        "context",
        "prompt",
        "選択プロフィールの文脈。未選択時は「（なし）」",
    ),
    (
        "terms",
        "prompt",
        "正規表記・説明・誤認識候補をまとめた用語一覧",
    ),
    (
        "profile_name",
        "prompt",
        "選択プロフィールの表示名",
    ),
    (
        "language",
        "prompt",
        "認識言語コード（ja / en など）",
    ),
    (
        "boundary",
        "prompt",
        "実行ごとに生成する未信頼データ境界用のランダム識別子",
    ),
    (
        "agent",
        "command",
        "Kiro adapterが実行ごとに生成する専用エージェント名",
    ),
    (
        "prompt",
        "command",
        "完成したプロンプト全体（argument方式のコマンド欄用）",
    ),
    (
        "system_prompt_file",
        "command",
        "実行ごとに作るsystem prompt一時ファイルのパス",
    ),
)
RECOGNITION_SELECTION_KEYS = frozenset(
    {
        "recognition.engine",
        "recognition.language",
        "recognition.device",
    }
)

READ_ONLY_CONFIG_FIELDS = frozenset(
    {
        ("recording", "sample_rate"),
        ("overlay", "position"),
        ("overlay", "size"),
    }
)

# UI variable -> AppConfig field, plus the label used in the change preview.
# Fields listed in READ_ONLY_CONFIG_FIELDS are displayed separately.
CONFIG_FORM_FIELDS: dict[str, tuple[str, str, str]] = {
    "hotkey.toggle": ("hotkey", "toggle", "録音トグル"),
    "hotkey.submit_toggle": (
        "hotkey",
        "submit_toggle",
        "貼り付け＋Enter",
    ),
    "hotkey.switch_lang": ("hotkey", "switch_lang", "言語切替"),
    "output.restore_clipboard": (
        "output",
        "restore_clipboard",
        "クリップボード復元",
    ),
    "output.paste_delay_ms": (
        "output",
        "paste_delay_ms",
        "貼り付け待機",
    ),
    "feedback.sound_enabled": (
        "feedback",
        "sound_enabled",
        "開始・停止音",
    ),
    "feedback.sound_type": (
        "feedback",
        "sound_type",
        "サウンド方式",
    ),
    "feedback.volume": ("feedback", "volume", "音量"),
    "feedback.custom_start_sound": (
        "feedback",
        "custom_start_sound",
        "録音開始音",
    ),
    "feedback.custom_stop_sound": (
        "feedback",
        "custom_stop_sound",
        "録音停止音",
    ),
    "overlay.enabled": ("overlay", "enabled", "録音オーバーレイ"),
    "logging.level": ("logging", "level", "ログレベル"),
    "logging.file": ("logging", "file", "ログファイル"),
    "recognition.engine": ("recognition", "engine", "認識エンジン"),
    "recognition.language": ("recognition", "language", "認識言語"),
    "recognition.device": ("recognition", "device", "実行先"),
    "recognition.model_size": (
        "recognition",
        "model_size",
        "Whisperモデル",
    ),
    "recognition.compute_type": (
        "recognition",
        "compute_type",
        "計算精度",
    ),
    "recognition.beam_size": (
        "recognition",
        "beam_size",
        "Beam size",
    ),
    "recognition.cpu_threads": (
        "recognition",
        "cpu_threads",
        "CPU threads",
    ),
    "recognition.model_load_timeout_sec": (
        "recognition",
        "model_load_timeout_sec",
        "モデル読込の警告",
    ),
    "recognition.reazon_language": (
        "recognition",
        "reazon_language",
        "Reazon言語モデル",
    ),
    "recognition.reazon_precision": (
        "recognition",
        "reazon_precision",
        "Reazon精度",
    ),
    "recognition.reazon_chunk_sec": (
        "recognition",
        "reazon_chunk_sec",
        "Reazonチャンク長",
    ),
    "recognition.reazon_trailing_silence_sec": (
        "recognition",
        "reazon_trailing_silence_sec",
        "Reazon末尾無音",
    ),
    "recognition.qwen3_model": (
        "recognition",
        "qwen3_model",
        "Qwen3モデル",
    ),
    "recognition.qwen3_max_new_tokens": (
        "recognition",
        "qwen3_max_new_tokens",
        "Qwen最大生成トークン",
    ),
    "recognition.qwen3_attn_implementation": (
        "recognition",
        "qwen3_attn_implementation",
        "Qwen Attention",
    ),
    "recognition.qwen3_torch_compile": (
        "recognition",
        "qwen3_torch_compile",
        "Qwen torch.compile",
    ),
    "recognition.no_speech_threshold": (
        "recognition",
        "no_speech_threshold",
        "無音判定しきい値",
    ),
    "recognition.condition_on_previous_text": (
        "recognition",
        "condition_on_previous_text",
        "直前テキストの引継ぎ",
    ),
    "recognition.hallucination_silence_threshold": (
        "recognition",
        "hallucination_silence_threshold",
        "幻覚抑制の無音長",
    ),
    "recording.microphone": ("recording", "microphone", "マイク"),
    "recording.vad_silence_threshold_sec": (
        "recording",
        "vad_silence_threshold_sec",
        "無音停止",
    ),
    "recording.min_recording_sec": (
        "recording",
        "min_recording_sec",
        "最短録音",
    ),
    "recording.min_audio_rms": (
        "recording",
        "min_audio_rms",
        "最小RMS",
    ),
    "recording.min_audio_peak": (
        "recording",
        "min_audio_peak",
        "最小Peak",
    ),
    "recording.max_recording_sec": (
        "recording",
        "max_recording_sec",
        "最大録音",
    ),
    "recording.max_recording_warning_pct": (
        "recording",
        "max_recording_warning_pct",
        "最大時間の警告",
    ),
    "enhancement.profile_label": (
        "enhancement",
        "profile",
        "使用中プロフィール",
    ),
    "enhancement.postprocessor_label": (
        "enhancement",
        "postprocessor",
        "使用中の後処理",
    ),
}


def changed_config_fields(
    baseline: Mapping[str, object],
    current: Mapping[str, object],
) -> tuple[str, ...]:
    """Return changed managed form keys in stable UI order."""
    return tuple(
        key
        for key in CONFIG_FORM_FIELDS
        if baseline.get(key) != current.get(key)
    )


def apply_changed_config_fields(
    baseline: AppConfig,
    parsed: AppConfig,
    changed_keys: tuple[str, ...] | list[str] | set[str],
) -> AppConfig:
    """Apply only explicitly changed UI fields to a config snapshot."""
    merged = copy.deepcopy(baseline)
    for key in changed_keys:
        field_spec = CONFIG_FORM_FIELDS.get(key)
        if field_spec is None:
            continue
        section_name, field_name, _label = field_spec
        source_section = getattr(parsed, section_name)
        target_section = getattr(merged, section_name)
        setattr(
            target_section,
            field_name,
            copy.deepcopy(getattr(source_section, field_name)),
        )
    return merged


def recognition_selection_text(
    engine: str,
    language: str,
    device: str,
) -> str:
    """Return an explicit human-readable recognition selection."""
    engine_label = {
        ENGINE_WHISPER: "Whisper",
        ENGINE_REAZON_K2: "Reazon K2",
        ENGINE_QWEN3_ASR: "Qwen3-ASR",
    }.get(engine, engine or "未選択")
    language_label = {
        "ja": "日本語 (ja)",
        "en": "English (en)",
    }.get(language, language or "未選択")
    device_label = {
        "cuda": "GPU (CUDA)",
        "cpu": "CPU",
        "mlx": "Apple MLX",
    }.get(device, device or "未選択")
    return f"現在の選択: {engine_label} / {language_label} / {device_label}"


@dataclass(frozen=True)
class SettingsSnapshot:
    config: AppConfig
    profiles: dict[str, Profile]
    postprocessors: dict[str, PostprocessorPreset]
    local_postprocessor_ids: frozenset[str] = frozenset()
    revision: int = 0
    config_fingerprint: FileFingerprint = MISSING_FILE_FINGERPRINT
    profile_fingerprints: dict[str, FileFingerprint] = field(
        default_factory=dict
    )
    postprocessors_fingerprint: FileFingerprint = (
        MISSING_FILE_FINGERPRINT
    )


def refresh_snapshot_resources(
    previous: SettingsSnapshot,
    refreshed: SettingsSnapshot,
) -> SettingsSnapshot:
    """Refresh resource files without blessing stale config form values."""
    return SettingsSnapshot(
        config=previous.config,
        profiles=refreshed.profiles,
        postprocessors=refreshed.postprocessors,
        local_postprocessor_ids=refreshed.local_postprocessor_ids,
        revision=previous.revision,
        config_fingerprint=previous.config_fingerprint,
        profile_fingerprints=refreshed.profile_fingerprints,
        postprocessors_fingerprint=refreshed.postprocessors_fingerprint,
    )


def refresh_snapshot_config(
    previous: SettingsSnapshot,
    refreshed: SettingsSnapshot,
) -> SettingsSnapshot:
    """Refresh config metadata without discarding resource editor state."""
    return SettingsSnapshot(
        config=refreshed.config,
        profiles=previous.profiles,
        postprocessors=previous.postprocessors,
        local_postprocessor_ids=previous.local_postprocessor_ids,
        revision=refreshed.revision,
        config_fingerprint=refreshed.config_fingerprint,
        profile_fingerprints=previous.profile_fingerprints,
        postprocessors_fingerprint=previous.postprocessors_fingerprint,
    )


def hotkey_value_to_text(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def parse_hotkey_value(value: str) -> str | list[str]:
    parts = [
        part.strip()
        for line in value.splitlines()
        for part in line.split(",")
        if part.strip()
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts


def lines_to_tuple(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(line.strip() for line in value.splitlines() if line.strip())
    )


def parse_environment(value: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, item_value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(
                f"環境変数の{line_number}行目は KEY=VALUE 形式で指定してください"
            )
        if key in environment:
            raise ValueError(f"環境変数 {key} が重複しています")
        environment[key] = item_value.strip()
    return environment


def environment_to_text(environment: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in environment.items())


def local_transport_changed(
    original: PostprocessorPreset | None,
    updated: PostprocessorPreset,
) -> bool:
    """Whether a local preset changed fields that can alter its destination."""
    return (
        original is not None
        and original.data_destination == DATA_DESTINATION_LOCAL
        and updated.data_destination == DATA_DESTINATION_LOCAL
        and (
            original.command != updated.command
            or original.environment != updated.environment
            or original.adapter != updated.adapter
            or original.model != updated.model
        )
    )


def reclassify_changed_local_preset(
    original: PostprocessorPreset,
    updated: PostprocessorPreset,
) -> tuple[PostprocessorPreset, tuple[str, ...]]:
    """Mark an edited local transport unknown without discarding new inputs."""
    command_changed = original.command != updated.command
    launch_contract_changed = (
        command_changed
        or original.adapter != updated.adapter
        or original.model != updated.model
    )
    cleared: list[str] = []
    preflight_command = updated.preflight_command
    preflight_failure_message = updated.preflight_failure_message
    environment = updated.environment

    if launch_contract_changed:
        if updated.preflight_command == original.preflight_command:
            preflight_command = ""
            if preflight_command != updated.preflight_command:
                cleared.append("事前確認コマンド")
        if (
            updated.preflight_failure_message
            == original.preflight_failure_message
        ):
            preflight_failure_message = ""
            if (
                preflight_failure_message
                != updated.preflight_failure_message
            ):
                cleared.append("事前確認の失敗メッセージ")
        if updated.environment == original.environment:
            environment = {}
            if environment != updated.environment:
                cleared.append("環境変数")

    return (
        replace(
            updated,
            data_destination=DATA_DESTINATION_UNKNOWN,
            preflight_command=preflight_command,
            preflight_failure_message=preflight_failure_message,
            environment=environment,
        ),
        tuple(cleared),
    )


class SettingsWindow:
    """One reusable settings window hosted by its own tkinter thread."""

    def __init__(
        self,
        snapshot_provider: Callable[[], SettingsSnapshot],
        on_save_config: Callable[
            [AppConfig, int, FileFingerprint],
            tuple[bool, str],
        ],
        on_save_profile: Callable[
            [Profile, FileFingerprint],
            tuple[bool, str],
        ],
        on_save_postprocessor: Callable[
            [PostprocessorPreset, FileFingerprint],
            tuple[bool, str],
        ],
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._on_save_config = on_save_config
        self._on_save_profile = on_save_profile
        self._on_save_postprocessor = on_save_postprocessor

        self._root: tk.Tk | None = None
        self._ready = threading.Event()
        self._show_requested = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._snapshot: SettingsSnapshot | None = None
        self._vars: dict[str, tk.Variable] = {}
        self._widgets: dict[str, tk.Widget] = {}
        self._status_var: tk.StringVar | None = None
        self._config_source_var: tk.StringVar | None = None
        self._config_changes_var: tk.StringVar | None = None
        self._recognition_summary_var: tk.StringVar | None = None
        self._save_config_button: ttk.Button | None = None
        self._config_form_loading = True
        self._config_form_baseline: dict[str, object] | None = None
        self._profile_context: tk.Text | None = None
        self._profile_tree: ttk.Treeview | None = None
        self._profile_terms: list[ProfileTerm] = []
        self._profile_editor_baseline: tuple[object, ...] | None = None
        self._loaded_profile_id = ""
        self._system_prompt_text: tk.Text | None = None
        self._prompt_text: tk.Text | None = None
        self._command_text: tk.Text | None = None
        self._environment_text: tk.Text | None = None
        self._postprocessor_editor_baseline: tuple[object, ...] | None = None
        self._loaded_postprocessor_id = ""
        self._profile_id_by_label: dict[str, str] = {}
        self._profile_label_by_id: dict[str, str] = {}
        self._postprocessor_id_by_label: dict[str, str] = {}
        self._postprocessor_label_by_id: dict[str, str] = {}
        self._scroll_canvases: dict[str, tk.Canvas] = {}

    def show(self) -> None:
        self._show_requested.set()
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._run_tk,
                    daemon=True,
                    name="zen-whisper-settings",
                )
                self._thread.start()
        root = self._root
        if root is None or not self._ready.is_set():
            return
        try:
            root.after(0, self._show_requested_window)
        except tk.TclError:
            logger.debug("設定画面を表示できませんでした", exc_info=True)

    def stop(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            root.after(0, root.destroy)
        except tk.TclError:
            pass

    def _run_tk(self) -> None:
        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            self._root = root
            root.title("ZenWhisper 設定")
            root.geometry("980x760")
            root.minsize(860, 640)
            root.protocol("WM_DELETE_WINDOW", self._hide_window)

            style = ttk.Style(root)
            if "vista" in style.theme_names():
                style.theme_use("vista")

            outer = ttk.Frame(root, padding=12)
            outer.pack(fill="both", expand=True)

            title = ttk.Label(
                outer,
                text="ZenWhisper 設定",
                font=("Segoe UI", 16, "bold"),
            )
            title.pack(anchor="w")
            ttk.Label(
                outer,
                text=(
                    "設定は検証後に保存します。録音・文字起こし・校正中は"
                    "保存できません。"
                ),
            ).pack(anchor="w", pady=(2, 10))
            self._config_source_var = tk.StringVar(
                master=root,
                value="config.toml の現在値を読み込んでいます..."
            )
            ttk.Label(
                outer,
                textvariable=self._config_source_var,
                foreground="#555555",
            ).pack(anchor="w", pady=(0, 8))

            notebook = ttk.Notebook(outer)
            notebook.pack(fill="both", expand=True)
            self._build_basic_tab(notebook)
            self._build_recognition_tab(notebook)
            self._build_recording_tab(notebook)
            self._build_advanced_tab(notebook)
            self._build_profile_tab(notebook)
            self._build_postprocessor_tab(notebook)
            root.bind_all(
                "<MouseWheel>",
                self._on_scrollable_mousewheel,
                add="+",
            )

            footer = ttk.Frame(outer)
            footer.pack(fill="x", pady=(10, 0))
            self._status_var = tk.StringVar(master=root, value="")
            status_frame = ttk.Frame(footer)
            status_frame.pack(side="left", fill="x", expand=True)
            ttk.Label(
                status_frame,
                textvariable=self._status_var,
                foreground="#555555",
            ).pack(anchor="w")
            self._config_changes_var = tk.StringVar(
                master=root,
                value="設定値の読み込み待ち"
            )
            ttk.Label(
                status_frame,
                textvariable=self._config_changes_var,
                foreground="#1F5F99",
            ).pack(anchor="w")
            ttk.Button(
                footer,
                text="現在値に戻す",
                command=self._discard_config_changes,
            ).pack(side="right", padx=(8, 0))
            self._save_config_button = ttk.Button(
                footer,
                text="設定を保存",
                command=self._save_config,
                state="disabled",
            )
            self._save_config_button.pack(side="right")

            root.withdraw()
            self._ready.set()
            root.after(0, self._show_requested_window)
            root.mainloop()
        except Exception:
            logger.exception("設定画面の初期化に失敗しました")
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            self._ready.set()
        finally:
            self._root = None

    def _show_window(self) -> None:
        root = self._root
        if root is None:
            return
        if root.state() == "withdrawn":
            self._refresh_snapshot()
        root.deiconify()
        root.lift()
        root.focus_force()

    def _hide_window(self) -> None:
        root = self._root
        if root is None:
            return
        pending_changes: list[str] = []
        changed_keys = self._config_changed_keys()
        if changed_keys:
            pending_changes.append(f"config.toml の設定: {len(changed_keys)}件")
        if self._profile_editor_changed():
            pending_changes.append("プロフィール定義")
        if self._postprocessor_editor_changed():
            pending_changes.append("CLIプリセット定義")
        if pending_changes and not messagebox.askyesno(
            "未保存の変更を破棄しますか？",
            (
                "次の未保存の変更があります。\n\n"
                + "\n".join(f"・{item}" for item in pending_changes)
                + "\n\n"
                "変更を破棄して設定画面を閉じますか？"
            ),
            icon="warning",
            parent=root,
        ):
            return
        root.withdraw()

    def _show_requested_window(self) -> None:
        if not self._show_requested.is_set():
            return
        self._show_requested.clear()
        self._show_window()

    def _settings_root(self) -> tk.Tk:
        root = self._root
        if root is None:
            raise RuntimeError("設定画面のTkルートが初期化されていません")
        return root

    def _new_string_var(self, key: str) -> tk.StringVar:
        variable = tk.StringVar(master=self._settings_root())
        self._vars[key] = variable
        variable.trace_add(
            "write",
            lambda *_args, tracked_key=key: (
                self._on_config_form_changed(tracked_key)
            ),
        )
        return variable

    def _new_bool_var(self, key: str) -> tk.BooleanVar:
        variable = tk.BooleanVar(master=self._settings_root())
        self._vars[key] = variable
        variable.trace_add(
            "write",
            lambda *_args, tracked_key=key: (
                self._on_config_form_changed(tracked_key)
            ),
        )
        return variable

    def _entry_row(
        self,
        parent: tk.Widget,
        row: int,
        label: str,
        key: str,
        *,
        width: int = 34,
        help_text: str = "",
        state: str = "normal",
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        entry = ttk.Entry(
            parent,
            textvariable=self._new_string_var(key),
            width=width,
            state=state,
        )
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if help_text:
            ttk.Label(
                parent,
                text=help_text,
                foreground="#666666",
            ).grid(row=row, column=2, sticky="w", padx=(10, 0), pady=4)
        return entry

    def _combo_row(
        self,
        parent: tk.Widget,
        row: int,
        label: str,
        key: str,
        values: tuple[str, ...] | list[str],
        *,
        state: str = "readonly",
        width: int = 32,
        help_text: str = "",
    ) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=4,
        )
        combo = ttk.Combobox(
            parent,
            textvariable=self._new_string_var(key),
            values=values,
            state=state,
            width=width,
        )
        combo.grid(row=row, column=1, sticky="ew", pady=4)
        if help_text:
            ttk.Label(
                parent,
                text=help_text,
                foreground="#666666",
            ).grid(row=row, column=2, sticky="w", padx=(10, 0), pady=4)
        return combo

    @staticmethod
    def _section(parent: tk.Widget, title: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.pack(fill="x", padx=10, pady=(10, 0))
        frame.columnconfigure(1, weight=1)
        return frame

    def _add_scrollable_tab(
        self,
        notebook: ttk.Notebook,
        title: str,
        *,
        padding: int = 0,
    ) -> ttk.Frame:
        container = ttk.Frame(notebook)
        notebook.add(container, text=title)

        canvas = tk.Canvas(
            container,
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(
            container,
            orient="vertical",
            command=canvas.yview,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = ttk.Frame(canvas, padding=padding)
        window_id = canvas.create_window(
            (0, 0),
            window=content,
            anchor="nw",
        )
        content.bind(
            "<Configure>",
            lambda event: canvas.configure(
                scrollregion=canvas.bbox("all"),
            ),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                window_id,
                width=event.width,
            ),
        )
        self._scroll_canvases[title] = canvas
        return content

    def _on_scrollable_mousewheel(self, event: tk.Event) -> str | None:
        """Scroll the containing tab when the pointer is over a child control."""
        if not event.delta or isinstance(event.widget, (tk.Text, ttk.Treeview)):
            return None
        scroll_canvases = set(self._scroll_canvases.values())
        widget: object | None = event.widget
        while widget is not None:
            if widget in scroll_canvases:
                scroll = getattr(widget, "yview_scroll", None)
                if callable(scroll):
                    scroll(-1 if event.delta > 0 else 1, "units")
                    return "break"
            widget = getattr(widget, "master", None)
        return None

    def _build_basic_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(notebook, "基本")

        hotkeys = self._section(tab, "ホットキー")
        self._entry_row(
            hotkeys,
            0,
            "録音トグル",
            "hotkey.toggle",
            help_text="複数指定はカンマ区切り",
        )
        self._entry_row(
            hotkeys,
            1,
            "貼り付け＋Enter",
            "hotkey.submit_toggle",
            help_text=f"{DISPLAY_DISABLED}で貼り付けのみ",
        )
        self._entry_row(
            hotkeys,
            2,
            "言語切替",
            "hotkey.switch_lang",
        )

        output = self._section(tab, "出力")
        ttk.Checkbutton(
            output,
            text="貼り付け後に元のクリップボードへ戻す",
            variable=self._new_bool_var("output.restore_clipboard"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        self._entry_row(
            output,
            1,
            "貼り付け待機 (ms)",
            "output.paste_delay_ms",
        )

        feedback = self._section(tab, "サウンド・オーバーレイ")
        ttk.Checkbutton(
            feedback,
            text="開始・停止音を鳴らす",
            variable=self._new_bool_var("feedback.sound_enabled"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        self._combo_row(
            feedback,
            1,
            "サウンド方式",
            "feedback.sound_type",
            ("tone", "custom"),
        )
        self._entry_row(feedback, 2, "音量 (0〜1)", "feedback.volume")
        ttk.Checkbutton(
            feedback,
            text="録音オーバーレイを表示",
            variable=self._new_bool_var("overlay.enabled"),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self._entry_row(
            feedback,
            4,
            "オーバーレイ位置",
            "overlay.position",
            help_text="config.toml の参照値（現行表示は下中央固定）",
            state="disabled",
        )
        self._entry_row(
            feedback,
            5,
            "オーバーレイサイズ",
            "overlay.size",
            help_text="config.toml の参照値（現行サイズは固定）",
            state="disabled",
        )

        logging_frame = self._section(tab, "ログ")
        self._combo_row(
            logging_frame,
            0,
            "ログレベル",
            "logging.level",
            ("DEBUG", "INFO", "WARNING", "ERROR"),
        )
        self._entry_row(logging_frame, 1, "ログファイル", "logging.file")
        ttk.Label(
            logging_frame,
            text="ホットキーとログ出力先の変更は再起動後に確実に反映されます。",
            foreground="#8A4B08",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_recognition_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(notebook, "認識")

        self._recognition_summary_var = tk.StringVar(
            master=self._settings_root(),
            value="現在の選択: 読み込み中..."
        )
        ttk.Label(
            tab,
            textvariable=self._recognition_summary_var,
            foreground="#1F5F99",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 0))

        common = self._section(tab, "共通")
        engine_combo = self._combo_row(
            common,
            0,
            "エンジン",
            "recognition.engine",
            available_recognition_engines(),
        )
        self._widgets["recognition.engine"] = engine_combo
        engine_combo.bind(
            "<<ComboboxSelected>>",
            self._on_recognition_engine_changed,
        )
        self._combo_row(
            common,
            1,
            "言語",
            "recognition.language",
            ("ja", "en"),
        )
        device_combo = self._combo_row(
            common,
            2,
            "実行先",
            "recognition.device",
            available_recognition_devices(ENGINE_WHISPER),
        )
        self._widgets["recognition.device"] = device_combo
        self._entry_row(
            common,
            3,
            "Whisperモデル",
            "recognition.model_size",
        )
        self._entry_row(common, 4, "Beam size", "recognition.beam_size")
        self._entry_row(common, 5, "CPU threads", "recognition.cpu_threads")
        self._entry_row(
            common,
            6,
            "モデル読込の警告 (秒)",
            "recognition.model_load_timeout_sec",
        )
        ttk.Label(
            common,
            text=(
                "未導入のエンジン・実行先は選択肢に表示されません。"
                "読込警告後も安全のため現在のロード完了を待ちます。"
            ),
            foreground="#666666",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

        reazon = self._section(tab, "Reazon K2")
        self._combo_row(
            reazon,
            0,
            "言語モデル",
            "recognition.reazon_language",
            ("ja",),
            help_text="ja-en は非対応。既存設定は起動時に ja へ移行",
        )
        self._combo_row(
            reazon,
            1,
            "精度",
            "recognition.reazon_precision",
            ("fp32", "int8", "int8-fp32"),
        )
        self._entry_row(
            reazon,
            2,
            "チャンク長 (秒)",
            "recognition.reazon_chunk_sec",
        )
        self._entry_row(
            reazon,
            3,
            "末尾無音 (秒)",
            "recognition.reazon_trailing_silence_sec",
        )

        qwen = self._section(tab, "Qwen3-ASR")
        self._combo_row(
            qwen,
            0,
            "モデル",
            "recognition.qwen3_model",
            (QWEN3_MODEL_LARGE, QWEN3_MODEL_SMALL),
            state="normal",
        )
        self._entry_row(
            qwen,
            1,
            "最大生成トークン",
            "recognition.qwen3_max_new_tokens",
        )
        self._combo_row(
            qwen,
            2,
            "Attention",
            "recognition.qwen3_attn_implementation",
            ("auto", "sdpa", "flash_attention_2", "eager"),
        )
        ttk.Checkbutton(
            qwen,
            text="torch.compile を使用",
            variable=self._new_bool_var("recognition.qwen3_torch_compile"),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

    def _build_recording_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(notebook, "録音")
        recording = self._section(tab, "録音・VAD")
        self._entry_row(
            recording,
            0,
            "マイク名",
            "recording.microphone",
            help_text=f"{DISPLAY_DEFAULT_MICROPHONE}はWindowsの既定入力",
        )
        self._entry_row(
            recording,
            1,
            "内部サンプルレート",
            "recording.sample_rate",
            help_text="ASR/VAD契約のため16kHz固定",
            state="disabled",
        )
        self._entry_row(
            recording,
            2,
            "無音停止 (秒)",
            "recording.vad_silence_threshold_sec",
        )
        self._entry_row(
            recording,
            3,
            "最短録音 (秒)",
            "recording.min_recording_sec",
        )
        self._entry_row(
            recording,
            4,
            "最小RMS",
            "recording.min_audio_rms",
        )
        self._entry_row(
            recording,
            5,
            "最小Peak",
            "recording.min_audio_peak",
        )
        self._entry_row(
            recording,
            6,
            "最大録音 (秒)",
            "recording.max_recording_sec",
        )
        self._entry_row(
            recording,
            7,
            "最大時間の警告 (%)",
            "recording.max_recording_warning_pct",
        )
        ttk.Label(
            recording,
            text="空欄ではなく、現在有効なconfig.tomlの値を表示しています。",
            foreground="#666666",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_advanced_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(notebook, "詳細")

        whisper = self._section(tab, "Whisper 詳細")
        self._combo_row(
            whisper,
            0,
            "計算精度",
            "recognition.compute_type",
            ("float16", "int8", "int8_float16", "float32"),
            state="normal",
        )
        self._entry_row(
            whisper,
            1,
            "無音判定しきい値",
            "recognition.no_speech_threshold",
            help_text="0〜1",
        )
        ttk.Checkbutton(
            whisper,
            text="直前の文字起こしを次の推論へ引き継ぐ",
            variable=self._new_bool_var(
                "recognition.condition_on_previous_text"
            ),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        self._entry_row(
            whisper,
            3,
            "幻覚抑制の無音長 (秒)",
            "recognition.hallucination_silence_threshold",
            help_text=f"{DISPLAY_DISABLED}で使用しない",
        )

        custom_sound = self._section(tab, "カスタムサウンド")
        self._entry_row(
            custom_sound,
            0,
            "録音開始音",
            "feedback.custom_start_sound",
        )
        self._entry_row(
            custom_sound,
            1,
            "録音停止音",
            "feedback.custom_stop_sound",
        )
        ttk.Label(
            custom_sound,
            text=(
                "サウンド方式が custom のときに使用します。"
                "相対パスはZenWhisperのフォルダー基準です。"
            ),
            foreground="#666666",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_profile_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(
            notebook,
            "プロフィール",
            padding=10,
        )
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(6, weight=1)

        ttk.Label(
            tab,
            text="config.toml の使用中",
        ).grid(row=0, column=0, sticky="w", pady=4)
        active_combo = ttk.Combobox(
            tab,
            textvariable=self._new_string_var("enhancement.profile_label"),
            state="readonly",
        )
        active_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self._widgets["enhancement.profile"] = active_combo
        ttk.Label(
            tab,
            text="下部の「設定を保存」で反映",
            foreground="#666666",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(
            tab,
            text="profiles/*.toml の編集対象",
        ).grid(row=1, column=0, sticky="w", pady=4)
        editor_combo = ttk.Combobox(
            tab,
            textvariable=self._new_string_var("profile.editor"),
            state="readonly",
        )
        editor_combo.grid(row=1, column=1, sticky="ew", pady=4)
        editor_combo.bind("<<ComboboxSelected>>", self._load_selected_profile)
        self._widgets["profile.editor"] = editor_combo
        ttk.Button(
            tab,
            text="新規",
            command=self._new_profile,
        ).grid(row=1, column=2, padx=(8, 0))

        ttk.Label(tab, text="ID").grid(row=2, column=0, sticky="w", pady=4)
        profile_id_entry = ttk.Entry(
            tab,
            textvariable=self._new_string_var("profile.id"),
        )
        profile_id_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self._widgets["profile.id"] = profile_id_entry
        ttk.Label(
            tab,
            text="英数字・_・-",
            foreground="#666666",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))

        ttk.Label(tab, text="表示名").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(
            tab,
            textvariable=self._new_string_var("profile.name"),
        ).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(tab, text="文脈").grid(row=4, column=0, sticky="nw", pady=4)
        self._profile_context = tk.Text(tab, height=5, wrap="word")
        self._profile_context.grid(
            row=4,
            column=1,
            columnspan=2,
            sticky="nsew",
            pady=4,
        )

        terms_header = ttk.Frame(tab)
        terms_header.grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(10, 4),
        )
        ttk.Label(
            terms_header,
            text="語彙・誤認識候補",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        ttk.Button(
            terms_header,
            text="追加",
            command=self._add_term,
        ).pack(side="right")
        ttk.Button(
            terms_header,
            text="編集",
            command=self._edit_term,
        ).pack(side="right", padx=(0, 6))
        ttk.Button(
            terms_header,
            text="行を外す",
            command=self._remove_term,
        ).pack(side="right", padx=(0, 6))

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(
            row=6,
            column=0,
            columnspan=3,
            sticky="nsew",
        )
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self._profile_tree = ttk.Treeview(
            tree_frame,
            columns=("canonical", "spoken", "replace", "description"),
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in (
            ("canonical", "正規表記", 150),
            ("spoken", "読み・呼び方", 190),
            ("replace", "よくある誤認識", 230),
            ("description", "説明", 240),
        ):
            self._profile_tree.heading(column, text=heading)
            self._profile_tree.column(column, width=width, minwidth=80)
        self._profile_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self._profile_tree.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._profile_tree.configure(yscrollcommand=scrollbar.set)
        self._profile_tree.bind("<Double-1>", self._edit_term)

        ttk.Button(
            tab,
            text="プロフィール定義を保存",
            command=self._save_profile,
        ).grid(row=7, column=2, sticky="e", pady=(10, 0))

    def _build_postprocessor_tab(self, notebook: ttk.Notebook) -> None:
        tab = self._add_scrollable_tab(
            notebook,
            "後処理CLI",
            padding=10,
        )
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(11, weight=1)

        ttk.Label(
            tab,
            text="config.toml の使用中",
        ).grid(row=0, column=0, sticky="w", pady=4)
        active_combo = ttk.Combobox(
            tab,
            textvariable=self._new_string_var(
                "enhancement.postprocessor_label"
            ),
            state="readonly",
        )
        active_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self._widgets["enhancement.postprocessor"] = active_combo
        ttk.Label(
            tab,
            text="下部の「設定を保存」で反映",
            foreground="#666666",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(
            tab,
            text="CLI定義ファイルの編集対象",
        ).grid(row=1, column=0, sticky="w", pady=4)
        editor_combo = ttk.Combobox(
            tab,
            textvariable=self._new_string_var("postprocessor.editor"),
            state="readonly",
        )
        editor_combo.grid(row=1, column=1, sticky="ew", pady=4)
        editor_combo.bind(
            "<<ComboboxSelected>>",
            self._load_selected_postprocessor,
        )
        self._widgets["postprocessor.editor"] = editor_combo
        ttk.Button(
            tab,
            text="新規",
            command=self._new_postprocessor,
        ).grid(row=1, column=2, padx=(8, 0))

        ttk.Label(tab, text="ID").grid(row=2, column=0, sticky="w", pady=4)
        postprocessor_id_entry = ttk.Entry(
            tab,
            textvariable=self._new_string_var("postprocessor.id"),
        )
        postprocessor_id_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self._widgets["postprocessor.id"] = postprocessor_id_entry
        ttk.Label(
            tab,
            text="組み込みIDで保存するとローカル上書き",
            foreground="#666666",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))

        ttk.Label(tab, text="表示名").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(
            tab,
            textvariable=self._new_string_var("postprocessor.name"),
        ).grid(row=3, column=1, sticky="ew", pady=4)

        options = ttk.Frame(tab)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=4)
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)
        ttk.Label(options, text="送信先").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self._new_string_var("postprocessor.destination"),
            values=(
                DATA_DESTINATION_UNKNOWN,
                DATA_DESTINATION_LOCAL,
                DATA_DESTINATION_REMOTE,
            ),
            state="readonly",
            width=18,
        ).grid(row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(options, text="入力方式").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self._new_string_var("postprocessor.input_mode"),
            values=("stdin", "argument"),
            state="readonly",
            width=18,
        ).grid(row=0, column=3, sticky="w", padx=(8, 20))
        ttk.Label(options, text="Timeout (秒)").grid(
            row=0,
            column=4,
            sticky="w",
        )
        ttk.Entry(
            options,
            textvariable=self._new_string_var("postprocessor.timeout"),
            width=8,
        ).grid(row=0, column=5, sticky="w", padx=(8, 0))
        ttk.Label(options, text="Adapter").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(
            options,
            textvariable=self._new_string_var("postprocessor.adapter"),
            values=(ADAPTER_GENERIC, ADAPTER_KIRO),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(8, 20), pady=(6, 0))
        ttk.Label(options, text="専用モデル").grid(
            row=1,
            column=2,
            sticky="w",
            pady=(6, 0),
        )
        ttk.Entry(
            options,
            textvariable=self._new_string_var("postprocessor.model"),
        ).grid(
            row=1,
            column=3,
            columnspan=3,
            sticky="ew",
            padx=(8, 0),
            pady=(6, 0),
        )

        ttk.Label(
            tab,
            text="コマンド（引数を含む）",
        ).grid(row=5, column=0, sticky="nw", pady=4)
        command_frame = ttk.Frame(tab)
        command_frame.grid(
            row=5,
            column=1,
            columnspan=2,
            sticky="nsew",
            pady=4,
        )
        self._command_text = tk.Text(command_frame, height=2, wrap="word")
        self._command_text.pack(fill="both", expand=True)
        ttk.Label(
            command_frame,
            text=(
                "genericではモデル指定など任意のCLI引数をここへ入力します。"
                "Kiroでは専用モデル欄と {{agent}} を使用します。"
            ),
            foreground="#555555",
        ).pack(anchor="w", pady=(3, 0))

        ttk.Label(tab, text="事前確認").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(
            tab,
            textvariable=self._new_string_var("postprocessor.preflight"),
        ).grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(tab, text="事前確認失敗文").grid(
            row=7,
            column=0,
            sticky="w",
            pady=4,
        )
        ttk.Entry(
            tab,
            textvariable=self._new_string_var(
                "postprocessor.preflight_message"
            ),
        ).grid(row=7, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(tab, text="環境変数").grid(row=8, column=0, sticky="nw", pady=4)
        self._environment_text = tk.Text(tab, height=2, wrap="none")
        self._environment_text.grid(
            row=8,
            column=1,
            columnspan=2,
            sticky="nsew",
            pady=4,
        )

        ttk.Label(tab, text="System prompt").grid(
            row=9,
            column=0,
            sticky="nw",
            pady=4,
        )
        self._system_prompt_text = tk.Text(tab, height=6, wrap="word")
        self._system_prompt_text.grid(
            row=9,
            column=1,
            columnspan=2,
            sticky="nsew",
            pady=4,
        )
        ttk.Label(
            tab,
            text=(
                "CLI組み込みsystem promptの置換内容。genericではコマンドに"
                " {{system_prompt_file}} が必要です。Kiroでは実行ごとの専用"
                "エージェントへ設定します。テンプレート変数は使えません。"
            ),
            foreground="#555555",
            wraplength=680,
        ).grid(
            row=10,
            column=1,
            columnspan=2,
            sticky="w",
            pady=(0, 4),
        )

        ttk.Label(tab, text="プロンプト").grid(
            row=11,
            column=0,
            sticky="nw",
            pady=4,
        )
        self._prompt_text = tk.Text(tab, height=6, wrap="word")
        self._prompt_text.grid(
            row=11,
            column=1,
            columnspan=2,
            sticky="nsew",
            pady=4,
        )

        placeholder_frame = ttk.LabelFrame(
            tab,
            text=(
                "使えるプレースホルダー"
                "（テンプレート変数／クリックで挿入）"
            ),
            padding=8,
        )
        placeholder_frame.grid(
            row=12,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(8, 0),
        )
        placeholder_frame.columnconfigure(1, weight=1)
        placeholder_frame.columnconfigure(3, weight=1)
        rows_per_column = math.ceil(
            len(POSTPROCESSOR_PLACEHOLDER_HELP) / 2
        )
        for index, (name, target, description) in enumerate(
            POSTPROCESSOR_PLACEHOLDER_HELP
        ):
            column_group, row = divmod(index, rows_per_column)
            button_column = column_group * 2
            placeholder = f"{{{{{name}}}}}"
            target_label = (
                "プロンプトへ挿入"
                if target == "prompt"
                else "コマンドへ挿入"
            )
            ttk.Button(
                placeholder_frame,
                text=placeholder,
                command=lambda value=name, destination=target: (
                    self._insert_postprocessor_placeholder(
                        value,
                        destination,
                    )
                ),
                width=18,
            ).grid(
                row=row,
                column=button_column,
                sticky="w",
                padx=(0 if button_column == 0 else 14, 0),
                pady=2,
            )
            ttk.Label(
                placeholder_frame,
                text=f"{description} — {target_label}",
                wraplength=250,
            ).grid(
                row=row,
                column=button_column + 1,
                sticky="w",
                padx=(10, 0),
                pady=2,
            )
        ttk.Label(
            placeholder_frame,
            text=(
                "stdin方式: コマンドは固定し、上の値はプロンプトで使います。"
                "argument方式: コマンドに {{prompt}} が必須で、"
                "完成したプロンプトがプロセス引数に現れます。"
            ),
            foreground="#8A4B08",
        ).grid(
            row=rows_per_column,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(7, 0),
        )

        footer = ttk.Frame(tab)
        footer.grid(
            row=13,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(8, 0),
        )
        ttk.Label(
            footer,
            text=(
                "remote/unknown は認識結果・文脈・辞書を外部CLIへ渡します。"
                "stdinではコマンドを固定し、{{transcript}}等はプロンプト側に置きます。"
            ),
            foreground="#8A4B08",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            footer,
            text="CLI定義を保存",
            command=self._save_postprocessor,
        ).pack(side="right")

    def _insert_postprocessor_placeholder(
        self,
        name: str,
        destination: str,
    ) -> None:
        if name not in SUPPORTED_TEMPLATE_PLACEHOLDERS:
            raise ValueError(f"未対応のプレースホルダーです: {name}")
        widget = {
            "prompt": self._prompt_text,
            "command": self._command_text,
        }.get(destination)
        if widget is None:
            return
        widget.insert("insert", f"{{{{{name}}}}}")
        widget.see("insert")
        widget.focus_set()

    def _config_form_value(self, key: str) -> object:
        variable = self._vars.get(key)
        if variable is None:
            return None
        value = variable.get()
        if isinstance(variable, tk.BooleanVar):
            return bool(value)

        text = str(value)
        if key in ("hotkey.toggle", "hotkey.submit_toggle"):
            if key == "hotkey.submit_toggle" and (
                not text.strip() or text.strip() == DISPLAY_DISABLED
            ):
                return ""
            return parse_hotkey_value(text)
        if key == "recording.microphone":
            return (
                ""
                if text.strip() in ("", DISPLAY_DEFAULT_MICROPHONE)
                else text.strip()
            )
        if key == "recognition.hallucination_silence_threshold":
            return (
                None
                if text.strip() in ("", DISPLAY_DISABLED)
                else text.strip()
            )
        if key == "enhancement.profile_label":
            return self._profile_id_by_label.get(
                text,
                f"__unknown_profile_label__:{text}",
            )
        if key == "enhancement.postprocessor_label":
            return self._postprocessor_id_by_label.get(
                text,
                f"__unknown_postprocessor_label__:{text}",
            )
        return text

    def _capture_config_form_state(self) -> dict[str, object]:
        return {
            key: self._config_form_value(key)
            for key in CONFIG_FORM_FIELDS
            if key in self._vars
        }

    def _config_changed_keys(self) -> tuple[str, ...]:
        baseline = self._config_form_baseline
        if baseline is None:
            return ()
        return changed_config_fields(
            baseline,
            self._capture_config_form_state(),
        )

    def _capture_profile_editor_state(self) -> tuple[object, ...] | None:
        required_keys = ("profile.id", "profile.name")
        if not all(key in self._vars for key in required_keys):
            return None
        context = (
            self._profile_context.get("1.0", "end").strip()
            if self._profile_context is not None
            else ""
        )
        return (
            str(self._vars["profile.id"].get()).strip(),
            str(self._vars["profile.name"].get()).strip(),
            context,
            tuple(self._profile_terms),
        )

    def _profile_editor_changed(self) -> bool:
        current = self._capture_profile_editor_state()
        return (
            self._profile_editor_baseline is not None
            and current is not None
            and current != self._profile_editor_baseline
        )

    def _capture_postprocessor_editor_state(
        self,
    ) -> tuple[object, ...] | None:
        required_keys = (
            "postprocessor.id",
            "postprocessor.name",
            "postprocessor.destination",
            "postprocessor.input_mode",
            "postprocessor.timeout",
            "postprocessor.preflight",
            "postprocessor.preflight_message",
        )
        if not all(key in self._vars for key in required_keys):
            return None

        def text_value(widget: tk.Text | None) -> str:
            return widget.get("1.0", "end").strip() if widget is not None else ""

        def variable_value(key: str, default: str) -> str:
            variable = self._vars.get(key)
            return str(variable.get()) if variable is not None else default

        return (
            str(self._vars["postprocessor.id"].get()).strip(),
            str(self._vars["postprocessor.name"].get()).strip(),
            str(self._vars["postprocessor.destination"].get()),
            str(self._vars["postprocessor.input_mode"].get()),
            variable_value("postprocessor.adapter", ADAPTER_GENERIC),
            variable_value("postprocessor.model", "").strip(),
            str(self._vars["postprocessor.timeout"].get()).strip(),
            str(self._vars["postprocessor.preflight"].get()).strip(),
            str(
                self._vars["postprocessor.preflight_message"].get()
            ).strip(),
            text_value(self._command_text),
            text_value(self._environment_text),
            text_value(self._system_prompt_text),
            text_value(self._prompt_text),
        )

    def _postprocessor_editor_changed(self) -> bool:
        current = self._capture_postprocessor_editor_state()
        return (
            self._postprocessor_editor_baseline is not None
            and current is not None
            and current != self._postprocessor_editor_baseline
        )

    def _confirm_discard_profile_changes(self, action: str) -> bool:
        if not self._profile_editor_changed():
            return True
        return messagebox.askyesno(
            "未保存のプロフィール変更を破棄しますか？",
            (
                "プロフィール定義に未保存の変更があります。\n"
                f"変更を破棄して{action}しますか？"
            ),
            icon="warning",
            parent=self._root,
        )

    def _confirm_discard_postprocessor_changes(self, action: str) -> bool:
        if not self._postprocessor_editor_changed():
            return True
        return messagebox.askyesno(
            "未保存のCLI変更を破棄しますか？",
            (
                "CLIプリセット定義に未保存の変更があります。\n"
                f"変更を破棄して{action}しますか？"
            ),
            icon="warning",
            parent=self._root,
        )

    def _on_config_form_changed(self, key: str) -> None:
        if key in RECOGNITION_SELECTION_KEYS:
            self._update_recognition_summary()
        if key not in CONFIG_FORM_FIELDS or self._config_form_loading:
            return
        self._update_config_change_state()

    def _update_recognition_summary(self) -> None:
        summary = self._recognition_summary_var
        if summary is None:
            return
        required_keys = (
            "recognition.engine",
            "recognition.language",
            "recognition.device",
        )
        if not all(key in self._vars for key in required_keys):
            return
        engine = str(self._vars["recognition.engine"].get())
        language = str(self._vars["recognition.language"].get())
        device = str(self._vars["recognition.device"].get())
        availability_note = ""
        if engine not in available_recognition_engines():
            availability_note = "（現在値のエンジンは未導入）"
        elif device not in available_recognition_devices(engine):
            availability_note = "（現在値の実行先は未導入）"
        selection = recognition_selection_text(engine, language, device)
        summary.set(
            f"{selection} {availability_note}".rstrip()
        )

    def _update_config_change_state(self) -> None:
        baseline = self._config_form_baseline
        if baseline is None:
            if self._config_changes_var is not None:
                self._config_changes_var.set("設定値の読み込み待ち")
            if self._save_config_button is not None:
                self._save_config_button.configure(state="disabled")
            return

        changed_keys = self._config_changed_keys()
        if self._save_config_button is not None:
            self._save_config_button.configure(
                state="normal" if changed_keys else "disabled"
            )
        if self._config_changes_var is None:
            return
        if not changed_keys:
            self._config_changes_var.set("未保存の設定変更はありません")
            return

        labels = [
            CONFIG_FORM_FIELDS[key][2]
            for key in changed_keys[:4]
        ]
        suffix = (
            f"、ほか{len(changed_keys) - len(labels)}件"
            if len(changed_keys) > len(labels)
            else ""
        )
        self._config_changes_var.set(
            f"未保存の設定変更: {len(changed_keys)}件"
            f"（{'、'.join(labels)}{suffix}）"
        )

    def _discard_config_changes(self) -> None:
        changed_keys = self._config_changed_keys()
        if changed_keys and not messagebox.askyesno(
            "変更を破棄しますか？",
            (
                f"未保存の設定変更が{len(changed_keys)}件あります。\n"
                "破棄して、現在のconfig.tomlを読み直しますか？"
            ),
            icon="warning",
            parent=self._root,
        ):
            self._set_status("現在値への再読込をキャンセルしました")
            return
        self._refresh_config_snapshot("現在の設定値へ戻しました")

    @staticmethod
    def _config_field_value(cfg: AppConfig, key: str) -> object:
        section_name, field_name, _label = CONFIG_FORM_FIELDS[key]
        return getattr(getattr(cfg, section_name), field_name)

    def _display_config_value(self, key: str, value: object) -> str:
        if isinstance(value, bool):
            return "有効" if value else "無効"
        if key == "hotkey.submit_toggle" and not value:
            return DISPLAY_DISABLED
        if key == "recording.microphone" and not value:
            return DISPLAY_DEFAULT_MICROPHONE
        if (
            key == "recognition.hallucination_silence_threshold"
            and value is None
        ):
            return DISPLAY_DISABLED
        if key == "enhancement.profile_label":
            return self._profile_label_by_id.get(
                str(value),
                str(value) or "オフ",
            )
        if key == "enhancement.postprocessor_label":
            return self._postprocessor_label_by_id.get(
                str(value),
                str(value),
            )
        if isinstance(value, list):
            return hotkey_value_to_text(value)
        if value == "":
            return "（空）"
        return str(value)

    def _confirm_config_changes(
        self,
        cfg: AppConfig,
        changed_keys: tuple[str, ...],
    ) -> bool:
        snapshot = self._snapshot
        if snapshot is None:
            return False
        lines: list[str] = []
        for key in changed_keys[:16]:
            _section, _field, label = CONFIG_FORM_FIELDS[key]
            before = self._display_config_value(
                key,
                self._config_field_value(snapshot.config, key),
            )
            after = self._display_config_value(
                key,
                self._config_field_value(cfg, key),
            )
            lines.append(f"・{label}: {before} → {after}")
        if len(changed_keys) > len(lines):
            lines.append(f"・ほか{len(changed_keys) - len(lines)}件")
        return messagebox.askyesno(
            "config.tomlへ保存しますか？",
            (
                "次の設定変更をconfig.tomlへ保存します。\n\n"
                + "\n".join(lines)
                + "\n\nプロフィールとCLIの定義内容は、"
                "それぞれの専用保存ボタンで保存します。"
            ),
            parent=self._root,
        )

    def _refresh_snapshot(
        self,
        status_message: str = "現在の設定を読み込みました",
    ) -> None:
        self._config_form_loading = True
        self._config_form_baseline = None
        if self._save_config_button is not None:
            self._save_config_button.configure(state="disabled")
        if self._config_source_var is not None:
            self._config_source_var.set(
                "config.toml の現在値を読み込んでいます..."
            )
        snapshot = self._request_snapshot()
        if snapshot is None:
            self._config_form_loading = False
            if self._config_source_var is not None:
                self._config_source_var.set(
                    "config.toml を読み込めないため保存できません"
                )
            self._update_config_change_state()
            return
        self._snapshot = snapshot
        self._load_config_variables(snapshot.config)
        self._refresh_profile_choices(snapshot)
        self._refresh_postprocessor_choices(snapshot)
        self._config_form_baseline = self._capture_config_form_state()
        self._config_form_loading = False
        if self._config_source_var is not None:
            self._config_source_var.set(
                "config.toml と既定値から読み込んだ現在の有効値を表示中"
            )
        self._update_config_change_state()
        hotkey_errors = validate_hotkey_config(snapshot.config.hotkey)
        if hotkey_errors:
            status_message = (
                "ホットキー設定を修正してください: "
                + hotkey_errors[0]
            )
        self._set_status(status_message)

    def _refresh_config_snapshot(
        self,
        status_message: str = "現在の設定値を読み込みました",
    ) -> None:
        """Reload config while preserving unsaved resource editor contents."""
        self._config_form_loading = True
        self._config_form_baseline = None
        if self._save_config_button is not None:
            self._save_config_button.configure(state="disabled")
        if self._config_source_var is not None:
            self._config_source_var.set(
                "config.toml の現在値を読み込んでいます..."
            )
        refreshed = self._request_snapshot()
        if refreshed is None:
            self._config_form_loading = False
            if self._config_source_var is not None:
                self._config_source_var.set(
                    "config.toml を読み込めないため保存できません"
                )
            self._update_config_change_state()
            return
        snapshot = (
            refresh_snapshot_config(self._snapshot, refreshed)
            if self._snapshot is not None
            else refreshed
        )
        self._snapshot = snapshot
        self._load_config_variables(snapshot.config)
        self._refresh_profile_choices(snapshot, reload_editor=False)
        self._refresh_postprocessor_choices(snapshot, reload_editor=False)
        self._config_form_baseline = self._capture_config_form_state()
        self._config_form_loading = False
        if self._config_source_var is not None:
            self._config_source_var.set(
                "config.toml と既定値から読み込んだ現在の有効値を表示中"
            )
        self._update_config_change_state()
        hotkey_errors = validate_hotkey_config(snapshot.config.hotkey)
        if hotkey_errors:
            status_message = (
                "ホットキー設定を修正してください: "
                + hotkey_errors[0]
            )
        self._set_status(status_message)

    def _request_snapshot(self) -> SettingsSnapshot | None:
        try:
            return self._snapshot_provider()
        except Exception:
            logger.exception("設定スナップショットを取得できませんでした")
            messagebox.showerror(
                "ZenWhisper 設定",
                "現在の設定を読み込めませんでした。ログを確認してください。",
                parent=self._root,
            )
            return None

    def _load_config_variables(self, cfg: AppConfig) -> None:
        values: dict[str, object] = {
            "hotkey.toggle": hotkey_value_to_text(cfg.hotkey.toggle),
            "hotkey.submit_toggle": (
                hotkey_value_to_text(cfg.hotkey.submit_toggle)
                if cfg.hotkey.submit_toggle
                else DISPLAY_DISABLED
            ),
            "hotkey.switch_lang": hotkey_value_to_text(
                cfg.hotkey.switch_lang
            ),
            "output.restore_clipboard": cfg.output.restore_clipboard,
            "output.paste_delay_ms": str(cfg.output.paste_delay_ms),
            "feedback.sound_enabled": cfg.feedback.sound_enabled,
            "feedback.sound_type": cfg.feedback.sound_type,
            "feedback.volume": str(cfg.feedback.volume),
            "feedback.custom_start_sound": (
                cfg.feedback.custom_start_sound
            ),
            "feedback.custom_stop_sound": cfg.feedback.custom_stop_sound,
            "overlay.enabled": cfg.overlay.enabled,
            "overlay.position": cfg.overlay.position,
            "overlay.size": str(cfg.overlay.size),
            "logging.level": cfg.logging.level,
            "logging.file": cfg.logging.file,
            "recognition.engine": cfg.recognition.engine,
            "recognition.language": cfg.recognition.language,
            "recognition.device": cfg.recognition.device,
            "recognition.model_size": cfg.recognition.model_size,
            "recognition.compute_type": cfg.recognition.compute_type,
            "recognition.beam_size": str(cfg.recognition.beam_size),
            "recognition.cpu_threads": str(cfg.recognition.cpu_threads),
            "recognition.model_load_timeout_sec": str(
                cfg.recognition.model_load_timeout_sec
            ),
            "recognition.reazon_language": cfg.recognition.reazon_language,
            "recognition.reazon_precision": cfg.recognition.reazon_precision,
            "recognition.reazon_chunk_sec": str(
                cfg.recognition.reazon_chunk_sec
            ),
            "recognition.reazon_trailing_silence_sec": str(
                cfg.recognition.reazon_trailing_silence_sec
            ),
            "recognition.qwen3_model": cfg.recognition.qwen3_model,
            "recognition.qwen3_max_new_tokens": str(
                cfg.recognition.qwen3_max_new_tokens
            ),
            "recognition.qwen3_attn_implementation": (
                cfg.recognition.qwen3_attn_implementation
            ),
            "recognition.qwen3_torch_compile": (
                cfg.recognition.qwen3_torch_compile
            ),
            "recognition.no_speech_threshold": str(
                cfg.recognition.no_speech_threshold
            ),
            "recognition.condition_on_previous_text": (
                cfg.recognition.condition_on_previous_text
            ),
            "recognition.hallucination_silence_threshold": (
                DISPLAY_DISABLED
                if cfg.recognition.hallucination_silence_threshold is None
                else str(
                    cfg.recognition.hallucination_silence_threshold
                )
            ),
            "recording.microphone": (
                cfg.recording.microphone
                if cfg.recording.microphone
                else DISPLAY_DEFAULT_MICROPHONE
            ),
            "recording.sample_rate": str(cfg.recording.sample_rate),
            "recording.vad_silence_threshold_sec": str(
                cfg.recording.vad_silence_threshold_sec
            ),
            "recording.min_recording_sec": str(
                cfg.recording.min_recording_sec
            ),
            "recording.min_audio_rms": str(cfg.recording.min_audio_rms),
            "recording.min_audio_peak": str(cfg.recording.min_audio_peak),
            "recording.max_recording_sec": str(
                cfg.recording.max_recording_sec
            ),
            "recording.max_recording_warning_pct": str(
                cfg.recording.max_recording_warning_pct
            ),
        }
        for key, value in values.items():
            variable = self._vars.get(key)
            if variable is not None:
                variable.set(value)
        self._configure_recognition_choices(preserve_current=True)
        self._update_recognition_summary()

    def _on_recognition_engine_changed(
        self,
        event: object | None = None,
    ) -> None:
        self._configure_recognition_choices(preserve_current=False)

    def _configure_recognition_choices(
        self,
        *,
        preserve_current: bool,
    ) -> None:
        engine = str(self._vars["recognition.engine"].get())
        engine_values = list(available_recognition_engines())
        engine_widget = self._widgets.get("recognition.engine")
        if isinstance(engine_widget, ttk.Combobox):
            engine_widget.configure(values=engine_values)

        device = str(self._vars["recognition.device"].get())
        device_values = (
            list(available_recognition_devices(engine))
            if engine in engine_values
            else []
        )
        device_widget = self._widgets.get("recognition.device")
        if isinstance(device_widget, ttk.Combobox):
            device_widget.configure(values=device_values)
        if not preserve_current and device not in device_values:
            self._vars["recognition.device"].set(
                device_values[0] if device_values else ""
            )

    @staticmethod
    def _destination_label(preset: PostprocessorPreset) -> str:
        labels = {
            DATA_DESTINATION_LOCAL: "ローカル",
            DATA_DESTINATION_REMOTE: "外部送信",
            DATA_DESTINATION_UNKNOWN: "送信先不明",
        }
        return labels[preset.data_destination]

    def _refresh_profile_choices(
        self,
        snapshot: SettingsSnapshot,
        *,
        reload_editor: bool = True,
    ) -> None:
        self._profile_id_by_label = {"オフ": ""}
        self._profile_label_by_id = {"": "オフ"}
        for profile_id, profile in snapshot.profiles.items():
            label = f"{profile_id} — {profile.name}"
            self._profile_id_by_label[label] = profile_id
            self._profile_label_by_id[profile_id] = label

        active_profile_id = snapshot.config.enhancement.profile
        if active_profile_id not in self._profile_label_by_id:
            missing_label = (
                f"{active_profile_id} — （定義ファイルが見つかりません）"
            )
            self._profile_id_by_label[missing_label] = active_profile_id
            self._profile_label_by_id[active_profile_id] = missing_label
        labels = list(self._profile_id_by_label)
        active_widget = self._widgets.get("enhancement.profile")
        if isinstance(active_widget, ttk.Combobox):
            active_widget.configure(values=labels)
        active_label = self._profile_label_by_id[active_profile_id]
        self._vars["enhancement.profile_label"].set(active_label)

        editor_widget = self._widgets.get("profile.editor")
        profile_ids = list(snapshot.profiles)
        if isinstance(editor_widget, ttk.Combobox):
            editor_widget.configure(values=profile_ids)
        if not reload_editor:
            return
        selected_id = snapshot.config.enhancement.profile
        if selected_id not in snapshot.profiles:
            selected_id = profile_ids[0] if profile_ids else ""
        self._vars["profile.editor"].set(selected_id)
        self._load_profile(selected_id)

    def _refresh_postprocessor_choices(
        self,
        snapshot: SettingsSnapshot,
        *,
        reload_editor: bool = True,
    ) -> None:
        self._postprocessor_id_by_label = {
            "オフ": POSTPROCESSOR_OFF,
            "辞書置換のみ": POSTPROCESSOR_DICTIONARY,
        }
        self._postprocessor_label_by_id = {
            POSTPROCESSOR_OFF: "オフ",
            POSTPROCESSOR_DICTIONARY: "辞書置換のみ",
        }
        for preset_id, preset in snapshot.postprocessors.items():
            local_suffix = (
                " / ローカル設定"
                if preset_id in snapshot.local_postprocessor_ids
                else " / 組み込み"
            )
            label = (
                f"{preset_id} — {preset.display_name} "
                f"（{self._destination_label(preset)}{local_suffix}）"
            )
            self._postprocessor_id_by_label[label] = preset_id
            self._postprocessor_label_by_id[preset_id] = label

        active_postprocessor_id = (
            snapshot.config.enhancement.postprocessor
        )
        if (
            active_postprocessor_id
            not in self._postprocessor_label_by_id
        ):
            missing_label = (
                f"{active_postprocessor_id} — "
                "（CLI定義が見つかりません）"
            )
            self._postprocessor_id_by_label[
                missing_label
            ] = active_postprocessor_id
            self._postprocessor_label_by_id[
                active_postprocessor_id
            ] = missing_label
        labels = list(self._postprocessor_id_by_label)
        active_widget = self._widgets.get("enhancement.postprocessor")
        if isinstance(active_widget, ttk.Combobox):
            active_widget.configure(values=labels)
        active_label = self._postprocessor_label_by_id[
            active_postprocessor_id
        ]
        self._vars["enhancement.postprocessor_label"].set(active_label)

        editor_widget = self._widgets.get("postprocessor.editor")
        preset_ids = list(snapshot.postprocessors)
        if isinstance(editor_widget, ttk.Combobox):
            editor_widget.configure(values=preset_ids)
        if not reload_editor:
            return
        selected_id = snapshot.config.enhancement.postprocessor
        if selected_id not in snapshot.postprocessors:
            selected_id = preset_ids[0] if preset_ids else ""
        self._vars["postprocessor.editor"].set(selected_id)
        self._load_postprocessor(selected_id)

    def _config_from_form(
        self,
        changed_keys: tuple[str, ...] | None = None,
    ) -> AppConfig:
        if self._snapshot is None:
            raise ValueError("設定が読み込まれていません")
        if self._config_form_baseline is None:
            raise ValueError("設定値の読み込みが完了していません")
        if changed_keys is None:
            changed_keys = self._config_changed_keys()
        cfg = copy.deepcopy(self._snapshot.config)
        cfg.hotkey.toggle = parse_hotkey_value(
            str(self._vars["hotkey.toggle"].get())
        )
        submit_hotkey_value = str(
            self._vars["hotkey.submit_toggle"].get()
        ).strip()
        if submit_hotkey_value == DISPLAY_DISABLED:
            submit_hotkey_value = ""
        cfg.hotkey.submit_toggle = parse_hotkey_value(
            submit_hotkey_value
        )
        cfg.hotkey.switch_lang = str(
            self._vars["hotkey.switch_lang"].get()
        ).strip()
        cfg.output.restore_clipboard = bool(
            self._vars["output.restore_clipboard"].get()
        )
        cfg.output.paste_delay_ms = self._int_value(
            "output.paste_delay_ms",
            "貼り付け待機",
        )
        cfg.feedback.sound_enabled = bool(
            self._vars["feedback.sound_enabled"].get()
        )
        cfg.feedback.sound_type = str(
            self._vars["feedback.sound_type"].get()
        )
        cfg.feedback.volume = self._float_value(
            "feedback.volume",
            "音量",
        )
        cfg.feedback.custom_start_sound = str(
            self._vars["feedback.custom_start_sound"].get()
        ).strip()
        cfg.feedback.custom_stop_sound = str(
            self._vars["feedback.custom_stop_sound"].get()
        ).strip()
        cfg.overlay.enabled = bool(self._vars["overlay.enabled"].get())
        cfg.logging.level = str(self._vars["logging.level"].get())
        cfg.logging.file = str(self._vars["logging.file"].get()).strip()

        cfg.recognition.engine = str(
            self._vars["recognition.engine"].get()
        )
        cfg.recognition.language = str(
            self._vars["recognition.language"].get()
        )
        cfg.recognition.device = str(
            self._vars["recognition.device"].get()
        )
        cfg.recognition.model_size = str(
            self._vars["recognition.model_size"].get()
        ).strip()
        cfg.recognition.compute_type = str(
            self._vars["recognition.compute_type"].get()
        ).strip()
        cfg.recognition.beam_size = self._int_value(
            "recognition.beam_size",
            "Beam size",
        )
        cfg.recognition.cpu_threads = self._int_value(
            "recognition.cpu_threads",
            "CPU threads",
        )
        cfg.recognition.model_load_timeout_sec = self._int_value(
            "recognition.model_load_timeout_sec",
            "モデル読込タイムアウト",
        )
        cfg.recognition.reazon_language = str(
            self._vars["recognition.reazon_language"].get()
        )
        cfg.recognition.reazon_precision = str(
            self._vars["recognition.reazon_precision"].get()
        )
        cfg.recognition.reazon_chunk_sec = self._float_value(
            "recognition.reazon_chunk_sec",
            "Reazonチャンク長",
        )
        cfg.recognition.reazon_trailing_silence_sec = self._float_value(
            "recognition.reazon_trailing_silence_sec",
            "Reazon末尾無音",
        )
        cfg.recognition.qwen3_model = str(
            self._vars["recognition.qwen3_model"].get()
        ).strip()
        cfg.recognition.qwen3_max_new_tokens = self._int_value(
            "recognition.qwen3_max_new_tokens",
            "Qwen最大生成トークン",
        )
        cfg.recognition.qwen3_attn_implementation = str(
            self._vars["recognition.qwen3_attn_implementation"].get()
        )
        cfg.recognition.qwen3_torch_compile = bool(
            self._vars["recognition.qwen3_torch_compile"].get()
        )
        cfg.recognition.no_speech_threshold = self._float_value(
            "recognition.no_speech_threshold",
            "無音判定しきい値",
        )
        cfg.recognition.condition_on_previous_text = bool(
            self._vars[
                "recognition.condition_on_previous_text"
            ].get()
        )
        hallucination_value = str(
            self._vars[
                "recognition.hallucination_silence_threshold"
            ].get()
        ).strip()
        cfg.recognition.hallucination_silence_threshold = (
            None
            if hallucination_value in ("", DISPLAY_DISABLED)
            else self._parse_float(
                hallucination_value,
                "幻覚抑制の無音長",
            )
        )

        microphone = str(
            self._vars["recording.microphone"].get()
        ).strip()
        cfg.recording.microphone = (
            "" if microphone == DISPLAY_DEFAULT_MICROPHONE else microphone
        )
        cfg.recording.vad_silence_threshold_sec = self._float_value(
            "recording.vad_silence_threshold_sec",
            "無音停止",
        )
        cfg.recording.min_recording_sec = self._float_value(
            "recording.min_recording_sec",
            "最短録音",
        )
        cfg.recording.min_audio_rms = self._float_value(
            "recording.min_audio_rms",
            "最小RMS",
        )
        cfg.recording.min_audio_peak = self._float_value(
            "recording.min_audio_peak",
            "最小Peak",
        )
        cfg.recording.max_recording_sec = self._float_value(
            "recording.max_recording_sec",
            "最大録音",
        )
        cfg.recording.max_recording_warning_pct = self._int_value(
            "recording.max_recording_warning_pct",
            "最大時間の警告",
        )

        profile_label = str(
            self._vars["enhancement.profile_label"].get()
        )
        postprocessor_label = str(
            self._vars["enhancement.postprocessor_label"].get()
        )
        if profile_label not in self._profile_id_by_label:
            raise ValueError("選択したプロフィールを特定できません")
        if postprocessor_label not in self._postprocessor_id_by_label:
            raise ValueError("選択したCLI後処理を特定できません")
        cfg.enhancement.profile = self._profile_id_by_label[profile_label]
        cfg.enhancement.postprocessor = (
            self._postprocessor_id_by_label[postprocessor_label]
        )

        cfg = apply_changed_config_fields(
            self._snapshot.config,
            cfg,
            changed_keys,
        )
        warnings = cfg.validate() + validate_hotkey_config(cfg.hotkey)
        if warnings:
            raise ValueError("\n".join(warnings))
        return cfg

    def _int_value(self, key: str, label: str) -> int:
        try:
            return int(str(self._vars[key].get()).strip())
        except ValueError as exc:
            raise ValueError(f"{label}は整数で指定してください") from exc

    def _float_value(self, key: str, label: str) -> float:
        return self._parse_float(str(self._vars[key].get()).strip(), label)

    @staticmethod
    def _parse_float(value: str, label: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{label}は数値で指定してください") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{label}は有限の数値で指定してください")
        return parsed

    def _save_config(self) -> None:
        if self._config_form_baseline is None:
            messagebox.showerror(
                "設定を保存できません",
                "config.toml の読み込みが完了していません",
                parent=self._root,
            )
            return
        changed_keys = self._config_changed_keys()
        if not changed_keys:
            self._set_status("保存する設定変更はありません")
            self._update_config_change_state()
            return
        try:
            cfg = self._config_from_form(changed_keys)
            if not self._confirm_config_changes(cfg, changed_keys):
                self._set_status("設定の保存をキャンセルしました")
                return
            if not self._confirm_external_postprocessor(cfg):
                self._set_status("設定の保存をキャンセルしました")
                return
            expected_revision = (
                self._snapshot.revision if self._snapshot is not None else -1
            )
            succeeded, message = self._on_save_config(
                cfg,
                expected_revision,
                self._snapshot.config_fingerprint,
            )
        except ValueError as exc:
            messagebox.showerror(
                "設定を保存できません",
                str(exc),
                parent=self._root,
            )
            return
        except Exception:
            logger.exception("設定保存コールバックで例外が発生しました")
            succeeded, message = False, "設定保存中にエラーが発生しました"
        if succeeded:
            self._refresh_config_snapshot(message)
            messagebox.showinfo(
                "ZenWhisper 設定",
                message,
                parent=self._root,
            )
        else:
            messagebox.showerror(
                "設定を保存できません",
                message,
                parent=self._root,
            )

    def _confirm_external_postprocessor(self, cfg: AppConfig) -> bool:
        snapshot = self._snapshot
        if (
            snapshot is None
            or cfg.enhancement.postprocessor
            == snapshot.config.enhancement.postprocessor
        ):
            return True
        preset = snapshot.postprocessors.get(
            cfg.enhancement.postprocessor
        )
        if (
            preset is None
            or preset.data_destination == DATA_DESTINATION_LOCAL
        ):
            return True
        destination = (
            "外部サービス"
            if preset.data_destination == DATA_DESTINATION_REMOTE
            else "送信先が確認できないCLI"
        )
        return messagebox.askyesno(
            "CLI後処理を有効にしますか？",
            (
                f"「{preset.display_name}」を有効にすると、認識結果、"
                "プロフィールの文脈、辞書データが"
                f"{destination}へ渡されます。\n\n"
                "この設定を保存しますか？"
            ),
            icon="warning",
            parent=self._root,
        )

    def _load_selected_profile(self, event: object | None = None) -> None:
        profile_id = str(self._vars["profile.editor"].get())
        if (
            profile_id != self._loaded_profile_id
            and not self._confirm_discard_profile_changes(
                "別のプロフィールへ切り替え"
            )
        ):
            self._vars["profile.editor"].set(self._loaded_profile_id)
            self._set_status("プロフィールの切替をキャンセルしました")
            return
        self._load_profile(profile_id)

    def _load_profile(self, profile_id: str) -> None:
        profile = (
            self._snapshot.profiles.get(profile_id)
            if self._snapshot is not None
            else None
        )
        self._vars["profile.id"].set(profile.profile_id if profile else "")
        self._vars["profile.name"].set(profile.name if profile else "")
        id_widget = self._widgets.get("profile.id")
        if isinstance(id_widget, ttk.Entry):
            id_widget.configure(state="disabled" if profile else "normal")
        if self._profile_context is not None:
            self._profile_context.delete("1.0", "end")
            if profile is not None:
                self._profile_context.insert("1.0", profile.context)
        self._profile_terms = list(profile.terms) if profile else []
        self._refresh_term_tree()
        self._loaded_profile_id = profile.profile_id if profile else ""
        self._profile_editor_baseline = self._capture_profile_editor_state()

    def _new_profile(self) -> None:
        if not self._confirm_discard_profile_changes(
            "新しいプロフィールを作成"
        ):
            self._set_status("新規プロフィールの作成をキャンセルしました")
            return
        self._vars["profile.editor"].set("")
        self._load_profile("")
        self._set_status("新しいプロフィールを入力してください")

    def _refresh_term_tree(self) -> None:
        tree = self._profile_tree
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        for index, term in enumerate(self._profile_terms):
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    term.canonical,
                    " / ".join(term.spoken),
                    " / ".join(term.replace_from),
                    term.description,
                ),
            )

    def _selected_term_index(self) -> int | None:
        tree = self._profile_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _add_term(self) -> None:
        term = self._term_dialog(None)
        if term is not None:
            self._profile_terms.append(term)
            self._refresh_term_tree()

    def _edit_term(self, event: object | None = None) -> None:
        index = self._selected_term_index()
        if index is None:
            return
        term = self._term_dialog(self._profile_terms[index])
        if term is not None:
            self._profile_terms[index] = term
            self._refresh_term_tree()

    def _remove_term(self) -> None:
        index = self._selected_term_index()
        if index is None:
            return
        del self._profile_terms[index]
        self._refresh_term_tree()

    def _term_dialog(self, term: ProfileTerm | None) -> ProfileTerm | None:
        if self._root is None:
            return None
        dialog = tk.Toplevel(self._root)
        dialog.title("語彙を編集")
        dialog.geometry("620x500")
        dialog.transient(self._root)
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)
        dialog.rowconfigure(2, weight=1)
        dialog.rowconfigure(3, weight=1)
        result: list[ProfileTerm] = []

        canonical = tk.StringVar(
            master=dialog,
            value=term.canonical if term else "",
        )
        description = tk.StringVar(
            master=dialog,
            value=term.description if term else "",
        )
        ttk.Label(dialog, text="正規表記").grid(
            row=0,
            column=0,
            sticky="w",
            padx=12,
            pady=(12, 4),
        )
        ttk.Entry(dialog, textvariable=canonical).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(0, 12),
            pady=(12, 4),
        )
        ttk.Label(dialog, text="説明").grid(
            row=1,
            column=0,
            sticky="w",
            padx=12,
            pady=4,
        )
        ttk.Entry(dialog, textvariable=description).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(0, 12),
            pady=4,
        )
        ttk.Label(dialog, text="読み・呼び方\n(1行1候補)").grid(
            row=2,
            column=0,
            sticky="nw",
            padx=12,
            pady=4,
        )
        spoken_text = tk.Text(dialog, height=7)
        spoken_text.grid(
            row=2,
            column=1,
            sticky="nsew",
            padx=(0, 12),
            pady=4,
        )
        ttk.Label(dialog, text="よくある誤認識\n(1行1候補)").grid(
            row=3,
            column=0,
            sticky="nw",
            padx=12,
            pady=4,
        )
        replace_text = tk.Text(dialog, height=7)
        replace_text.grid(
            row=3,
            column=1,
            sticky="nsew",
            padx=(0, 12),
            pady=4,
        )
        if term is not None:
            spoken_text.insert("1.0", "\n".join(term.spoken))
            replace_text.insert("1.0", "\n".join(term.replace_from))

        def save() -> None:
            canonical_value = canonical.get().strip()
            if not canonical_value:
                messagebox.showerror(
                    "語彙を保存できません",
                    "正規表記は空にできません",
                    parent=dialog,
                )
                return
            result.append(
                ProfileTerm(
                    canonical=canonical_value,
                    spoken=lines_to_tuple(spoken_text.get("1.0", "end")),
                    replace_from=lines_to_tuple(
                        replace_text.get("1.0", "end")
                    ),
                    description=description.get().strip(),
                )
            )
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="e",
            padx=12,
            pady=12,
        )
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(
            side="right"
        )
        ttk.Button(buttons, text="反映", command=save).pack(
            side="right",
            padx=(0, 8),
        )
        dialog.wait_window()
        return result[0] if result else None

    def _save_profile(self) -> None:
        profile_id = str(self._vars["profile.id"].get()).strip()
        name = str(self._vars["profile.name"].get()).strip()
        context = (
            self._profile_context.get("1.0", "end").strip()
            if self._profile_context is not None
            else ""
        )
        profile = Profile(
            profile_id=profile_id,
            name=name,
            context=context,
            terms=tuple(self._profile_terms),
        )
        snapshot = self._snapshot
        editor_id = str(self._vars["profile.editor"].get())
        if (
            snapshot is not None
            and profile_id in snapshot.profiles
            and editor_id != profile_id
            and not messagebox.askyesno(
                "既存プロフィールを上書きしますか？",
                (
                    f"プロフィールID「{profile_id}」は既に存在します。"
                    "現在の内容で上書きしますか？"
                ),
                icon="warning",
                parent=self._root,
            )
        ):
            self._set_status("プロフィールの保存をキャンセルしました")
            return
        try:
            snapshot = self._snapshot
            expected_fingerprint = (
                snapshot.profile_fingerprints.get(
                    profile.profile_id,
                    MISSING_FILE_FINGERPRINT,
                )
                if snapshot is not None
                else MISSING_FILE_FINGERPRINT
            )
            succeeded, message = self._on_save_profile(
                profile,
                expected_fingerprint,
            )
        except Exception:
            logger.exception("プロフィール保存コールバックで例外が発生しました")
            succeeded, message = False, "プロフィール保存中にエラーが発生しました"
        if not succeeded:
            messagebox.showerror(
                "プロフィールを保存できません",
                message,
                parent=self._root,
            )
            return
        active_label = str(
            self._vars["enhancement.profile_label"].get()
        )
        active_id = self._profile_id_by_label.get(active_label, "")
        refreshed = self._request_snapshot()
        if refreshed is None:
            self._set_status(f"{message}（画面の再読込に失敗）")
            return
        snapshot = (
            refresh_snapshot_resources(snapshot, refreshed)
            if snapshot is not None
            else refreshed
        )
        self._snapshot = snapshot
        self._refresh_profile_choices(snapshot)
        self._vars["enhancement.profile_label"].set(
            self._profile_label_by_id.get(active_id, "オフ")
        )
        self._vars["profile.editor"].set(profile_id)
        self._load_profile(profile_id)
        self._set_status(message)

    def _load_selected_postprocessor(
        self,
        event: object | None = None,
    ) -> None:
        preset_id = str(self._vars["postprocessor.editor"].get())
        if (
            preset_id != self._loaded_postprocessor_id
            and not self._confirm_discard_postprocessor_changes(
                "別のCLIプリセットへ切り替え"
            )
        ):
            self._vars["postprocessor.editor"].set(
                self._loaded_postprocessor_id
            )
            self._set_status("CLIプリセットの切替をキャンセルしました")
            return
        self._load_postprocessor(preset_id)

    def _load_postprocessor(self, preset_id: str) -> None:
        preset = (
            self._snapshot.postprocessors.get(preset_id)
            if self._snapshot is not None
            else None
        )
        self._vars["postprocessor.id"].set(
            preset.preset_id if preset else ""
        )
        id_widget = self._widgets.get("postprocessor.id")
        if isinstance(id_widget, ttk.Entry):
            id_widget.configure(state="disabled" if preset else "normal")
        self._vars["postprocessor.name"].set(
            preset.display_name if preset else ""
        )
        self._vars["postprocessor.destination"].set(
            preset.data_destination
            if preset
            else DATA_DESTINATION_UNKNOWN
        )
        self._vars["postprocessor.input_mode"].set(
            preset.input_mode if preset else "stdin"
        )
        self._vars["postprocessor.adapter"].set(
            preset.adapter if preset else ADAPTER_GENERIC
        )
        self._vars["postprocessor.model"].set(
            preset.model if preset else ""
        )
        self._vars["postprocessor.timeout"].set(
            str(preset.timeout_sec if preset else 30)
        )
        self._vars["postprocessor.preflight"].set(
            preset.preflight_command if preset else ""
        )
        self._vars["postprocessor.preflight_message"].set(
            preset.preflight_failure_message if preset else ""
        )
        for widget, value in (
            (self._command_text, preset.command if preset else ""),
            (
                self._environment_text,
                environment_to_text(preset.environment) if preset else "",
            ),
            (
                self._system_prompt_text,
                preset.system_prompt if preset else "",
            ),
            (
                self._prompt_text,
                preset.prompt_template if preset else "{{transcript}}",
            ),
        ):
            if widget is not None:
                widget.delete("1.0", "end")
                widget.insert("1.0", value)
        self._loaded_postprocessor_id = preset.preset_id if preset else ""
        self._postprocessor_editor_baseline = (
            self._capture_postprocessor_editor_state()
        )

    def _new_postprocessor(self) -> None:
        if not self._confirm_discard_postprocessor_changes(
            "新しいCLIプリセットを作成"
        ):
            self._set_status("新規CLIプリセットの作成をキャンセルしました")
            return
        self._vars["postprocessor.editor"].set("")
        self._load_postprocessor("")
        self._set_status("新しいCLIプリセットを入力してください")

    def _save_postprocessor(self) -> None:
        try:
            timeout_sec = float(
                str(self._vars["postprocessor.timeout"].get()).strip()
            )
            environment = parse_environment(
                self._environment_text.get("1.0", "end")
                if self._environment_text is not None
                else ""
            )
        except ValueError as exc:
            messagebox.showerror(
                "CLIプリセットを保存できません",
                str(exc),
                parent=self._root,
            )
            return
        preset = PostprocessorPreset(
            preset_id=str(self._vars["postprocessor.id"].get()).strip(),
            display_name=str(
                self._vars["postprocessor.name"].get()
            ).strip(),
            command=(
                self._command_text.get("1.0", "end").strip()
                if self._command_text is not None
                else ""
            ),
            input_mode=str(
                self._vars["postprocessor.input_mode"].get()
            ),
            output_mode="stdout",
            timeout_sec=timeout_sec,
            data_destination=str(
                self._vars["postprocessor.destination"].get()
            ),
            adapter=str(self._vars["postprocessor.adapter"].get()),
            model=str(self._vars["postprocessor.model"].get()).strip(),
            system_prompt=(
                self._system_prompt_text.get("1.0", "end").strip()
                if self._system_prompt_text is not None
                else ""
            ),
            prompt_template=(
                self._prompt_text.get("1.0", "end").strip()
                if self._prompt_text is not None
                else ""
            ),
            preflight_command=str(
                self._vars["postprocessor.preflight"].get()
            ).strip(),
            preflight_failure_message=str(
                self._vars["postprocessor.preflight_message"].get()
            ).strip(),
            environment=environment,
        )
        snapshot = self._snapshot
        editor_id = str(self._vars["postprocessor.editor"].get())
        original = (
            snapshot.postprocessors.get(preset.preset_id)
            if snapshot is not None
            else None
        )
        if local_transport_changed(original, preset):
            assert original is not None
            reclassified, cleared_fields = reclassify_changed_local_preset(
                original,
                preset,
            )
            clearing_notice = ""
            if cleared_fields:
                clearing_notice = (
                    "\n\n旧コマンドの値から変更されていない次の項目は"
                    "誤用防止のためクリアされます: "
                    + "、".join(cleared_fields)
                )
            else:
                clearing_notice = (
                    "\n\n今回編集した事前確認・環境変数の値は保持されます。"
                )
            if not messagebox.askyesno(
                "送信先を再確認してください",
                (
                    "ローカル扱いのプリセットでコマンドまたは環境変数が"
                    "変更されました。変更後の送信先は自動判定できません。\n\n"
                    "送信先を「unknown」に変更して保存しますか？"
                    f"{clearing_notice}"
                ),
                icon="warning",
                parent=self._root,
            ):
                self._set_status("CLI後処理の保存をキャンセルしました")
                return
            preset = reclassified
            self._vars["postprocessor.destination"].set(
                DATA_DESTINATION_UNKNOWN
            )
            self._vars["postprocessor.preflight"].set(
                preset.preflight_command
            )
            self._vars["postprocessor.preflight_message"].set(
                preset.preflight_failure_message
            )
            if self._environment_text is not None:
                self._environment_text.delete("1.0", "end")
                self._environment_text.insert(
                    "1.0",
                    environment_to_text(preset.environment),
                )
        if (
            snapshot is not None
            and preset.preset_id in snapshot.postprocessors
            and editor_id != preset.preset_id
            and not messagebox.askyesno(
                "既存CLIプリセットを上書きしますか？",
                (
                    f"プリセットID「{preset.preset_id}」は既に存在します。"
                    "ローカル設定として上書きしますか？"
                ),
                icon="warning",
                parent=self._root,
            )
        ):
            self._set_status("CLI後処理の保存をキャンセルしました")
            return
        if (
            snapshot is not None
            and snapshot.config.enhancement.postprocessor
            == preset.preset_id
            and preset.data_destination != DATA_DESTINATION_LOCAL
            and snapshot.postprocessors.get(preset.preset_id) != preset
            and not messagebox.askyesno(
                "使用中のCLI後処理を変更しますか？",
                (
                    "このプリセットは現在使用中です。保存後から、認識結果、"
                    "プロフィールの文脈、辞書データが変更後のCLIへ渡されます。\n\n"
                    "変更を保存しますか？"
                ),
                icon="warning",
                parent=self._root,
            )
        ):
            self._set_status("CLI後処理の保存をキャンセルしました")
            return
        try:
            expected_fingerprint = (
                self._snapshot.postprocessors_fingerprint
                if self._snapshot is not None
                else MISSING_FILE_FINGERPRINT
            )
            succeeded, message = self._on_save_postprocessor(
                preset,
                expected_fingerprint,
            )
        except Exception:
            logger.exception("CLIプリセット保存コールバックで例外が発生しました")
            succeeded, message = False, "CLIプリセット保存中にエラーが発生しました"
        if not succeeded:
            messagebox.showerror(
                "CLIプリセットを保存できません",
                message,
                parent=self._root,
            )
            return
        active_label = str(
            self._vars["enhancement.postprocessor_label"].get()
        )
        active_id = self._postprocessor_id_by_label.get(
            active_label,
            POSTPROCESSOR_OFF,
        )
        refreshed = self._request_snapshot()
        if refreshed is None:
            self._set_status(f"{message}（画面の再読込に失敗）")
            return
        snapshot = (
            refresh_snapshot_resources(snapshot, refreshed)
            if snapshot is not None
            else refreshed
        )
        self._snapshot = snapshot
        self._refresh_postprocessor_choices(snapshot)
        self._vars["enhancement.postprocessor_label"].set(
            self._postprocessor_label_by_id.get(active_id, "オフ")
        )
        self._vars["postprocessor.editor"].set(preset.preset_id)
        self._load_postprocessor(preset.preset_id)
        self._set_status(message)

    def _set_status(self, message: str) -> None:
        if self._status_var is not None:
            self._status_var.set(message)
