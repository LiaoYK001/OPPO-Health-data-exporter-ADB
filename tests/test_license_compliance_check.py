from scripts.license_compliance_check import PYTHON_LICENSE_MARKERS, _license_matches


def test_numpy_accepts_spdx_license_expression() -> None:
    assert _license_matches("BSD-3-Clause", PYTHON_LICENSE_MARKERS["numpy"])


def test_license_matching_is_case_insensitive() -> None:
    assert _license_matches("License-Expression: MIT", ("mit",))


def test_unreviewed_license_is_rejected() -> None:
    assert not _license_matches("GPL-3.0-only", PYTHON_LICENSE_MARKERS["numpy"])
