#
# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Repairs wheels for all platforms to ensure broad compatibility.
- Windows: delvewheel (bundles DLLs)
- macOS: delocate (bundles dylibs)
- Linux: auditwheel (bundles SOs)
"""

import platform
import subprocess

from pathlib import Path

from colorama import Fore

from _helper_functions import print_color


def get_platform():
    return platform.system()


def is_pure_python_wheel(wheel_name):
    return "py3-none-any" in wheel_name


def is_platform_wheel(wheel_name, target_platform, current_arch=None):
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


def repair_wheel_windows(wheel_path, temp_dir):
    """Repair Windows wheel using delvewheel."""
    result = subprocess.run(
        ["delvewheel", "repair", str(wheel_path), "-w", str(temp_dir), "--no-mangle-all"],
        capture_output=True,
        text=True,
    )
    return result


def fix_universal2_wheel_name(wheel_path, error_msg):
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


def repair_wheel_macos(wheel_path, temp_dir):
    """Repair macOS wheel using delocate."""
    cmd = ["delocate-wheel", "-w", str(temp_dir), "-v", str(wheel_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def repair_wheel_linux(wheel_path, temp_dir):
    """Repair Linux wheel using auditwheel."""
    result = subprocess.run(
        ["auditwheel", "repair", str(wheel_path), "-w", str(temp_dir)], capture_output=True, text=True
    )
    return result


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

    current_platform = get_platform()
    current_arch = platform.machine()

    repaired_count = 0
    skipped_count = 0
    deleted_count = 0
    error_count = 0
    errors = []

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
                result = repair_wheel_macos(renamed_wheel, temp_dir)

                if result.stdout:
                    print(f"  {result.stdout.strip()}")
                if result.stderr:
                    print_color(f"  {result.stderr.strip()}", Fore.RED)

                # Update wheel reference and error message for subsequent checks
                wheel = renamed_wheel
                error_msg = result.stderr.strip() if result.stderr else ""

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
