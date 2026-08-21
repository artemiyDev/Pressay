"""Guard against the install scripts drifting from pyproject.toml.

The dependency list is intentionally duplicated: `pyproject.toml` is the
package metadata, while `scripts/setup.ps1` installs a literal `pip install`
list instead of `pip install -e .` (the project may sit on a read-mostly
mount, see the comment above that call in `setup.ps1`). Nothing here runs an
install script; everything is plain text parsing so it works on every CI
platform.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project requires >=3.11
    raise RuntimeError("tomllib requires Python 3.11+")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def _split_marker(spec: str) -> tuple[str, str | None]:
    """Split a PEP 508 specifier into its base spec and environment marker."""

    if ";" in spec:
        base, marker = spec.split(";", 1)
        return base.strip(), marker.strip()
    return spec.strip(), None


def _load_pyproject_dependencies() -> tuple[list[str], list[str], list[str]]:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    dependencies = project["dependencies"]
    optional = project["optional-dependencies"]
    return dependencies, optional["dev"], optional["macos"]


def _load_pyproject_version() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    return data["project"]["version"]


def _extract_single_match(*, path: Path, pattern: str, label: str) -> str:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(pattern, text, flags=re.MULTILINE)
    assert len(matches) == 1, (
        f"Expected exactly one {label} in {path.relative_to(PROJECT_ROOT)}, "
        f"found {len(matches)}."
    )
    return matches[0]


def _applicable_base_specs(specs: list[str], *, excluded_platform: str) -> set[str]:
    """Base specifiers that install on every platform except ``excluded_platform``.

    A spec with no marker applies everywhere. A spec whose marker names
    ``excluded_platform`` is dropped; any other marker (including the target
    platform) is kept, with the marker itself stripped.
    """

    result: set[str] = set()
    for spec in specs:
        base, marker = _split_marker(spec)
        if marker and excluded_platform.lower() in marker.lower():
            continue
        result.add(base)
    return result


def _assert_same_specs(
    actual: set[str],
    expected: set[str],
    *,
    script_name: str,
    expected_description: str,
) -> None:
    missing = expected - actual
    extra = actual - expected
    if not missing and not extra:
        return
    details = []
    if missing:
        details.append(
            f"missing from {script_name} (present in {expected_description}): "
            f"{sorted(missing)}"
        )
    if extra:
        details.append(
            f"present in {script_name} but not in {expected_description}: "
            f"{sorted(extra)}"
        )
    pytest.fail(f"{script_name} package list has drifted from pyproject.toml: " + "; ".join(details))


def _extract_setup_ps1_pip_specs() -> list[str]:
    """Pull Windows runtime specs from the installer contract function.

    `setup.ps1` consumes this function directly, so the contract has one
    source of truth while this test still checks it against pyproject.toml.
    """

    text = (SCRIPTS / "install-layout.ps1").read_text(encoding="utf-8")
    match = re.search(
        r"function Get-PressayWindowsRuntimeDependencySpecs\s*\{"
        r"(.*?)"
        r"return \[string\[\]\]@\((.*?)\)\s*\}",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        pytest.fail(
            "Could not locate Get-PressayWindowsRuntimeDependencySpecs in "
            "scripts/install-layout.ps1."
        )
    specs = re.findall(r'"([^"]+)"', match.group(2))
    if not specs:
        pytest.fail(
            "No package specifiers were found in the Windows runtime contract."
        )
    return specs


def test_setup_ps1_pip_install_matches_pyproject_windows_and_dev_dependencies() -> None:
    dependencies, dev, _macos = _load_pyproject_dependencies()

    expected = _applicable_base_specs(dependencies, excluded_platform="Darwin")
    expected |= _applicable_base_specs(dev, excluded_platform="Darwin")

    actual = set(_extract_setup_ps1_pip_specs())

    _assert_same_specs(
        actual,
        expected,
        script_name="scripts/setup.ps1",
        expected_description="pyproject.toml [project.dependencies] (Windows-applicable) + [dev]",
    )


def test_setup_ps1_does_not_install_a_darwin_only_dependency() -> None:
    """A Windows-only script must never carry a macOS-marked package spec."""

    dependencies, _dev, macos = _load_pyproject_dependencies()
    darwin_only_bases = {
        base
        for spec in (*dependencies, *macos)
        for base, marker in [_split_marker(spec)]
        if marker and "darwin" in marker.lower()
    }
    actual = set(_extract_setup_ps1_pip_specs())
    leaked = actual & darwin_only_bases
    assert not leaked, (
        f"scripts/setup.ps1 installs macOS-only package(s) {sorted(leaked)}; "
        "these are marked `platform_system == 'Darwin'` in pyproject.toml and "
        "should not be present in the Windows install script."
    )


def test_setup_macos_script_installs_dependencies_from_pyproject_not_a_duplicated_list() -> None:
    """`setup-macos.sh` has no literal package/version list to drift.

    Unlike `setup.ps1`, it installs the project itself with the `macos`
    extra (`pip install "$project_root[macos]"`), so pyproject.toml stays
    the single source of truth and there is nothing here for a parity test
    to compare against. This test pins that approach: if a literal
    version list is ever reintroduced, add a parity check against
    pyproject.toml analogous to
    test_setup_ps1_pip_install_matches_pyproject_windows_and_dev_dependencies
    instead of letting the drift go unnoticed.
    """

    text = (SCRIPTS / "setup-macos.sh").read_text(encoding="utf-8")
    match = re.search(r'pip install\s+"[^"]*\[macos\]"', text)
    assert match is not None, (
        "scripts/setup-macos.sh no longer installs via the pyproject.toml "
        "'macos' extra. If it now hardcodes a package/version list, add a "
        "parity test against pyproject.toml here."
    )


def test_project_python_and_macos_bundle_versions_match() -> None:
    """All public version declarations must move together."""

    project_version = _load_pyproject_version()
    package_version = _extract_single_match(
        path=PROJECT_ROOT / "src" / "pressay" / "__init__.py",
        pattern=r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        label="__version__ assignment",
    )
    macos_bundle_version = _extract_single_match(
        path=SCRIPTS / "install-macos.sh",
        pattern=(
            r"<key>CFBundleShortVersionString</key>\s*"
            r"<string>([^<]+)</string>"
        ),
        label="CFBundleShortVersionString declaration",
    )

    assert package_version == project_version, (
        "src/pressay/__init__.py __version__ does not match "
        "pyproject.toml [project].version"
    )
    assert macos_bundle_version == project_version, (
        "scripts/install-macos.sh CFBundleShortVersionString does not match "
        "pyproject.toml [project].version"
    )
