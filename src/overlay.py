"""フローティングマイクウィジェットモジュール。タイトルバー付きの小さなウィンドウを表示する。"""

from __future__ import annotations

import logging
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable

from src.config import OverlayConfig
from src.tray import TrayState

logger = logging.getLogger(__name__)

_STATE_COLORS: dict[TrayState, str] = {
    TrayState.IDLE: "#808080",
    TrayState.LOADING: "#FF8C00",
    TrayState.RECORDING: "#FF3B30",
    TrayState.SPEECH_DETECTED: "#34C759",
    TrayState.TRANSCRIBING: "#FFD700",
}

_STATE_LABELS: dict[TrayState, str] = {
    TrayState.IDLE: "待機中",
    TrayState.LOADING: "準備中…",
    TrayState.RECORDING: "録音中…",
    TrayState.SPEECH_DETECTED: "録音中…",
    TrayState.TRANSCRIBING: "処理中…",
}

# レイアウト定数
_WIDTH = 110
_TITLEBAR_HEIGHT = 24
_BODY_HEIGHT = 44
_HEIGHT = _TITLEBAR_HEIGHT + _BODY_HEIGHT
_MIC_CX = 18
_CONTENT_CY = 20
_TEXT_X = 75
_TIMER_UPDATE_MS = 250

# プラットフォーム別フォント
_FONT_FAMILY = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"


def _is_recording_state(state: TrayState) -> bool:
    """録音タイマーを表示する状態かどうかを返す。"""
    return state in (TrayState.RECORDING, TrayState.SPEECH_DETECTED)


def _format_elapsed_seconds(elapsed_seconds: float) -> str:
    """経過秒数を MM:SS 形式に整形する。"""
    total_seconds = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _get_active_monitor_rect() -> tuple[int, int, int, int]:
    """アクティブウィンドウがあるモニターのワークエリア (left, top, right, bottom) を返す。"""
    if sys.platform == "win32":
        from src.platform.windows import get_active_monitor_rect
        return get_active_monitor_rect()
    elif sys.platform == "darwin":
        from src.platform.darwin import get_active_monitor_rect
        return get_active_monitor_rect()
    else:
        return (0, 0, 1920, 1080)


def _setup_overlay_window(root: tk.Tk) -> None:
    """プラットフォーム固有のウィンドウ属性を設定する。"""
    if sys.platform == "win32":
        from src.platform.windows import setup_overlay_window
        setup_overlay_window(root)
    elif sys.platform == "darwin":
        from src.platform.darwin import setup_overlay_window
        setup_overlay_window(root)
        # オプション: Cmd キートグルでクリックスルーを動的に切り替え
        try:
            from src.platform.darwin import setup_overlay_event_monitor
            setup_overlay_event_monitor(root)
        except Exception:
            logger.debug("Cmd キートグルイベントモニターの設定に失敗しました", exc_info=True)


class OverlayIndicator:
    """タイトルバー付きフローティングマイクウィジェット。"""

    def __init__(self, cfg: OverlayConfig, on_click: Callable[[], None] | None = None) -> None:
        self._cfg = cfg
        self._on_click = on_click
        self._root: tk.Tk | None = None
        self._body_canvas: tk.Canvas | None = None
        self._mic_ids: list[int] = []
        self._label_id: int | None = None
        self._ready = threading.Event()
        self._current_state = TrayState.IDLE
        self._positioned = False
        self._recording_started_at: float | None = None
        self._timer_after_id: str | None = None
        # ドラッグ用
        self._drag_x = 0
        self._drag_y = 0

        if not cfg.enabled:
            return

        t = threading.Thread(target=self._run_tk, daemon=True)
        t.start()
        self._ready.wait(timeout=5.0)

    def _run_tk(self) -> None:
        """専用スレッドで tkinter mainloop を実行する。"""
        try:
            self._root = tk.Tk()
            root = self._root

            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#2D2D2D")
            root.geometry(f"{_WIDTH}x{_HEIGHT}")

            # プラットフォーム固有のウィンドウ属性を設定
            _setup_overlay_window(root)

            # ── タイトルバー ──────────────────────────────
            titlebar = tk.Frame(root, bg="#1E1E1E", height=_TITLEBAR_HEIGHT)
            titlebar.pack(fill="x", side="top")
            titlebar.pack_propagate(False)

            title_label = tk.Label(
                titlebar, text="zen-whisper", fg="#999999", bg="#1E1E1E",
                font=(_FONT_FAMILY, 8), anchor="w", padx=6,
            )
            title_label.pack(side="left", fill="both", expand=True)

            close_btn = tk.Label(
                titlebar, text="✕", fg="#999999", bg="#1E1E1E",
                font=(_FONT_FAMILY, 9), width=3, cursor="hand2",
            )
            close_btn.pack(side="right")
            close_btn.bind("<Enter>", lambda e: close_btn.configure(bg="#C42B1C", fg="#FFFFFF"))
            close_btn.bind("<Leave>", lambda e: close_btn.configure(bg="#1E1E1E", fg="#999999"))
            close_btn.bind("<Button-1>", lambda e: self._hide())

            # タイトルバーでドラッグ移動
            for widget in (titlebar, title_label):
                widget.bind("<ButtonPress-1>", self._on_drag_start)
                widget.bind("<B1-Motion>", self._on_drag_motion)

            # ── ボディ（マイクアイコン + 状態テキスト）───────
            self._body_canvas = tk.Canvas(
                root, width=_WIDTH, height=_BODY_HEIGHT,
                bg="#2D2D2D", highlightthickness=0,
            )
            self._body_canvas.pack(fill="both", expand=True)

            # マイクアイコン (左側)
            self._draw_mic_icon(_MIC_CX, _CONTENT_CY, "#808080")

            # 状態テキスト (右側)
            self._label_id = self._body_canvas.create_text(
                _TEXT_X, _CONTENT_CY, text="待機中",
                fill="#CCCCCC", font=(_FONT_FAMILY, 11),
                anchor="center",
            )

            # ボディクリック → 録音トグル
            self._body_canvas.bind("<Button-1>", self._on_body_click)

            # 初期状態は非表示
            root.withdraw()
            self._ready.set()
            root.mainloop()
        except Exception:
            logger.exception("オーバーレイの初期化に失敗しました")
            self._ready.set()

    def _draw_mic_icon(self, cx: int, cy: int, color: str) -> None:
        """Canvas上にマイクアイコンを描画する。"""
        if self._body_canvas is None:
            return
        canvas = self._body_canvas
        for item_id in self._mic_ids:
            canvas.delete(item_id)
        self._mic_ids.clear()

        self._mic_ids.append(canvas.create_oval(
            cx - 5, cy - 10, cx + 5, cy + 2, fill=color, outline="",
        ))
        self._mic_ids.append(canvas.create_arc(
            cx - 8, cy - 6, cx + 8, cy + 8, start=180, extent=180,
            style="arc", outline=color, width=2,
        ))
        self._mic_ids.append(canvas.create_line(
            cx, cy + 8, cx, cy + 14, fill=color, width=2,
        ))
        self._mic_ids.append(canvas.create_line(
            cx - 5, cy + 14, cx + 5, cy + 14, fill=color, width=2,
        ))

    def _on_body_click(self, event: tk.Event) -> None:
        if self._on_click is not None:
            threading.Thread(target=self._on_click, daemon=True).start()

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root
        self._drag_y = event.y_root

    def _on_drag_motion(self, event: tk.Event) -> None:
        if self._root is None:
            return
        dx = event.x_root - self._drag_x
        dy = event.y_root - self._drag_y
        x = self._root.winfo_x() + dx
        y = self._root.winfo_y() + dy
        self._root.geometry(f"+{x}+{y}")
        self._drag_x = event.x_root
        self._drag_y = event.y_root

    def _position_on_active_monitor(self) -> None:
        """アクティブモニターの下部中央にウィジェットを配置する。"""
        if self._root is None:
            return
        left, top, right, bottom = _get_active_monitor_rect()
        x = left + (right - left - _WIDTH) // 2
        y = bottom - _HEIGHT - 60  # タスクバー/Dock の少し上
        self._root.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

    def _start_recording_timer(self) -> None:
        """録音経過タイマーを開始する。"""
        if self._root is None or self._body_canvas is None or self._label_id is None:
            return
        if self._recording_started_at is None:
            self._recording_started_at = time.monotonic()
        if self._timer_after_id is None:
            self._update_recording_timer()

    def _stop_recording_timer(self) -> None:
        """録音経過タイマーを停止して開始時刻をリセットする。"""
        if self._root is not None and self._timer_after_id is not None:
            try:
                self._root.after_cancel(self._timer_after_id)
            except tk.TclError:
                logger.debug("録音タイマーのキャンセルに失敗しました", exc_info=True)
        self._timer_after_id = None
        self._recording_started_at = None

    def _update_recording_timer(self) -> None:
        """録音経過タイマーの表示を更新し、次回更新を予約する。"""
        self._timer_after_id = None
        if (
            self._root is None
            or self._body_canvas is None
            or self._label_id is None
            or not _is_recording_state(self._current_state)
        ):
            return
        if self._recording_started_at is None:
            self._recording_started_at = time.monotonic()

        elapsed = time.monotonic() - self._recording_started_at
        self._body_canvas.itemconfig(
            self._label_id,
            text=_format_elapsed_seconds(elapsed),
            fill="#FFFFFF",
        )
        self._timer_after_id = self._root.after(_TIMER_UPDATE_MS, self._update_recording_timer)

    def set_state(self, state: TrayState) -> None:
        """ウィジェットの状態を変更する。"""
        if not self._cfg.enabled or self._root is None:
            return

        def _update() -> None:
            if self._root is None or self._body_canvas is None:
                return

            prev_state = self._current_state
            self._current_state = state
            color = _STATE_COLORS.get(state, "#808080")
            label = _STATE_LABELS.get(state, "")

            if _is_recording_state(state):
                if not self._positioned:
                    self._position_on_active_monitor()
                    self._positioned = True
                self._draw_mic_icon(_MIC_CX, _CONTENT_CY, color)
                self._start_recording_timer()
                self._root.deiconify()
                self._root.lift()
            elif state == TrayState.IDLE:
                self._stop_recording_timer()
                self._draw_mic_icon(_MIC_CX, _CONTENT_CY, color)
                self._body_canvas.itemconfig(self._label_id, text=label, fill="#AAAAAA")
                # LOADING → IDLE 遷移時はオーバーレイを自動で非表示にする
                if prev_state == TrayState.LOADING:
                    self._root.withdraw()
                    self._positioned = False
            else:
                self._stop_recording_timer()
                if not self._positioned:
                    self._position_on_active_monitor()
                    self._positioned = True
                self._draw_mic_icon(_MIC_CX, _CONTENT_CY, color)
                self._body_canvas.itemconfig(self._label_id, text=label, fill="#FFFFFF")
                self._root.deiconify()
                self._root.lift()

        try:
            self._root.after(0, _update)
        except Exception:
            logger.debug("オーバーレイの更新に失敗しました", exc_info=True)

    def _hide(self) -> None:
        """ウィジェットを非表示にする。次回表示時に再配置される。"""
        if self._root is not None:
            self._root.withdraw()
            self._positioned = False

    def stop(self) -> None:
        """オーバーレイを停止する。"""
        if self._root is not None:
            try:
                def _destroy() -> None:
                    self._stop_recording_timer()
                    if self._root is not None:
                        self._root.destroy()

                self._root.after(0, _destroy)
            except Exception:
                pass
