"""Windows NVIDIA selection by stable UUID, without allocating a CUDA context."""

from __future__ import annotations

import csv
import ctypes
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from src.config import RecognitionConfig


class GPUSelectionError(RuntimeError):
    """An actionable GPU selection or resource error suitable for the UI."""


@dataclass(frozen=True)
class NvidiaGPU:
    index: int
    uuid: str
    name: str
    total_mib: int
    free_mib: int

    @property
    def label(self) -> str:
        return f"{self.name}（{self.total_mib / 1024:.1f} GiB・GPU {self.index}）"


def valid_gpu_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(
        r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value,
    ))


def list_nvidia_gpus() -> list[NvidiaGPU]:
    """Read current physical GPU names and free dedicated memory via the driver CLI."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        raise GPUSelectionError("NVIDIA GPU一覧を取得できません。ドライバーとnvidia-smiを確認してください")
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,uuid,name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
            check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        devices = []
        for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
            if not row:
                continue
            index, identity, name, total, free = (part.strip() for part in row)
            device = NvidiaGPU(int(index), identity, name, int(total), int(free))
            if not valid_gpu_uuid(identity) or not name or device.index < 0 or not 0 <= device.free_mib <= device.total_mib:
                raise ValueError("Invalid GPU inventory")
            devices.append(device)
        if len({d.uuid for d in devices}) != len(devices) or len({d.index for d in devices}) != len(devices):
            raise ValueError("Duplicate GPU identity")
        return devices
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise GPUSelectionError("GPUの空きVRAMを確認できないためロードを停止しました。GPU一覧を更新してください") from exc


def select_gpu(cfg: RecognitionConfig, devices: list[NvidiaGPU] | None = None) -> NvidiaGPU:
    if not isinstance(cfg.cuda_gpu_uuid, str) or (cfg.cuda_gpu_uuid and not valid_gpu_uuid(cfg.cuda_gpu_uuid)):
        raise GPUSelectionError("GPUの選択が無効です。GPUを選び直してください")
    devices = list_nvidia_gpus() if devices is None else devices
    # Preserve existing CrispASR numeric configurations until a named selection
    # is saved. Once selected by UUID, disappearance must never pick another GPU.
    index = cfg.crispasr_gpu_device if cfg.engine == "crispasr" else 0
    for device in devices:
        matches = device.uuid.casefold() == cfg.cuda_gpu_uuid.casefold() if cfg.cuda_gpu_uuid else device.index == index
        if matches:
            return device
    raise GPUSelectionError("選択したGPUが見つかりません。GPUを選び直してください。別のGPUへの自動切替は行いません")


def require_free_vram(device: NvidiaGPU, required_mib: int) -> None:
    if device.free_mib < required_mib:
        raise GPUSelectionError(
            f"{device.name}: 空きVRAM {device.free_mib / 1024:.1f} GiB、"
            f"必要な空きの目安 {required_mib / 1024:.1f} GiB。ロードを停止しました。"
            "GPU使用量を減らすか、CPU・小さいモデルを選び、再読み込みしてください"
        )


# Conservative admission budgets, including inference buffers and headroom.
# These implementation budgets are estimates, not reservations or guarantees
# that every input will fit alongside other applications.
CRISPASR_VRAM_MIB = {
    "qwen3-1.7b-q8": 7168,
    "qwen3-1.7b-f16": 12288,
    "qwen3-0.6b-q8": 3584,
    "parakeet-ja-0.6b-q8": 2048,
    "parakeet-ja-0.6b-f16": 3072,
    "parakeet-ctc-ja-1.1b-q8": 4096,
}


def admit_crispasr(cfg: RecognitionConfig) -> NvidiaGPU:
    device = select_gpu(cfg)
    require_free_vram(device, CRISPASR_VRAM_MIB[cfg.crispasr_model])
    return device


def cuda_device_index(identity: str) -> int:
    """Map a physical UUID to the current process's CUDA ordinal (no context)."""
    class CUuuid(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    try:
        driver = ctypes.WinDLL(str(Path(os.environ["SystemRoot"]) / "System32/nvcuda.dll"))
        driver.cuInit.argtypes = [ctypes.c_uint]
        driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        driver.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        driver.cuDeviceGetUuid.argtypes = [ctypes.POINTER(CUuuid), ctypes.c_int]

        def check(code):
            if code:
                raise GPUSelectionError(f"CUDAのGPU照合に失敗しました（コード {code}）。ロードを停止しました")

        check(driver.cuInit(0))
        count = ctypes.c_int()
        check(driver.cuDeviceGetCount(ctypes.byref(count)))
        for ordinal in range(count.value):
            device, raw = ctypes.c_int(), CUuuid()
            check(driver.cuDeviceGet(ctypes.byref(device), ordinal))
            check(driver.cuDeviceGetUuid(ctypes.byref(raw), device.value))
            if ("GPU-" + str(uuid.UUID(bytes=bytes(raw.bytes)))).casefold() == identity.casefold():
                return ordinal
    except (OSError, AttributeError, KeyError) as exc:
        raise GPUSelectionError("CUDAのGPU照合ができないためロードを停止しました") from exc
    raise GPUSelectionError("選択GPUがこのプロセスのCUDAから見えません。起動時のGPU制限を確認してください")
