from __future__ import annotations

import shutil
from pathlib import Path


def cleanup_workspace(job_root: Path, output: Path | None, keep_output: bool = True) -> None:
    """Remove intermediates only after publication has succeeded.

    The verified output is retained by default; failed uploads never call this.
    """
    if not job_root.exists():
        return
    for child in list(job_root.iterdir()):
        if output and child.resolve() == output.resolve() and keep_output:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass
