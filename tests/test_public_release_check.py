from __future__ import annotations

from scripts.public_release_check import (
    _configure_utf8_output,
    _github_escape,
    _sensitive_path,
    _text_rules,
)


def _rules():
    return {name: pattern for name, pattern in _text_rules()}


def test_sensitive_release_paths_are_rejected() -> None:
    assert _sensitive_path("data/health.sqlite3")
    assert _sensitive_path("somewhere/export.xlsx")
    assert _sensitive_path("private/web-auth.json")
    assert not _sensitive_path("health_exporter/db.py")


def test_identity_and_credential_patterns_are_detected() -> None:
    rules = _rules()
    home_path = "C:" + "\\Users\\" + "person\\project"
    email = "person" + "@example.invalid"
    token = "ghp_" + "A" * 24
    hardware_address = "AA" + ":BB:CC:DD:EE:FF"

    assert rules["windows-user-home"].search(home_path)
    assert rules["email-address"].search(email)
    assert rules["github-token"].search(token)
    assert rules["hardware-address"].search(hardware_address)


def test_placeholders_do_not_trigger_identity_rules() -> None:
    rules = _rules()

    assert not rules["windows-user-home"].search("C:\\path\\to\\project")
    assert not rules["email-address"].search("GitHub noreply address")


def test_github_annotation_values_are_command_escaped() -> None:
    assert _github_escape("path%name\r\nnext") == "path%25name%0D%0Anext"


def test_public_checker_configures_utf8_output_when_supported() -> None:
    class Stream:
        settings: dict[str, str] = {}

        def reconfigure(self, **settings: str) -> None:
            self.settings = settings

    stream = Stream()
    _configure_utf8_output([stream])  # type: ignore[list-item]

    assert stream.settings == {"encoding": "utf-8", "errors": "replace"}
