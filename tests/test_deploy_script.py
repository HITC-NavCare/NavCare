"""Runs deployment/deploy.sh with a fake gcloud that records its commands. No cloud calls."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"--format=value(status.url)"*) echo "https://fake.run.app"; exit 0 ;;
  *" describe "*) exit "$FAKE_DESCRIBE_EXIT" ;;
  *" builds submit "*) tar -tzf "$5" > "$FAKE_LOG.archive" ;;  # $5: after --project, --quiet
esac
"""
# Windows needs Git Bash (MSYSTEM set); the bash.exe in System32 is WSL.
BASH = shutil.which("bash") if os.name != "nt" or os.environ.get("MSYSTEM") else None
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")


def deploy(tmp_path, existing=False, **env):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE_GCLOUD, newline="\n")
    log = tmp_path / "calls.log"
    run_env = {**os.environ, "GCLOUD": fake.as_posix(), "FAKE_LOG": log.as_posix(),
               "FAKE_DESCRIBE_EXIT": "0" if existing else "1", **env}
    for name in ("PROJECT_ID", "ENV", "SERVICE", "ENGINE_HEADER_ENABLED", "MAX_INSTANCES"):
        if name not in env:
            run_env.pop(name, None)
    result = subprocess.run([BASH, "deployment/deploy.sh"], cwd=REPO, env=run_env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    archive = Path(f"{log}.archive")
    return log.read_text().splitlines(), archive.read_text().split() if archive.exists() else []


def only(calls, text):
    (call,) = [c for c in calls if text in c]
    return call


def test_first_deploy_creates_the_repo_and_sets_safe_defaults(tmp_path):
    calls, _ = deploy(tmp_path)

    assert any("artifacts repositories create navcare" in c for c in calls)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
    registry = "northamerica-northeast1-docker.pkg.dev/project-69a1b17d-f37d-485c-912"
    image = f"{registry}/navcare/api:{sha}"
    assert f"--tag={image}" in only(calls, "builds submit")
    run = only(calls, "run deploy")
    for flag in ("navcare-api-dev", f"--image={image}", "--region=northamerica-northeast1",
                 "--service-account=navcare-run@project-69a1b17d-f37d-485c-912.iam.gserviceaccount.com",
                 "--set-secrets=GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest",
                 "ENGINE_HEADER_ENABLED=false", "V2_TRAFFIC_PERCENT=0",
                 "--labels=app=navcare,env=dev", "--cpu=1", "--memory=512Mi",
                 "--concurrency=40", "--timeout=120", "--min-instances=0", "--max-instances=2"):
        assert flag in run
    assert "allow-unauthenticated" not in run  # public access is left to the owner
    assert "V2_FAULT" not in run


def test_redeploy_keeps_the_canary_share_and_the_repo(tmp_path):
    calls, _ = deploy(tmp_path, existing=True)

    assert not any("repositories create" in c for c in calls)
    assert "V2_TRAFFIC_PERCENT" not in only(calls, "run deploy")


def test_header_only_on_when_asked_and_never_in_prod(tmp_path):
    calls, _ = deploy(tmp_path, ENGINE_HEADER_ENABLED="true")
    assert "ENGINE_HEADER_ENABLED=true" in only(calls, "run deploy")

    (tmp_path / "calls.log").unlink()
    calls, _ = deploy(tmp_path, ENV="prod", ENGINE_HEADER_ENABLED="true")
    run = only(calls, "run deploy")
    assert "navcare-api-prod" in run and "ENGINE_HEADER_ENABLED=false" in run


def test_build_uploads_only_committed_files(tmp_path):
    _, archive = deploy(tmp_path)

    committed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=REPO,
                               capture_output=True, text=True).stdout.split()
    files = [name for name in archive if not name.endswith("/")]  # tar also lists folders
    assert sorted(files) == sorted(committed)
    assert ".env" not in archive
