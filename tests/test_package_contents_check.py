from scripts.package_contents_check import (
    REQUIRED_LICENSE_BASENAMES,
    findings_for_names,
)


def _valid_names() -> list[str]:
    return [
        "package/health_exporter/__init__.py",
        *(f"package/licenses/{name}" for name in REQUIRED_LICENSE_BASENAMES),
    ]


def test_package_contents_accepts_code_and_required_licenses() -> None:
    assert findings_for_names("release.whl", _valid_names()) == []


def test_package_contents_rejects_private_data_and_traversal() -> None:
    findings = findings_for_names(
        "release.whl",
        [
            *_valid_names(),
            "package/data/health.sqlite3",
            "../private/id_salt.bin",
            "package/document/chat-gemini-before.md",
        ],
    )

    assert any("sensitive-component" in finding for finding in findings)
    assert any("sensitive-suffix" in finding for finding in findings)
    assert any("unsafe-member-path" in finding for finding in findings)
    assert any("forbidden-private-document" in finding for finding in findings)


def test_package_contents_requires_every_license_file() -> None:
    names = _valid_names()
    missing = next(name for name in REQUIRED_LICENSE_BASENAMES if name != "LICENSE")
    names.remove(f"package/licenses/{missing}")

    findings = findings_for_names("release.tar.gz", names)

    assert findings == [f"release.tar.gz: missing-license-file: {missing}"]
