"""システムトレイモジュール。pystray + Pillow で動的アイコンとメニューを提供する。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from src.audio_devices import default_input_name, list_microphone_names, microphone_available
from src.config import (
    ENGINE_QWEN3_ASR,
    ENGINE_REAZON_K2,
    ENGINE_WHISPER,
    QWEN3_MODEL_LARGE,
    QWEN3_MODEL_SMALL,
    FeedbackConfig,
)
from src.platform import is_mac
from src.startup import is_registered, toggle as toggle_startup
from src.transcriber import (
    is_qwen3_available,
    is_reazon_k2_available,
    is_whisper_cuda_available,
)

logger = logging.getLogger(__name__)

_ICON_SIZE = 64


class TrayState(Enum):
    IDLE = "idle"
    LOADING = "loading"
    RECORDING = "recording"
    SPEECH_DETECTED = "speech_detected"
    TRANSCRIBING = "transcribing"


_STATE_COLORS: dict[TrayState, str] = {
    TrayState.IDLE: "#808080",       # グレー
    TrayState.LOADING: "#FF8C00",    # オレンジ（モデルロード中）
    TrayState.RECORDING: "#FF0000",  # 赤
    TrayState.SPEECH_DETECTED: "#00CC00",  # 緑（音声検知中）
    TrayState.TRANSCRIBING: "#FFD700",  # 黄
}


def _create_icon_image(color: str) -> Image.Image:
    """指定色の背景にマイクシルエットを描いたアイコンを生成する。"""
    size = _ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景円
    margin = 2
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
    )

    # マイクシルエット（白）
    mic_color = "#FFFFFF"
    cx = size // 2  # 中心X

    # マイク本体（角丸長方形）
    mic_w = size // 5       # マイク幅
    mic_h = size * 5 // 16  # マイク高さ
    mic_top = size // 5
    mic_left = cx - mic_w // 2
    mic_right = cx + mic_w // 2
    mic_bottom = mic_top + mic_h
    r = mic_w // 2  # 角丸の半径
    draw.rounded_rectangle(
        [mic_left, mic_top, mic_right, mic_bottom],
        radius=r,
        fill=mic_color,
    )

    # マイクの弧（U字型のキャリア）
    arc_w = size * 2 // 5
    arc_top = size // 4
    arc_bottom = size * 5 // 8
    arc_left = cx - arc_w // 2
    arc_right = cx + arc_w // 2
    line_w = max(2, size // 16)
    draw.arc(
        [arc_left, arc_top, arc_right, arc_bottom],
        start=0, end=180,
        fill=mic_color, width=line_w,
    )

    # スタンド（縦線）
    stand_top = arc_bottom - line_w // 2
    stand_bottom = size * 3 // 4
    draw.line(
        [(cx, stand_top), (cx, stand_bottom)],
        fill=mic_color, width=line_w,
    )

    # ベース（横線）
    base_w = size // 4
    draw.line(
        [(cx - base_w // 2, stand_bottom), (cx + base_w // 2, stand_bottom)],
        fill=mic_color, width=line_w,
    )

    return img


class TrayApp:
    """システムトレイアプリケーション。"""

    def __init__(
        self,
        on_set_language: Callable[[str], None],
        on_set_engine: Callable[[str, str | None, str | None], None],
        on_quit: Callable[[], None],
        on_set_microphone: Callable[[str], None] | None = None,
        initial_language: str = "ja",
        initial_engine: str = ENGINE_WHISPER,
        initial_device: str | None = None,
        initial_microphone: str = "",
        sample_rate: int = 16000,
        initial_qwen3_model: str = QWEN3_MODEL_LARGE,
        feedback_config: FeedbackConfig | None = None,
        on_save_config: Callable[[], bool] | None = None,
    ) -> None:
        self._on_set_language = on_set_language
        self._on_set_engine = on_set_engine
        self._on_set_microphone = on_set_microphone
        self._on_quit = on_quit
        self._language = initial_language
        self._engine = initial_engine
        self._device = initial_device or ("mlx" if is_mac() else "cuda")
        self._microphone = initial_microphone
        self._sample_rate = sample_rate
        self._qwen3_model = initial_qwen3_model
        self._feedback_config = feedback_config
        self._on_save_config = on_save_config
        self._state = TrayState.IDLE
        self._icon: Icon | None = None

    def _is_lang(self, lang: str) -> Callable[[MenuItem], bool]:
        """メニューアイテムのチェック状態を返すコールバック。"""
        def checked(item: MenuItem) -> bool:
            return self._language == lang
        return checked

    def _set_lang(self, lang: str) -> Callable[[Icon, MenuItem], None]:
        """言語切替のコールバック。"""
        def handler(icon: Icon, item: MenuItem) -> None:
            self._language = lang
            self._on_set_language(lang)
            logger.info("言語を %s に切替えました（トレイメニュー）", lang)
            self._update_icon()
        return handler

    def _is_microphone(self, microphone: str) -> Callable[[MenuItem], bool]:
        """マイクメニューアイテムのチェック状態を返すコールバック。"""
        def checked(item: MenuItem) -> bool:
            return self._microphone == microphone
        return checked

    def _set_microphone(self, microphone: str) -> Callable[[Icon, MenuItem], None]:
        """マイク切替のコールバック。"""
        def handler(icon: Icon, item: MenuItem) -> None:
            self._microphone = microphone
            if self._on_set_microphone is not None:
                self._on_set_microphone(microphone)
            logger.info(
                "マイクを %s に切替えました（トレイメニュー）",
                microphone or "OS既定",
            )
            self._update_title()
        return handler

    def _refresh_microphones(self, icon: Icon, item: MenuItem) -> None:
        """マイク一覧を再取得してメニューを更新する。"""
        logger.info("マイク一覧を更新します（トレイメニュー）")
        self.refresh_menu()
        self.notify("マイク一覧を更新しました")

    def _build_microphone_menu(self) -> Menu:
        default_name = default_input_name(self._sample_rate)
        items: list[MenuItem] = [
            MenuItem(
                f"OS既定: {default_name}",
                self._set_microphone(""),
                checked=self._is_microphone(""),
                radio=True,
            )
        ]

        if self._microphone and not microphone_available(self._microphone, self._sample_rate):
            items.append(
                MenuItem(
                    f"選択中: {self._microphone}（未検出・OS既定へfallback）",
                    None,
                    enabled=False,
                )
            )

        for name in list_microphone_names(self._sample_rate):
            items.append(
                MenuItem(
                    name,
                    self._set_microphone(name),
                    checked=self._is_microphone(name),
                    radio=True,
                )
            )

        items.extend(
            [
                Menu.SEPARATOR,
                MenuItem("マイク一覧を更新", self._refresh_microphones),
            ]
        )
        return Menu(*items)

    def _is_engine(
        self,
        engine: str,
        qwen3_model: str | None = None,
        device: str | None = None,
    ) -> Callable[[MenuItem], bool]:
        """エンジンメニューアイテムのチェック状態を返すコールバック。

        qwen3_model / device を指定した場合は、その下位設定まで一致しているかで判定する。
        """
        def checked(item: MenuItem) -> bool:
            if self._engine != engine:
                return False
            if qwen3_model is not None:
                return self._qwen3_model == qwen3_model
            if device is not None:
                return self._device == device
            return True
        return checked

    def _set_engine(
        self,
        engine: str,
        qwen3_model: str | None = None,
        device: str | None = None,
    ) -> Callable[[Icon, MenuItem], None]:
        """エンジン（および Qwen のモデルサイズ）切替のコールバック。"""
        def handler(icon: Icon, item: MenuItem) -> None:
            self._engine = engine
            if qwen3_model is not None:
                self._qwen3_model = qwen3_model
            if device is not None:
                self._device = device
            self._on_set_engine(engine, qwen3_model, device)
            logger.info(
                "エンジンを %s%s%s に切替えました（トレイメニュー）",
                engine,
                f" ({qwen3_model})" if qwen3_model else "",
                f" [{device}]" if device else "",
            )
            self._update_icon()
        return handler

    def _is_qwen3_enabled(self, item: MenuItem) -> bool:
        """Qwen3-ASR メニュー項目が有効かどうかを返す。"""
        return is_qwen3_available() and not is_mac()

    def _is_reazon_enabled(self, item: MenuItem) -> bool:
        """ReazonSpeech K2 メニュー項目が有効かどうかを返す。"""
        return is_reazon_k2_available() and not is_mac()

    def _is_whisper_cuda_enabled(self, item: MenuItem) -> bool:
        """Whisper CUDA メニュー項目が有効かどうかを返す。"""
        return is_whisper_cuda_available() and not is_mac()

    def _is_whisper_cpu_enabled(self, item: MenuItem) -> bool:
        """Whisper CPU メニュー項目が有効かどうかを返す。"""
        return not is_mac()

    def _is_whisper_mlx_enabled(self, item: MenuItem) -> bool:
        """Whisper MLX メニュー項目が有効かどうかを返す。"""
        return is_mac()

    def _toggle_startup(self, icon: Icon, item: MenuItem) -> None:
        """スタートアップ登録のトグルコールバック。"""
        new_state = toggle_startup()
        status = "登録" if new_state else "解除"
        logger.info("スタートアップを%sしました", status)
        self.notify(f"スタートアップ: {status}")

    def _is_startup_registered(self, item: MenuItem) -> bool:
        """スタートアップ登録状態を返す。"""
        return is_registered()

    def _toggle_sound(self, icon: Icon, item: MenuItem) -> None:
        """サウンド有効/無効のトグルコールバック。"""
        if self._feedback_config is None:
            return
        self._feedback_config.sound_enabled = not self._feedback_config.sound_enabled
        status = "ON" if self._feedback_config.sound_enabled else "OFF"
        logger.info("サウンドを %s に切替えました", status)
        self.notify(f"サウンド: {status}")
        if self._on_save_config is not None:
            self._on_save_config()

    def _is_sound_enabled(self, item: MenuItem) -> bool:
        """サウンド有効状態を返す。"""
        if self._feedback_config is None:
            return False
        return self._feedback_config.sound_enabled

    def _quit(self, icon: Icon, item: MenuItem) -> None:
        """終了コールバック。"""
        logger.info("トレイメニューから終了が選択されました")
        self._on_quit()
        icon.stop()

    def _build_menu(self) -> Menu:
        return Menu(
            MenuItem(
                "言語",
                Menu(
                    MenuItem(
                        "日本語",
                        self._set_lang("ja"),
                        checked=self._is_lang("ja"),
                        radio=True,
                    ),
                    MenuItem(
                        "English",
                        self._set_lang("en"),
                        checked=self._is_lang("en"),
                        radio=True,
                    ),
                ),
            ),
            MenuItem("マイク", self._build_microphone_menu()),
            MenuItem(
                "エンジン",
                Menu(
                    MenuItem(
                        "Whisper",
                        Menu(
                            MenuItem(
                                "GPU (CUDA)",
                                self._set_engine(ENGINE_WHISPER, device="cuda"),
                                checked=self._is_engine(ENGINE_WHISPER, device="cuda"),
                                radio=True,
                                enabled=self._is_whisper_cuda_enabled,
                            ),
                            MenuItem(
                                "CPU (int8)",
                                self._set_engine(ENGINE_WHISPER, device="cpu"),
                                checked=self._is_engine(ENGINE_WHISPER, device="cpu"),
                                radio=True,
                                enabled=self._is_whisper_cpu_enabled,
                            ),
                            MenuItem(
                                "MLX",
                                self._set_engine(ENGINE_WHISPER, device="mlx"),
                                checked=self._is_engine(ENGINE_WHISPER, device="mlx"),
                                radio=True,
                                enabled=self._is_whisper_mlx_enabled,
                            ),
                        ),
                    ),
                    MenuItem(
                        "Reazon K2",
                        self._set_engine(ENGINE_REAZON_K2, device="cpu"),
                        checked=self._is_engine(ENGINE_REAZON_K2),
                        radio=True,
                        enabled=self._is_reazon_enabled,
                    ),
                    MenuItem(
                        "Qwen3-ASR",
                        Menu(
                            MenuItem(
                                "1.7B",
                                self._set_engine(
                                    ENGINE_QWEN3_ASR,
                                    QWEN3_MODEL_LARGE,
                                    device="cuda",
                                ),
                                checked=self._is_engine(
                                    ENGINE_QWEN3_ASR,
                                    QWEN3_MODEL_LARGE,
                                ),
                                radio=True,
                                enabled=self._is_qwen3_enabled,
                            ),
                            MenuItem(
                                "0.6B",
                                self._set_engine(
                                    ENGINE_QWEN3_ASR,
                                    QWEN3_MODEL_SMALL,
                                    device="cuda",
                                ),
                                checked=self._is_engine(
                                    ENGINE_QWEN3_ASR,
                                    QWEN3_MODEL_SMALL,
                                ),
                                radio=True,
                                enabled=self._is_qwen3_enabled,
                            ),
                        ),
                    ),
                ),
            ),
            Menu.SEPARATOR,
            MenuItem(
                "スタートアップに登録",
                self._toggle_startup,
                checked=self._is_startup_registered,
            ),
            MenuItem(
                "サウンド",
                self._toggle_sound,
                checked=self._is_sound_enabled,
            ),
            Menu.SEPARATOR,
            MenuItem("終了", self._quit),
        )

    def _update_icon(self) -> None:
        if self._icon is not None:
            self._icon.icon = _create_icon_image(_STATE_COLORS[self._state])
            self._update_title()

    def _title_text(self) -> str:
        microphone = self._microphone or "OS既定"
        return f"zen-whisper - マイク: {microphone}"

    def _update_title(self) -> None:
        if self._icon is not None:
            self._icon.title = self._title_text()

    def refresh_menu(self) -> None:
        """トレイメニューを現在のデバイス状態で再構築する。"""
        if self._icon is not None:
            self._icon.menu = self._build_menu()

    def set_state(self, state: TrayState) -> None:
        """トレイアイコンの状態を変更する。"""
        self._state = state
        self._update_icon()

    def set_language(self, lang: str) -> None:
        """現在の言語表示を更新する（ホットキーからの切替時に呼ぶ）。"""
        self._language = lang
        self._update_icon()

    def notify(self, message: str, title: str = "zen-whisper") -> None:
        """トレイ通知を表示する。"""
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                logger.debug("トレイ通知の表示に失敗しました", exc_info=True)

    def run(self, setup: Callable[[Icon], None] | None = None) -> None:
        """
        トレイアイコンを表示し、イベントループを開始する（メインスレッドをブロック）。

        Args:
            setup: Icon.run() の setup コールバック。アイコン表示後に呼ばれる。
        """
        self._icon = Icon(
            name="zen-whisper",
            icon=_create_icon_image(_STATE_COLORS[TrayState.IDLE]),
            title=self._title_text(),
            menu=self._build_menu(),
        )
        def _setup_wrapper(icon: Icon) -> None:
            icon.visible = True
            if setup is not None:
                setup(icon)

        self._icon.run(setup=_setup_wrapper)

    def run_detached(self, setup: Callable[[Icon], None] | None = None) -> None:
        """
        トレイアイコンをバックグラウンドスレッドで起動する（メインスレッドをブロックしない）。

        Args:
            setup: Icon.run_detached() の setup コールバック。アイコン表示後に呼ばれる。
        """
        self._icon = Icon(
            name="zen-whisper",
            icon=_create_icon_image(_STATE_COLORS[TrayState.IDLE]),
            title=self._title_text(),
            menu=self._build_menu(),
        )
        def _setup_wrapper(icon: Icon) -> None:
            icon.visible = True
            if setup is not None:
                setup(icon)

        self._icon.run_detached(setup=_setup_wrapper)

    def stop(self) -> None:
        """トレイアイコンを停止する。"""
        if self._icon is not None:
            self._icon.stop()
