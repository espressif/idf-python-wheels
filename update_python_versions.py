# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
"""
Script to update README.md and pyproject.toml with dynamically detected supported Python versions.
"""

import json
import re


def update_python_versions():
    """Update Python versions in README.md and pyproject.toml based on supported_versions.json"""

    try:
        with open("supported_versions.json", "r") as f:
            versions_data = json.load(f)
    except FileNotFoundError:
        raise SystemExit("Error: supported_versions.json not found. Run supported_versions.py first.")

    supported_python = versions_data.get("supported_python", [])
    if not supported_python:
        raise SystemExit("Error: No supported Python versions found in JSON.")

    oldest_python = versions_data.get("oldest_supported_python", "")
    if not oldest_python:
        raise SystemExit("Error: No oldest supported Python version found in JSON.")

    changes_made = []

    # Update README.md
    try:
        with open("README.md", "r") as f:
            readme_content = f.read()
    except FileNotFoundError:
        raise SystemExit("Error: README.md not found.")

    new_python_section = []
    for version in supported_python:
        new_python_section.append(f"* {version}")
    new_python_lines = "\n".join(new_python_section)

    # Use regex to find and replace the Python versions section
    pattern = r"(Supported Python versions:\n)((?:\* \d+\.\d+\n)*)"
    replacement = f"\\1{new_python_lines}\n"
    new_readme = re.sub(pattern, replacement, readme_content)

    if new_readme != readme_content:
        with open("README.md", "w") as f:
            f.write(new_readme)
        changes_made.append("README.md")

    # Update pyproject.toml
    try:
        with open("pyproject.toml", "r") as f:
            pyproject_content = f.read()
    except FileNotFoundError:
        raise SystemExit("Error: pyproject.toml not found.")

    # Update ruff target-version (e.g., "py38")
    py_version_compact = f"py{oldest_python.replace('.', '')}"
    ruff_pattern = r'(target-version\s*=\s*["\'])py\d+(["\'])'
    new_pyproject = re.sub(ruff_pattern, f"\\g<1>{py_version_compact}\\g<2>", pyproject_content)

    # Update mypy python_version (e.g., "3.9")
    mypy_pattern = r'(python_version\s*=\s*["\'])\d+\.\d+(["\'])'
    new_pyproject = re.sub(mypy_pattern, f"\\g<1>{oldest_python}\\g<2>", new_pyproject)

    if new_pyproject != pyproject_content:
        with open("pyproject.toml", "w") as f:
            f.write(new_pyproject)
        changes_made.append("pyproject.toml")

    if not changes_made:
        print("ℹ️  No changes needed - Python versions are already up to date.")
        return False

    files_updated = " and ".join(changes_made)
    print(f"✅ Updated {files_updated}")
    print(f"   Supported versions: {', '.join(supported_python)}")
    print(f"   Oldest supported Python version: {oldest_python}")
    return True


update_python_versions()
