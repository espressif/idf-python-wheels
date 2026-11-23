## Summary

- Each ESP-IDF release is resolved on its own. Its requirement files and its constraint file, including the generated upper bounds, are solved together.
- Platform and Python exclusions are applied before resolving. Unsupported packages, such as ARMv7/Python 3.8 `cryptography`, cannot fail pip resolution before they are excluded.
- The solve produces an exact version list. Those versions are what get built. A dependency is not resolved a second time during the wheel build.
- Versions from different releases are kept. An older exact pin is no longer dropped just because a newer wheel of the same package is already in the wheel directory.
- Other Python versions rebuild only wheels whose filename is tied to one Python (`cp` in the ABI tag). Lines that merely mention `python_version` are not copied into that list.
- Linux x86_64 and ARM64 still keep a `manylinux_2_28` wheel for older glibc. ARMv7 and ARMv7 Legacy stay on their own images and are not mixed. A Bookworm binary is not published as if it ran on Debian 11 / Ubuntu 20.04.

## What was going wrong

- Constraint files were treated as more packages to build, not as version limits.
- The new generated upper-bounds block added hundreds of those lines, almost all with a `python_version` marker.
- Every one of those markers was queued for every other Python version, and those jobs then tried to compile the whole list from source.
- That failed Linux builds (for example pillow) and made the ARMv7 jobs hit the time limit.
- Some excluded packages were still sent to pip before the exclude list was applied, so an unsupported package could fail resolution even though it would never be built.

## What stays the same

- One platform job still builds the wheels. There is no new job per ESP-IDF branch.
- The same exclude list, include list, and per-package wheel build are used.
- Exclusions are checked both before resolution and after pinning; the original requirements are still used for the sdist list.
- Packages named only as a version cap, and not pulled in by a requirement, are not built. ESP-IDF does not install them.
