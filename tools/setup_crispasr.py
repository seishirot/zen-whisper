"""Explicit, pinned Windows CrispASR setup. Never imported by the application."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "src" / "crispasr_manifest.json"
DEFAULT_ROOT = REPO_ROOT / "tools" / "bin" / "crispasr" / "v0.8.32"


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_file(path: Path, spec: dict) -> None:
    if path.stat().st_size != spec["size"] or sha256_file(path) != spec["sha256"]:
        raise ValueError(f"Integrity check failed: {path.name}")


def download(url: str, target: Path, spec: dict) -> None:
    if target.exists():
        verify_file(target, spec)
        print(f"Verified existing {target.name}", flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    print(f"Downloading {target.name} ({spec['size'] / 1e9:.2f} GB)", flush=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ZenWhisper-CrispASR-setup"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        verify_file(partial, spec)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def extract_archive(archive: Path, destination: Path) -> None:
    """Validate every member before extraction; preserve changed local installs."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="crispasr-setup-") as temp:
        staging = Path(temp)
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                parts = PurePosixPath(entry.filename.replace("\\", "/"))
                if parts.is_absolute() or ".." in parts.parts or ":" in entry.filename:
                    raise ValueError("Unsafe archive member")
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Archive symlinks are not supported")
            source.extractall(staging)
        files = [p for p in staging.rglob("*") if p.is_file()]
        for source in files:
            target = destination / source.relative_to(staging)
            if target.exists() and sha256_file(target) != sha256_file(source):
                raise FileExistsError(f"Changed installed file: {target.name}; choose a new --root")
        for source in files:
            target = destination / source.relative_to(staging)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source, target)


def signatures(directory: Path) -> dict[str, str]:
    """Record Authenticode status without installing certificates or changing policy."""
    if os.name != "nt":
        return {}
    shell = Path(shutil.which("pwsh") or str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"))
    # Path is passed as data through a process-local environment variable.
    script = (
        "$ErrorActionPreference = 'Stop'; $items = @(Get-ChildItem -LiteralPath $env:ZEN_CRISP_SIGNATURE_ROOT -Recurse -File | "
        "Where-Object { $_.Extension -in '.exe','.dll' } | ForEach-Object { "
        "$sig = Get-AuthenticodeSignature -LiteralPath $_.FullName; "
        "@{ name = $_.Name; status = [string]$sig.Status } }); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    env = dict(os.environ, ZEN_CRISP_SIGNATURE_ROOT=str(directory))
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, capture_output=True, check=True, timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    items = json.loads(result.stdout)
    if any(not item.get("status") for item in items):
        raise RuntimeError("Authenticode returned no status")
    return {item["name"]: item["status"] for item in items}


def pe_imports(path: Path) -> list[str]:
    """Read normal and delay-load PE imports, without loading the binary."""
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Not a PE binary")
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError("Unsupported PE optional header")
    directories = optional + (112 if magic == 0x20B else 96)
    image_base = struct.unpack_from("<Q" if magic == 0x20B else "<I", data, optional + (24 if magic == 0x20B else 28))[0]
    sections = optional + struct.unpack_from("<H", data, pe + 20)[0]
    count = struct.unpack_from("<H", data, pe + 6)[0]

    def offset(rva):
        for i in range(count):
            start = sections + i * 40
            virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", data, start + 8)
            if address <= rva < address + max(virtual_size, raw_size):
                return raw_offset + rva - address
        raise ValueError("PE RVA outside sections")

    imports = set()
    for index, length, name_offset in ((1, 20, 12), (13, 32, 4)):
        rva, size = struct.unpack_from("<II", data, directories + index * 8)
        if not rva:
            continue
        table = offset(rva)
        for position in range(table, table + size, length):
            if not any(data[position:position + length]):
                break
            name = struct.unpack_from("<I", data, position + name_offset)[0]
            if index == 13 and not struct.unpack_from("<I", data, position)[0] & 1:
                name -= image_base
            start = offset(name)
            imports.add(data[start:data.index(b"\0", start)].decode("ascii"))
    return sorted(imports)


def setup_runtime(device: str, root: Path, manifest: dict) -> None:
    spec = manifest["runtimes"][device]
    url = f"{manifest['source']}/releases/download/v{manifest['version']}/{spec['asset']}"
    archive = REPO_ROOT / "tools" / "downloads" / "crispasr-v0.8.32" / spec["asset"]
    download(url, archive, spec)
    destination = root / device
    extract_archive(archive, destination)
    signed = signatures(destination)
    files = {
        path.relative_to(destination).as_posix(): {
            "sha256": sha256_file(path), "size": path.stat().st_size,
            "imports": pe_imports(path) if path.suffix in (".exe", ".dll") else [],
            "signature": signed.get(path.name, "not-applicable" if path.suffix not in (".exe", ".dll") else "not-checked"),
        }
        for path in sorted(destination.rglob("*")) if path.is_file() and path.name not in ("receipt.json", "receipt.json.tmp")
    }
    executable = destination / spec["asset"].removesuffix(".zip") / "crispasr.exe"
    result = subprocess.run(
        [str(executable), "--version"], cwd=executable.parent,
        capture_output=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    build_info = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if manifest["version"] not in build_info or manifest["commit"][:8] not in build_info:
        raise ValueError("Runtime build identity does not match manifest")
    if device == "cpu" and any(
        any(name.lower().startswith(("cuda", "cublas", "cudnn", "nvcuda", "ggml-cuda")) for name in item["imports"])
        for item in files.values()
    ):
        raise ValueError("CPU runtime has a NVIDIA dependency")
    receipt = {
        "build_info": build_info,
        "version": manifest["version"], "commit": manifest["commit"], "device": device,
        "source": url, "archive_sha256": spec["sha256"], "license": manifest["license"],
        "files": files,
    }
    temporary_receipt = destination / "receipt.json.tmp"
    temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    temporary_receipt.replace(destination / "receipt.json")
    print(f"Installed {device}; {len(files)} files verified; signatures recorded", flush=True)


def setup_model(profile: str, root: Path, manifest: dict) -> None:
    spec = manifest["models"][profile]
    url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{spec['file']}"
    download(url, root / "models" / spec["file"], spec)


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--runtime", action="append", choices=manifest["runtimes"], default=[])
    parser.add_argument("--model", action="append", choices=manifest["models"], default=[])
    args = parser.parse_args()
    if not args.runtime and not args.model:
        parser.error("Select at least one --runtime or --model; no implicit downloads")
    for device in args.runtime:
        setup_runtime(device, args.root.resolve(), manifest)
    for profile in args.model:
        setup_model(profile, args.root.resolve(), manifest)


if __name__ == "__main__":
    main()
