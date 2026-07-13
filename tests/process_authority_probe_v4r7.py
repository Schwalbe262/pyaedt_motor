from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import continue_ipmsm_v2_stage3_acquisition_v4r7 as runner


def main() -> int:
    executable = str(Path(sys.executable).resolve(strict=True))
    script = str(Path(__file__).resolve(strict=True))
    context = SimpleNamespace(
        runner_dry_argv=(executable, "-B", script),
        runner_execute_argv=(executable, "-B", script, "--execute"),
    )
    execute = sys.argv[1:] == ["--execute"]
    runner._audit_process_authority(context, execute=execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
