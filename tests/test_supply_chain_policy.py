"""Regression tests for repository supply-chain controls."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (ROOT, ROOT / "macos" / "backend")
BUILD_AUDIT = ROOT / "security" / "build-audit"
FULL_SHA_ACTION = re.compile(r"^\s*-?\s*uses:\s*[^\s@]+@[0-9a-f]{40}(?:\s*#.*)?$")
APPROVED_ACTION_REFS = {
    "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
    "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
}


def _load_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_uv_policy_is_fail_closed_for_each_python_project() -> None:
    mise = _load_toml(ROOT / ".mise.toml")
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )

    assert mise["tools"]["uv"] == "0.11.32"
    setup_uv_steps = workflow.count("uses: astral-sh/setup-uv@")
    assert setup_uv_steps > 0
    assert workflow.count('version: "0.11.32"') == setup_uv_steps

    for project in PROJECTS:
        config = _load_toml(project / "pyproject.toml")
        uv = config["tool"]["uv"]

        assert uv["required-version"] == "==0.11.32"
        assert uv["preview-features"] == [
            "audit-command",
            "json-output",
            "malware-check",
        ]
        assert uv["exclude-newer"] == "1 week"
        assert uv["audit"]["malware-check"] is True
        assert uv["build-constraint-dependencies"] == ["hatchling==1.31.0"]
        assert config["build-system"]["requires"] == ["hatchling==1.31.0"]
        assert (project / "uv.lock").is_file()

    build_audit = _load_toml(BUILD_AUDIT / "pyproject.toml")
    assert build_audit["project"]["dependencies"] == ["hatchling==1.31.0"]
    assert build_audit["tool"]["uv"]["package"] is False
    assert build_audit["tool"]["uv"]["required-version"] == "==0.11.32"
    assert build_audit["tool"]["uv"]["audit"]["malware-check"] is True
    assert (BUILD_AUDIT / "uv.lock").is_file()


def test_dependabot_delays_new_uv_releases_for_both_projects() -> None:
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text(
        encoding="utf-8"
    )

    assert dependabot.count('package-ecosystem: "uv"') == 3
    assert 'directory: "/"' in dependabot
    assert 'directory: "/macos/backend"' in dependabot
    assert 'directory: "/security/build-audit"' in dependabot
    assert dependabot.count("default-days: 7") == 3
    assert 'package-ecosystem: "github-actions"' in dependabot


def test_audit_exceptions_are_owned_explainable_and_unexpired() -> None:
    project = _load_toml(ROOT / "pyproject.toml")
    policy = _load_toml(ROOT / "security" / "audit-exceptions.toml")
    audit = project["tool"]["uv"]["audit"]

    configured_ignore = set(audit.get("ignore", []))
    configured_until_fixed = set(audit.get("ignore-until-fixed", []))
    exceptions = policy["exceptions"]
    exception_ids = {item["id"] for item in exceptions}
    root_lock = _load_toml(ROOT / "uv.lock")
    locked_versions: dict[str, set[str]] = {}
    for package in root_lock["package"]:
        locked_versions.setdefault(package["name"], set()).add(package["version"])

    assert type(policy["schema_version"]) is int
    assert policy["schema_version"] == 1
    assert len(exception_ids) == len(exceptions)
    assert configured_ignore.isdisjoint(configured_until_fixed)
    assert exception_ids == configured_ignore | configured_until_fixed

    for item in exceptions:
        assert isinstance(item["package"], str) and item["package"]
        assert item["owner"].startswith("@")
        assert len(item["reason"]) >= 40
        assert set(item["profiles"]) == {
            "qwen3",
            "qwen3-cuda",
            "macos-python-mlx",
        }
        versions = item["locked_versions"]
        assert isinstance(versions, list) and versions
        assert all(isinstance(version, str) and version for version in versions)
        assert set(versions) <= locked_versions[item["package"]]
        assert item["expires"] > date.today(), f"audit exception expired: {item['id']}"
        if item["disposition"] == "ignore_until_fixed":
            assert item["id"] in configured_until_fixed
            assert item["fix_available"] is False
        else:
            assert item["disposition"] == "temporarily_accepted"
            assert item["id"] in configured_ignore
            assert item["fix_available"] is True

    shipped_python = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py")
    )
    assert "torch.jit.script" not in shipped_python
    assert "torch.package" not in shipped_python
    assert "PackageImporter" not in shipped_python


def test_hashed_build_closure_matches_the_audited_lock() -> None:
    constraints_text = (ROOT / "security" / "build-constraints.txt").read_text(
        encoding="utf-8"
    )
    constraints = dict(
        re.findall(r"^([a-z0-9][a-z0-9-]*)==([^\s\\]+)", constraints_text, re.MULTILINE)
    )
    audit_lock = _load_toml(BUILD_AUDIT / "uv.lock")
    audited = {
        package["name"]: package["version"]
        for package in audit_lock["package"]
        if package["name"] in constraints
    }

    assert constraints
    assert audited == constraints
    assert constraints_text.count("--hash=sha256:") >= len(constraints)


def test_github_actions_are_immutable_and_least_privilege() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )
    uses_lines = [line for line in workflow.splitlines() if "uses:" in line]

    assert uses_lines
    assert all(FULL_SHA_ACTION.match(line) for line in uses_lines)
    for line in uses_lines:
        action_ref = line.split("uses:", 1)[1].split("#", 1)[0].strip()
        action, revision = action_ref.rsplit("@", 1)
        assert action in APPROVED_ACTION_REFS
        assert revision == APPROVED_ACTION_REFS[action]
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "uv sync --locked" in workflow
    assert "uv run --locked" in workflow
    assert "--no-project" not in workflow
    assert "--with " not in workflow
    assert workflow.count("--require-hashes") >= 4
    assert "security/build-constraints.txt" in workflow
    assert "uv sync --project macos/backend --locked --extra mlx --extra dev" in workflow
    assert '--require-hashes -r "$RUNNER_TEMP/requirements-mlx.txt"' in workflow
    assert "project: security/build-audit" in workflow


def test_bootstrap_docs_do_not_pipe_network_content_to_a_shell() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()

    assert "irm https://" not in readme
    assert "curl https://mise.run | sh" not in readme


def test_launch_and_macos_gate_use_the_lockfile() -> None:
    launcher = (ROOT / "start.vbs").read_text(encoding="utf-8")
    phase0 = (ROOT / "macos" / "scripts" / "phase0_asr_gate.sh").read_text(
        encoding="utf-8"
    )

    assert "uv run --locked zen-whisper" in launcher
    assert "uv sync --locked" in phase0
    assert "mise exec -- uv run \\" in phase0
    assert "\n  --locked \\" in phase0
    assert "--with " not in phase0
    assert "_pinned_model_snapshot" in phase0
    assert "path_or_hf_repo=whisper_path" in phase0
    assert "load(qwen_path)" in phase0


def test_distribution_security_manifests_are_forced_into_wheels() -> None:
    root = _load_toml(ROOT / "pyproject.toml")
    backend = _load_toml(ROOT / "macos" / "backend" / "pyproject.toml")

    assert root["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"][
        "src/model_manifest.json"
    ] == "src/model_manifest.json"
    assert backend["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]["src/zen_whisper_mac_backend/resources/model_registry.json"] == (
        "zen_whisper_mac_backend/resources/model_registry.json"
    )


def test_macos_bundle_build_and_install_require_hashes() -> None:
    build = (ROOT / "macos" / "scripts" / "install_app.sh").read_text(
        encoding="utf-8"
    )
    install = (
        ROOT / "macos" / "scripts" / "install_backend_from_app.sh"
    ).read_text(encoding="utf-8")

    assert "uv export --project \"$BACKEND_DIR\" --locked" in build
    assert "--no-hashes" not in build
    assert "--build-constraint \"$REPO_ROOT/security/build-constraints.txt\"" in build
    assert "--require-hashes" in build
    assert "pip install --no-config" in install
    assert '--require-hashes -r "$REQUIREMENTS"' in install
    assert '--no-deps "$WHEEL"' in install
