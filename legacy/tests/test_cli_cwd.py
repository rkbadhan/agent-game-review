"""`python -m legacy eval` resolves its default data against the checkout, not the cwd."""

import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_eval_defaults_work_from_another_working_directory(tmp_path):
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "legacy", "--store", str(tmp_path / "store"), "eval"],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Reviewer evaluation" in proc.stdout
