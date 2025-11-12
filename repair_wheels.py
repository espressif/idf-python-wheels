#
# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Repairs Linux wheels using auditwheel to ensure manylinux compatibility.
"""

import platform
import subprocess

from pathlib import Path

from colorama import Fore

from _helper_functions import print_color


def get_wheel_arch(wheel_name):
    """Extract architecture from wheel filename."""
    # {name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl
    parts = wheel_name.replace(".whl", "").split("-")
    if len(parts) >= 5:
        platform_tag = parts[-1]
        for arch in ["x86_64", "aarch64", "armv7l", "i686"]:
            if arch in platform_tag:
                return arch
    return None


def main():
    wheels_dir = Path("./downloaded_wheels")
    temp_dir = Path("./temp_repair")
    temp_dir.mkdir(exist_ok=True)

    # Find all wheel files
    wheels = list(wheels_dir.rglob("*.whl"))

    if not wheels:
        print(f"No wheels found in {wheels_dir}")
        raise SystemExit("No wheels found in downloaded_wheels directory")

    print_color(f"Found {len(wheels)} wheels\n")

    repaired_count = 0
    skipped_count = 0
    error_count = 0
    errors = []

    # Repair each wheel
    for wheel in wheels:
        print(f"Processing: {wheel.name}")

        if "py3-none-any" in wheel.name:
            print_color("  → Skipping pure Python wheel")
            skipped_count += 1
            continue

        # Skip wheels for different architectures
        current_arch = platform.machine()
        wheel_arch = get_wheel_arch(wheel.name)
        if wheel_arch and wheel_arch != current_arch:
            print_color(f"  → Skipping incompatible architecture ({wheel_arch} wheel on {current_arch} platform)")
            skipped_count += 1
            continue

        # Clean temp directory
        for old_wheel in temp_dir.glob("*.whl"):
            old_wheel.unlink()

        # auditwheel repair automatically:
        # - Detects if wheel needs repair
        # - Bundles required libraries
        # - Sets appropriate manylinux tag
        result = subprocess.run(
            ["auditwheel", "repair", str(wheel), "-w", str(temp_dir)], capture_output=True, text=True
        )

        # auditwheel output
        if result.stdout:
            print(f"  {result.stdout.strip()}")
        if result.stderr:
            if not result.stderr.strip().startswith("INFO"):
                errors.append(f"{wheel.name}: {result.stderr.strip()}")
                error_count += 1
                print_color(f"  → ERROR: {result.stderr.strip()}", Fore.RED)
            else:
                print_color(f"  {result.stderr.strip()}")

        # Check if a repaired wheel was created
        repaired = next(temp_dir.glob("*.whl"), None)

        if repaired and repaired.name != wheel.name:
            # A new wheel was created with a different name (repaired)
            repaired.rename(wheel.parent / repaired.name)
            wheel.unlink()  # Remove original
            print_color(f"  → Replaced with repaired wheel: {repaired.name}\n", Fore.GREEN)
            repaired_count += 1
        else:
            print_color("  → Keeping original wheel", Fore.GREEN)
            skipped_count += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Total wheels: {len(wheels)}")
    print_color(f"Kept wheels: {skipped_count}")
    print_color(f"Repaired wheels: {repaired_count}", Fore.GREEN)
    print_color(f"Errors: {error_count}", Fore.RED)

    if errors:
        print_color("---------- ERRORS ----------", Fore.RED)
        for i, error in enumerate(errors, start=1):
            print_color(f"  • {i}. {error}", Fore.RED)
        raise SystemExit("One or more wheels failed to repair")

    print("All wheels processed successfully")


if __name__ == "__main__":
    main()
