#
# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Repairs wheels for dynamically linked libraries on all platforms to ensure broad compatibility.
If this is not able to achieve the broken wheel is deleted and not published to Espressif's PyPI.
See: https://github.com/espressif/idf-python-wheels/blob/main/README.md#universal-wheel-tag---linking-of-dynamic-libraries
- Windows: delvewheel (bundles DLLs)
- macOS: delocate (bundles dylibs)
- Linux: auditwheel (bundles SOs)
"""

import platform
import subprocess

from pathlib import Path
from typing import Union

from colorama import Fore

from _helper_functions import print_color


def get_platform() -> str:
    return platform.system()


def is_pure_python_wheel(wheel_name: str) -> bool:
    return "py3-none-any" in wheel_name


def is_platform_wheel(wheel_name: str, target_platform: str, current_arch: Union[str, None] = None) -> bool:
    """Check if wheel is for the target platform and architecture."""
    if target_platform == "Windows":
        return "win" in wheel_name
    elif target_platform in ["Darwin", "macOS ARM", "macOS Intel"]:
        if "macosx" not in wheel_name:
            return False
        if target_platform == "macOS ARM" or (target_platform == "Darwin" and current_arch == "arm64"):
            return "arm64" in wheel_name or "universal2" in wheel_name
        elif target_platform == "macOS Intel" or (target_platform == "Darwin" and current_arch == "x86_64"):
            return "x86_64" in wheel_name or "universal2" in wheel_name
        return True  # If no specific arch check needed
    elif target_platform == "Linux":
        return "linux" in wheel_name
    return False


def get_wheel_arch(wheel_name: str) -> Union[str, None]:
    """Extract architecture from wheel filename."""
    # {name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl
    parts = wheel_name.replace(".whl", "").split("-")
    if len(parts) >= 5:
        platform_tag = parts[-1]
        for arch in ["x86_64", "aarch64", "armv7l"]:
            if arch in platform_tag:
                return arch
    return None


def repair_wheel_windows(wheel_path: Path, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    """Repair Windows wheel using delvewheel."""
    result = subprocess.run(
        ["delvewheel", "repair", str(wheel_path), "-w", str(temp_dir), "--no-mangle-all"],
        capture_output=True,
        text=True,
    )
    return result


def fix_universal2_wheel_name(wheel_path: Path, error_msg: str) -> Union[Path, str, None]:
    """Fix incorrectly tagged universal2 wheel by renaming to actual architecture.

    Returns:
        - New wheel path if successfully renamed
        - None if not a fixable case
        - "delete" if wheel is corrupted (missing all architectures) and should be deleted
    """
    if (
        "universal2" not in str(wheel_path)
        or "Failed to find any binary with the required architecture" not in error_msg
    ):
        return None

    # Parse which architecture is missing from the error
    # "Failed to find any binary with the required architecture: 'x86_64'"
    # means the wheel only has arm64
    if "'arm64,x86_64'" in error_msg or "'x86_64,arm64'" in error_msg:
        # Missing BOTH architectures - wheel is corrupted, delete it
        print_color("  -> Deleting corrupted wheel (missing native binaries for all architectures)", Fore.RED)
        wheel_path.unlink()
        return "delete"
    elif "'x86_64'" in error_msg:
        # Missing x86_64, so it only has arm64
        actual_arch = "arm64"
    elif "'arm64'" in error_msg:
        # Missing arm64, so it only has x86_64
        actual_arch = "x86_64"
    else:
        return None

    # Rename the wheel
    new_name = str(wheel_path.name).replace("_universal2.whl", f"_{actual_arch}.whl")
    new_path = wheel_path.parent / new_name

    print(f"  -> Renaming: {wheel_path.name} -> {new_name}")
    wheel_path.rename(new_path)

    return new_path


def repair_wheel_macos(wheel_path: Path, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    """Repair macOS wheel using delocate."""
    cmd = ["delocate-wheel", "-w", str(temp_dir), "-v", str(wheel_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def repair_wheel_linux(wheel_path: Path, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    """Repair Linux wheel using auditwheel.

    Uses --strip option to strip debugging symbols which can help with
    ELF alignment issues on ARM (fixes "ELF load command address/offset not properly aligned" errors).
    """
    result = subprocess.run(
        ["auditwheel", "repair", str(wheel_path), "-w", str(temp_dir), "--strip"],
        capture_output=True,
        text=True,
    )
    return result


def main() -> None:
    wheels_dir: Path = Path("./downloaded_wheels")
    temp_dir: Path = Path("./temp_repair")
    temp_dir.mkdir(exist_ok=True)

    # Find all wheel files
    wheels: list[Path] = list(wheels_dir.rglob("*.whl"))

    if not wheels:
        print_color(f"No wheels found in {wheels_dir}", Fore.RED)
        raise SystemExit("No wheels found in downloaded_wheels directory")

    print_color(f"Found {len(wheels)} wheels\n")

    current_platform: str = get_platform()
    current_arch: str = platform.machine()

    repaired_count: int = 0
    skipped_count: int = 0
    deleted_count: int = 0
    error_count: int = 0
    errors: list[str] = []

    # Repair each wheel
    for wheel in wheels:
        print(f"Processing: {wheel.name}")

        # Skip pure Python wheels
        if is_pure_python_wheel(wheel.name):
            print_color("  -> Skipping pure Python wheel")
            skipped_count += 1
            continue

        # Skip pywin32 wheels on Windows (DLLs are internal to the wheel)
        if current_platform == "Windows" and wheel.name.startswith("pywin32"):
            print_color("  -> Skipping pywin32 wheel (DLLs are internal)")
            skipped_count += 1
            continue

        # Skip wheels not for the workflow platform
        if not is_platform_wheel(wheel.name, current_platform, current_arch):
            print_color(f"  -> Skipping (not a {current_platform} wheel)")
            skipped_count += 1
            continue

        # For Linux, skip wheels for different architectures
        if current_platform == "Linux":
            wheel_arch = get_wheel_arch(wheel.name)
            if wheel_arch and wheel_arch != current_arch:
                print_color(f"  -> Skipping incompatible architecture ({wheel_arch} wheel on {current_arch} platform)")
                skipped_count += 1
                continue

        # Clean temp directory
        for old_wheel in temp_dir.glob("*.whl"):
            old_wheel.unlink()

        # Repair wheel using platform-specific tool
        if current_platform == "Windows":
            result = repair_wheel_windows(wheel, temp_dir)
        elif current_platform == "Darwin":
            result = repair_wheel_macos(wheel, temp_dir)
        elif current_platform == "Linux":
            result = repair_wheel_linux(wheel, temp_dir)
        else:
            print_color(f"  -> ERROR: Unsupported platform {current_platform}", Fore.RED)
            error_count += 1
            continue

        if result.stdout:
            print(f"  {result.stdout.strip()}")
        if result.stderr:
            print_color(f"  {result.stderr.strip()}", Fore.RED)

        # Check for errors
        error_msg = result.stderr.strip() if result.stderr else ""

        # Special handling for incorrectly tagged universal2 wheels on macOS
        if (
            current_platform == "Darwin"
            and "universal2" in wheel.name
            and "Failed to find any binary with the required architecture" in error_msg
        ):
            # Try to fix by renaming the wheel to the correct architecture
            renamed_wheel = fix_universal2_wheel_name(wheel, error_msg)

            if renamed_wheel == "delete":
                # Wheel was corrupted and has been deleted
                deleted_count += 1
                continue
            elif renamed_wheel:
                # Clean temp directory and retry with renamed wheel
                for old_wheel in temp_dir.glob("*.whl"):
                    old_wheel.unlink()

                print_color("  -> Retrying delocate with corrected wheel name", Fore.CYAN)
                result = repair_wheel_macos(Path(renamed_wheel), temp_dir)

                if result.stdout:
                    print(f"  {result.stdout.strip()}")
                if result.stderr:
                    print_color(f"  {result.stderr.strip()}", Fore.RED)

                # Update wheel reference and error message for subsequent checks
                wheel = Path(renamed_wheel)
                error_msg = result.stderr.strip() if result.stderr else ""

        # Special handling forLinux ARMv7 broken wheels
        if (
            current_platform == "Linux"
            and current_arch == "armv7l"
            and "This does not look like a platform wheel, no ELF executable" in error_msg
        ):
            print_color("  -> Deleting corrupted wheel", Fore.RED)
            wheel.unlink()
            deleted_count += 1
            continue

        # Check for non-critical errors (keep original wheel)
        is_noncritical = (
            "too-recent versioned symbols" in error_msg
            # manylinux wheel can't find its libraries
            # it means it was already properly repaired
            or ("manylinux" in wheel.name and "could not be located" in error_msg)
        )

        has_error = (
            any(
                [
                    "ValueError:" in error_msg,
                    "FileNotFoundError:" in error_msg,
                    "Cannot repair wheel" in error_msg,
                    "could not be located" in error_msg,
                    "DelocationError:" in error_msg,
                ]
            )
            and not is_noncritical
        )

        if is_noncritical:
            # Non-critical error - keep the wheel
            if "too-recent versioned symbols" in error_msg:
                print_color("  -> Keeping original wheel (build issue: needs older toolchain)", Fore.YELLOW)
            elif "manylinux" in wheel.name and "could not be located" in error_msg:
                print_color("  -> Keeping original wheel (already bundled from PyPI)", Fore.GREEN)
            skipped_count += 1
        elif has_error:
            # Actual error occurred (even if a wheel was created, it may be broken)
            # Clean up any partial wheel
            for old_wheel in temp_dir.glob("*.whl"):
                old_wheel.unlink()
            print_color(f"  -> ERROR: {error_msg}", Fore.RED)
            errors.append(f"{wheel.name}: {error_msg}")
            error_count += 1
        else:
            # Check if a repaired wheel was created
            repaired = next(temp_dir.glob("*.whl"), None)

            if repaired:
                # A repaired wheel was created successfully
                if repaired.name != wheel.name:
                    wheel.unlink()  # Remove original
                    repaired.rename(wheel.parent / repaired.name)
                    print_color(f"  -> Replaced with repaired wheel: {repaired.name}", Fore.GREEN)
                else:
                    # Name unchanged
                    wheel.unlink()
                    repaired.rename(wheel)
                    print_color(f"  -> Repaired successfully: {repaired.name}", Fore.GREEN)
                repaired_count += 1
            elif result.returncode == 0:
                # No repaired wheel created, but command succeeded (already compatible)
                print_color("  -> Keeping original wheel (already compatible)", Fore.GREEN)
                skipped_count += 1
            else:
                # Command failed and no wheel created
                print_color(f"  -> ERROR: {error_msg}", Fore.RED)
                errors.append(f"{wheel.name}: {error_msg}")
                error_count += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Total wheels: {len(wheels)}")
    print_color(f"Deleted wheels: {deleted_count}", Fore.RED)
    print_color(f"Kept wheels: {skipped_count}")
    print_color(f"Repaired wheels: {repaired_count}", Fore.GREEN)
    print_color(f"Errors: {error_count}", Fore.RED)

    if errors:
        print_color("---------- ERRORS ----------", Fore.RED)
        for i, error in enumerate(errors, start=1):
            print_color(f"{i}. {error}", Fore.RED)
        raise SystemExit("One or more wheels failed to repair")

    print("All wheels processed successfully")


if __name__ == "__main__":
    main()
