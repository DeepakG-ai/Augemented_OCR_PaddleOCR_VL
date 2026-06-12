"""Tests for the PaddleOCR native-thread oversubscription guard.

Each OCR worker process runs up to OCR_WORKERS engine threads concurrently. If
OpenMP/MKL/OpenBLAS are left uncapped, every concurrent engine call fans out
across all CPU cores and the box is oversubscribed. ocr_runner pins those libs
to OCR_CPU_THREADS at import time; these tests lock that behaviour in.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_THREAD_ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")


class ConfigCpuThreadsTests(unittest.TestCase):
    def test_default_is_a_positive_int(self) -> None:
        from backend.config import OCR_CPU_THREADS

        self.assertIsInstance(OCR_CPU_THREADS, int)
        self.assertGreaterEqual(OCR_CPU_THREADS, 1)


class OcrRunnerThreadCapTests(unittest.TestCase):
    def test_import_sets_all_native_thread_caps(self) -> None:
        # Importing the module is what installs the caps (via os.environ.setdefault).
        import backend.ocr_runner  # noqa: F401

        for var in _THREAD_ENV_VARS:
            with self.subTest(var=var):
                self.assertIn(var, os.environ, f"{var} should be pinned after import")
                self.assertGreaterEqual(int(os.environ[var]), 1)

    def test_clean_env_pins_caps_to_cpu_threads(self) -> None:
        """In a fresh interpreter with the vars unset, the caps equal OCR_CPU_THREADS.

        Runs in a subprocess so the assertion is deterministic regardless of the
        ambient environment or whether ocr_runner was already imported here.
        """
        import json
        import subprocess

        snippet = (
            "import os\n"
            "for v in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS'):\n"
            "    os.environ.pop(v, None)\n"
            "import backend.ocr_runner\n"
            "from backend.config import OCR_CPU_THREADS\n"
            "import json\n"
            "print(json.dumps({\n"
            "    'caps': {v: os.environ.get(v) for v in "
            "('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS')},\n"
            "    'cpu_threads': OCR_CPU_THREADS,\n"
            "}))\n"
        )
        env = dict(os.environ)
        for var in _THREAD_ENV_VARS:
            env.pop(var, None)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        # Keep the heavy ML pipeline from spinning up MLflow during import.
        env["MLFLOW_ENABLED"] = "false"

        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, msg=f"subprocess failed:\n{proc.stderr}")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        expected = str(payload["cpu_threads"])
        for var, value in payload["caps"].items():
            with self.subTest(var=var):
                self.assertEqual(value, expected)


if __name__ == "__main__":
    unittest.main()
