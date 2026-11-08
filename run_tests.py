#!/usr/bin/env python3
"""
CloudPilot Automated Test Suite Runner (Phase 4A)
Executes isolated test suites for Auth Service, Workspace Management, and Cloud Service.
Measures line & branch coverage and aggregates results.
"""

import sys
import os
import subprocess
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def get_test_db_url():
    if os.getenv("TEST_DATABASE_URL"):
        return os.getenv("TEST_DATABASE_URL")
    # Resolve password from git-ignored local .env if present
    auth_env = os.path.join(PROJECT_ROOT, "backend", "auth-service", ".env")
    if os.path.exists(auth_env):
        with open(auth_env, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if "CloudPilot" in val:
                        return val.replace("/CloudPilot", "/cloudpilot_test")
    return "postgresql://postgres:postgres@localhost:5432/cloudpilot_test"

TEST_DB_URL = get_test_db_url()

SUITES = [
    {
        "name": "Auth & Workspace Service",
        "service_dir": os.path.join(PROJECT_ROOT, "backend", "auth-service"),
        "test_path": os.path.join("backend", "auth-service", "tests"),
        "cov_source": "app",
    },
    {
        "name": "Cloud Account Management Service",
        "service_dir": os.path.join(PROJECT_ROOT, "backend", "cloud-service"),
        "test_path": os.path.join("backend", "cloud-service", "tests"),
        "cov_source": "app",
    }
]


def run_suite(suite: dict, extra_args: list) -> tuple[int, str]:
    env = os.environ.copy()
    env["TEST_DATABASE_URL"] = TEST_DB_URL
    env["TESTING"] = "true"
    # Ensure service dir is first in PYTHONPATH to load appropriate app package
    env["PYTHONPATH"] = suite["service_dir"] + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [
        sys.executable, "-m", "pytest",
        suite["test_path"],
        f"--cov={suite['cov_source']}",
        "--cov-report=term-missing",
        "-v"
    ] + extra_args

    print(f"\n{'='*78}")
    print(f"  RUNNING: {suite['name']}")
    print(f"  PATH:    {suite['test_path']}")
    print(f"{'='*78}\n")

    start_time = time.time()
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    duration = time.time() - start_time

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    return result.returncode, duration, result.stdout


def main():
    extra_args = sys.argv[1:]
    total_passed = 0
    total_failed = 0
    suite_summaries = []
    has_failure = False

    print("==============================================================================")
    print("                CLOUDPILOT AUTOMATED TESTING FOUNDATION                       ")
    print(f"             Target Database: cloudpilot_test (ISOLATED)                      ")
    print("==============================================================================")

    for suite in SUITES:
        code, duration, stdout = run_suite(suite, extra_args)
        if code != 0:
            has_failure = True

        # Parse test counts from output
        passed = 0
        failed = 0
        for line in stdout.splitlines():
            if "passed" in line and ("=" in line):
                parts = line.strip("=").strip().split(",")
                for p in parts:
                    p = p.strip()
                    if "passed" in p:
                        try:
                            passed = int(p.split()[0])
                        except ValueError:
                            pass
                    if "failed" in p:
                        try:
                            failed = int(p.split()[0])
                        except ValueError:
                            pass

        total_passed += passed
        total_failed += failed
        suite_summaries.append({
            "name": suite["name"],
            "status": "PASSED" if code == 0 else "FAILED",
            "passed": passed,
            "failed": failed,
            "duration": f"{duration:.2f}s"
        })

    print("\n" + "="*78)
    print("                        OVERALL TEST EXECUTION SUMMARY                         ")
    print("="*78)
    print(f"{'Service Suite':<36} | {'Status':<8} | {'Passed':<7} | {'Failed':<7} | {'Duration':<8}")
    print("-" * 78)
    for s in suite_summaries:
        print(f"{s['name']:<36} | {s['status']:<8} | {s['passed']:<7} | {s['failed']:<7} | {s['duration']:<8}")
    print("-" * 78)
    print(f"{'TOTAL':<36} | {'ALL OK' if not has_failure else 'FAIL':<8} | {total_passed:<7} | {total_failed:<7} |")
    print("="*78 + "\n")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()

