"""Pre-push privacy gate rules, using synthetic data only."""
import json

from tools import check_publish_safety as gate


def test_emails_allow_only_no_reply_and_examples():
    assert gate.scan_text("Co-Authored-By: Claude <noreply@anthropic.com>", windows=set(), terms=[]) == []
    assert gate.scan_text("contact alice@example.com", windows=set(), terms=[]) == []
    assert gate.scan_text("mail taro.yamada@corp.co.jp", windows=set(), terms=[]) == ["email"]  # publish-safety: allow


def test_user_profile_paths_except_placeholders():
    assert gate.scan_text(r"C:\Users\taro\AppData\x", windows=set(), terms=[]) == ["user-path"]  # publish-safety: allow
    assert gate.scan_text("/home/hanako/.cache", windows=set(), terms=[]) == ["user-path"]  # publish-safety: allow
    assert gate.scan_text(r"C:\Users\<user>\AppData and %USERPROFILE%", windows=set(), terms=[]) == []


def test_gpu_uuid_placeholders_are_allowed():
    assert gate.scan_text("GPU-00000000-0000-0000-0000-000000000001", windows=set(), terms=[]) == []
    assert gate.scan_text("GPU-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d", windows=set(), terms=[]) == ["gpu-uuid"]  # publish-safety: allow


def test_allow_marker_exempts_a_line():
    line = "owner taro.yamada@corp.co.jp"  # publish-safety: allow
    assert gate.scan_text(line + "  # publish-safety: allow", windows=set(), terms=[]) == []
    assert gate.scan_text(line, windows=set(), terms=[]) == ["email"]


def test_deny_terms_match_whole_words_case_insensitively():
    assert gate.scan_text("built on MyDevBox today", windows=set(), terms=["mydevbox"]) == ["deny-list"]
    assert gate.scan_text("mydevboxes", windows=set(), terms=["mydevbox"]) == []


def test_private_reference_windows_detect_copied_japanese(tmp_path):
    corpus = tmp_path / "set" / "manifest.json"
    corpus.parent.mkdir()
    corpus.write_text(json.dumps({"items": [{"reference": "明日の会議では新しい予算案について話し合います。"}]},
                                 ensure_ascii=False), encoding="utf-8")
    windows = gate.private_windows(tmp_path)
    assert gate.scan_text("# 例: 新しい予算案について話し合います", windows=windows, terms=[]) == ["private-reference"]
    assert gate.scan_text("# 新しい機能を追加します", windows=windows, terms=[]) == []


def test_ascii_identifiers_in_references_are_not_private(tmp_path):
    corpus = tmp_path / "manifest.json"
    corpus.write_text(json.dumps({"items": [{"reference": "update the model manifest json file"}]}), encoding="utf-8")
    assert gate.private_windows(tmp_path) == set()


def test_forbidden_paths_and_synthetic_fixtures():
    assert gate.path_forbidden("tools/bench_outputs/private/report.md")
    assert gate.path_forbidden("config.toml")
    assert gate.path_forbidden("notes/recording.m4a")
    assert gate.path_forbidden("zen-whisper.log.1")
    assert not gate.path_forbidden("tools/samples/bench_sample_ja.wav")
    assert not gate.path_forbidden("src/asr/haas.py")


def test_pre_push_ranges_handle_new_branches_and_deletions():
    zero = gate.ZERO_SHA
    stdin = "\n".join([
        f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}",
        f"refs/heads/new {'c' * 40} refs/heads/new {zero}",
        f"(delete) {zero} refs/heads/old {'d' * 40}",
    ])
    assert gate.ranges_from_pre_push(stdin) == [f"{'b' * 40}..{'a' * 40}", f"{'c' * 40} --not --remotes"]
