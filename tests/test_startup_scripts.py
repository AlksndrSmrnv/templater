from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_docker_entrypoint_runs_migrations_then_application_without_reference_seeding(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"

    _write_executable(
        bin_dir / "python",
        """#!/usr/bin/env bash
printf 'python' >> "$CALL_LOG"
printf ' %s' "$@" >> "$CALL_LOG"
printf '\\n' >> "$CALL_LOG"
if [ "$#" -eq 0 ]; then
    cat >/dev/null
fi
""",
    )
    _write_executable(
        bin_dir / "alembic",
        """#!/usr/bin/env bash
printf 'alembic %s\\n' "$*" >> "$CALL_LOG"
""",
    )
    _write_executable(
        bin_dir / "fake-app",
        """#!/usr/bin/env bash
printf 'fake-app %s\\n' "$*" >> "$CALL_LOG"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "CALL_LOG": str(call_log),
            "DATABASE_DSN": (
                "host=postgres port=5432 dbname=template_maker "
                "user=template_maker password=template_maker"
            ),
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SEED_REFERENCE_DATA": "1",
        }
    )

    result = subprocess.run(
        [PROJECT_ROOT / "docker/docker-entrypoint.sh", "fake-app", "--serve"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "python -",
        "alembic upgrade head",
        "fake-app --serve",
    ]
