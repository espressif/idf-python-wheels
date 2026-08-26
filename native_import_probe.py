#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Run native import probes in a subprocess (segfault → non-zero exit)."""

from __future__ import annotations

import subprocess
import sys

from typing import Mapping
from typing import Sequence


def run_import_probes(
    imports: Sequence[str],
    *,
    timeout: int = 60,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Execute import statements; return (ok, combined stdout/stderr or error text)."""
    if not imports:
        return True, ""
    code = "\n".join(imports)
    cmd = [sys.executable, "-X", "faulthandler", "-c", code]
    run_env = dict(env) if env is not None else None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        return False, f"import probe timed out after {timeout}s"
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == 0:
        return True, out
    if result.returncode < 0:
        return False, f"process killed by signal {-result.returncode}" + (f"\n{out}" if out else "")
    return False, out or f"exit code {result.returncode}"
