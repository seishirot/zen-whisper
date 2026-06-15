"""Audio input device discovery and resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sounddevice as sd

from src.platform import is_windows

logger = logging.getLogger(__name__)

_WINDOWS_HOST_API_PRIORITY = (
    "Windows WASAPI",
    "Windows DirectSound",
    "MME",
    "Windows WDM-KS",
)
_WINDOWS_MENU_HOST_APIS = {
    "MME",
    "Windows DirectSound",
    "Windows WASAPI",
}
_WINDOWS_MENU_HIDDEN_NAMES = {
    "microsoft sound mapper - input",
    "primary sound capture driver",
}
_WINDOWS_GENERIC_ACTIVE_ENDPOINT_NAMES = {
    "microphone",
    "line in",
    "マイク",
    "ライン入力",
    "ヘッドセット",
}
_TRUNCATED_NAME_PREFIX_MIN_LEN = 24


@dataclass(frozen=True)
class InputAudioDevice:
    """A PortAudio input device with app-specific availability metadata."""

    index: int
    name: str
    hostapi: str
    max_input_channels: int
    stream_channels: int
    default_samplerate: float
    supports_sample_rate: bool | None = None
    supports_default_samplerate: bool | None = None
    is_default: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.name}, {self.hostapi}"


@dataclass(frozen=True)
class ResolvedInputDevice:
    """Resolved microphone setting used to open the input stream."""

    configured: str
    stream_device: int | None
    actual_index: int | None
    name: str
    hostapi: str
    stream_sample_rate: int
    stream_channels: int
    fallback_used: bool = False
    fallback_reason: str | None = None

    @property
    def display_name(self) -> str:
        if self.actual_index is None:
            return self.name
        return f"#{self.actual_index} {self.name}"

    @property
    def warning_message(self) -> str | None:
        if not self.fallback_used or not self.configured:
            return None
        return f"マイク '{self.configured}' を使用できないため、OS既定入力へfallbackします"


def _hostapi_name(hostapi_index: int, hostapis: object) -> str:
    if hostapi_index < 0:
        return f"hostapi:{hostapi_index}"
    try:
        return str(hostapis[hostapi_index]["name"])  # type: ignore[index]
    except Exception:
        try:
            return str(sd.query_hostapis(hostapi_index)["name"])
        except Exception:
            return f"hostapi:{hostapi_index}"


def _clean_device_name(name: str) -> str:
    return " ".join(name.split())


def _name_key(name: str) -> str:
    return _clean_device_name(name).casefold()


def _same_microphone_name(left: str, right: str) -> bool:
    left_key = _name_key(left)
    right_key = _name_key(right)
    if left_key == right_key:
        return True
    shortest = min(len(left_key), len(right_key))
    if shortest < _TRUNCATED_NAME_PREFIX_MIN_LEN:
        return False
    return left_key.startswith(right_key) or right_key.startswith(left_key)


def _default_input_index() -> int | None:
    try:
        default_input = sd.query_devices(kind="input")
        index = default_input.get("index")
        if isinstance(index, int) and index >= 0:
            return index
    except Exception:
        logger.debug("OS既定入力デバイスの取得に失敗しました", exc_info=True)

    try:
        device = sd.default.device
        if isinstance(device, (list, tuple)):
            device = device[0]
        if isinstance(device, int) and device >= 0:
            return device
    except Exception:
        logger.debug("sounddevice.default.device の取得に失敗しました", exc_info=True)
    return None


def _supports_input_settings(index: int, sample_rate: int | None, channels: int) -> bool | None:
    if sample_rate is None:
        return None
    try:
        sd.check_input_settings(
            device=index,
            channels=channels,
            dtype="float32",
            samplerate=sample_rate,
        )
    except Exception:
        return False
    return True


def _can_stream_at_rate(
    supports_sample_rate: bool | None,
    supports_default_samplerate: bool | None,
    default_sample_rate: int | None,
) -> bool:
    return supports_sample_rate is not False or (
        supports_default_samplerate is not False and bool(default_sample_rate)
    )


def _resolve_stream_support(
    index: int,
    max_input_channels: int,
    sample_rate: int | None,
    default_sample_rate: int | None,
) -> tuple[int, bool | None, bool | None]:
    candidate_channels = [min(2, max_input_channels)]
    if candidate_channels[0] != 1:
        candidate_channels.append(1)

    fallback: tuple[int, bool | None, bool | None] | None = None
    for channels in candidate_channels:
        supports_sample_rate = _supports_input_settings(index, sample_rate, channels)
        supports_default_samplerate = _supports_input_settings(
            index,
            default_sample_rate,
            channels,
        )
        fallback = (channels, supports_sample_rate, supports_default_samplerate)
        if _can_stream_at_rate(
            supports_sample_rate,
            supports_default_samplerate,
            default_sample_rate,
        ):
            return fallback

    return fallback or (1, False, False)


def _default_stream_sample_rate(device: object) -> int | None:
    try:
        sample_rate = int(round(float(device.get("default_samplerate", 0.0))))  # type: ignore[attr-defined]
    except Exception:
        return None
    return sample_rate if sample_rate > 0 else None


def list_input_devices(sample_rate: int | None = None) -> list[InputAudioDevice]:
    """Return all input devices visible to PortAudio."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        logger.exception("オーディオデバイス一覧の取得に失敗しました")
        return []

    default_index = _default_input_index()
    inputs: list[InputAudioDevice] = []
    for index, device in enumerate(devices):
        max_input_channels = int(device.get("max_input_channels", 0))
        if max_input_channels <= 0:
            continue
        default_sample_rate = _default_stream_sample_rate(device)
        stream_channels, supports_sample_rate, supports_default_samplerate = (
            _resolve_stream_support(
                index,
                max_input_channels,
                sample_rate,
                default_sample_rate,
            )
        )
        inputs.append(
            InputAudioDevice(
                index=index,
                name=_clean_device_name(str(device.get("name", ""))),
                hostapi=_hostapi_name(int(device.get("hostapi", -1)), hostapis),
                max_input_channels=max_input_channels,
                stream_channels=stream_channels,
                default_samplerate=float(default_sample_rate or 0),
                supports_sample_rate=supports_sample_rate,
                supports_default_samplerate=supports_default_samplerate,
                is_default=index == default_index,
            )
        )
    return inputs


def default_input_device(sample_rate: int | None = None) -> InputAudioDevice | None:
    """Return the current OS default input device, if available."""
    default_index = _default_input_index()
    if default_index is None:
        return None
    for device in list_input_devices(sample_rate):
        if device.index == default_index:
            return device
    return None


def default_input_name(sample_rate: int | None = None) -> str:
    """Return a readable name for the current OS default input."""
    device = default_input_device(sample_rate)
    return device.name if device is not None else "不明"


def _hostapi_priority(hostapi: str) -> int:
    if not is_windows():
        return 0
    try:
        return _WINDOWS_HOST_API_PRIORITY.index(hostapi)
    except ValueError:
        return len(_WINDOWS_HOST_API_PRIORITY)


def _usable_devices(sample_rate: int) -> list[InputAudioDevice]:
    return [
        device
        for device in list_input_devices(sample_rate)
        if _stream_sample_rate(device, sample_rate) is not None
    ]


def _sort_candidates(devices: list[InputAudioDevice]) -> list[InputAudioDevice]:
    return sorted(devices, key=lambda device: (_hostapi_priority(device.hostapi), device.index))


def _is_menu_visible_device(device: InputAudioDevice) -> bool:
    if not is_windows():
        return True
    if device.hostapi not in _WINDOWS_MENU_HOST_APIS:
        return False
    return _name_key(device.name) not in _WINDOWS_MENU_HIDDEN_NAMES


def _active_capture_device_names() -> set[str] | None:
    if not is_windows():
        return None
    try:
        from src.platform.windows import active_capture_device_names
    except Exception:
        logger.debug("Windows active capture endpoint filter is unavailable", exc_info=True)
        return None
    return active_capture_device_names()


def _is_specific_active_endpoint_name(name: str) -> bool:
    key = _name_key(name)
    if not key or key in _WINDOWS_GENERIC_ACTIVE_ENDPOINT_NAMES:
        return False
    return len(key) >= 4


def _matches_active_capture_endpoint(device_name: str, active_names: set[str] | None) -> bool:
    if active_names is None:
        return True
    device_key = _name_key(device_name)
    for active_name in active_names:
        active_key = _name_key(active_name)
        if _same_microphone_name(device_name, active_name):
            return True
        if not _is_specific_active_endpoint_name(active_name):
            continue
        if active_key in device_key or device_key in active_key:
            return True
    return False


def _filter_active_capture_devices(devices: list[InputAudioDevice]) -> list[InputAudioDevice]:
    active_names = _active_capture_device_names()
    if not active_names:
        return devices
    return [
        device
        for device in devices
        if _matches_active_capture_endpoint(device.name, active_names)
    ]


def list_microphone_names(sample_rate: int) -> list[str]:
    """Return de-duplicated microphone names that have a usable input candidate."""
    names: list[str] = []
    for device in _filter_active_capture_devices(_usable_devices(sample_rate)):
        if not _is_menu_visible_device(device):
            continue
        for position, existing_name in enumerate(names):
            if _same_microphone_name(device.name, existing_name):
                if len(device.name) > len(existing_name):
                    names[position] = device.name
                break
        else:
            names.append(device.name)
    return names


def _find_device_by_index(index: int, sample_rate: int) -> InputAudioDevice | None:
    for device in list_input_devices(sample_rate):
        if device.index == index:
            return device
    return None


def _matches_query(device: InputAudioDevice, query: str) -> bool:
    lowered_query = _name_key(query)
    return lowered_query in _name_key(device.name) or lowered_query in _name_key(device.full_name)


def _matching_devices(
    microphone: str,
    sample_rate: int,
    *,
    active_only: bool = False,
) -> list[InputAudioDevice]:
    query = microphone.strip()
    if not query:
        return []

    devices = _usable_devices(sample_rate)
    if active_only:
        devices = _filter_active_capture_devices(devices)
    lowered_query = _name_key(query)
    exact = [
        device
        for device in devices
        if lowered_query in (_name_key(device.name), _name_key(device.full_name))
        or _same_microphone_name(query, device.name)
    ]
    if exact:
        return exact
    return [device for device in devices if _matches_query(device, query)]


def microphone_available(microphone: str, sample_rate: int) -> bool:
    """Return whether the configured microphone can be resolved without fallback."""
    configured = microphone.strip()
    if not configured:
        return True
    try:
        index = int(configured)
    except ValueError:
        return bool(_matching_devices(configured, sample_rate, active_only=True))

    device = _find_device_by_index(index, sample_rate)
    return (
        device is not None
        and _stream_sample_rate(device, sample_rate) is not None
        and device in _filter_active_capture_devices([device])
    )


def _stream_sample_rate(device: InputAudioDevice, target_sample_rate: int) -> int | None:
    if device.supports_sample_rate is not False:
        return target_sample_rate
    if device.supports_default_samplerate is not False and device.default_samplerate > 0:
        return int(round(device.default_samplerate))
    return None


def _default_resolution(configured: str, sample_rate: int, reason: str | None = None) -> ResolvedInputDevice:
    default_device = default_input_device(sample_rate)
    stream_sample_rate = (
        _stream_sample_rate(default_device, sample_rate)
        if default_device is not None
        else sample_rate
    )
    return ResolvedInputDevice(
        configured=configured,
        stream_device=None,
        actual_index=default_device.index if default_device is not None else None,
        name=default_device.name if default_device is not None else "OS既定入力",
        hostapi=default_device.hostapi if default_device is not None else "",
        stream_sample_rate=stream_sample_rate or sample_rate,
        stream_channels=default_device.stream_channels if default_device is not None else 1,
        fallback_used=bool(reason),
        fallback_reason=reason,
    )


def resolve_input_device(microphone: str, sample_rate: int) -> ResolvedInputDevice:
    """Resolve the configured microphone to a concrete input device.

    Empty configuration intentionally keeps ``stream_device=None`` so PortAudio
    follows the current OS default input. Non-empty values are resolved to a
    concrete device when possible; otherwise the stream falls back to the OS
    default without mutating configuration.
    """
    configured = microphone.strip()
    if not configured:
        return _default_resolution(configured, sample_rate)

    try:
        index = int(configured)
    except ValueError:
        candidates = _matching_devices(configured, sample_rate, active_only=True)
        if not candidates:
            return _default_resolution(
                configured,
                sample_rate,
                f"configured microphone '{configured}' was not found or is unsupported",
            )
        device = _sort_candidates(candidates)[0]
    else:
        device = _find_device_by_index(index, sample_rate)
        if (
            device is None
            or _stream_sample_rate(device, sample_rate) is None
            or device not in _filter_active_capture_devices([device])
        ):
            return _default_resolution(
                configured,
                sample_rate,
                f"configured microphone id '{configured}' was not found or is unsupported",
            )

    return ResolvedInputDevice(
        configured=configured,
        stream_device=device.index,
        actual_index=device.index,
        name=device.name,
        hostapi=device.hostapi,
        stream_sample_rate=_stream_sample_rate(device, sample_rate) or sample_rate,
        stream_channels=device.stream_channels,
        fallback_used=False,
    )
