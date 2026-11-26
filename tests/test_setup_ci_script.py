"""Runs deployment/setup_ci.sh with a fake gcloud that remembers what it created. No cloud calls."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROJECT = "project-69a1b17d-f37d-485c-912"
DEPLOYER = f"github-deployer@{PROJECT}.iam.gserviceaccount.com"
BUILDER = "471599020734-compute@developer.gserviceaccount.com"
# Files in FAKE_STATE stand for the deployer account, the pool and the provider.
FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"$FAKE_FAIL"*) exit 1 ;;
  *" projects describe "*) echo 471599020734 ;;
  *" builds get-default-service-account "*)
    echo "projects/471599020734/serviceAccounts/$FAKE_BUILDER" ;;
  *" iam service-accounts describe "*) [ -e "$FAKE_STATE/account" ] ;;
  *" iam service-accounts create "*) touch "$FAKE_STATE/account" ;;
  *" workload-identity-pools providers describe "*) [ -e "$FAKE_STATE/provider" ] ;;
  *" workload-identity-pools describe "*) [ -e "$FAKE_STATE/pool" ] ;;
esac
"""
READS = (" describe ", " get-default-service-account ")
BASH = shutil.which("bash") if os.name != "nt" or os.environ.get("MSYSTEM") else None
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")


def setup_ci(tmp_path, *args, existing=(), fail="no such call"):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE_GCLOUD, newline="\n")
    fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for name in existing:
        (state / name).touch()
    log = tmp_path / "calls.log"
    env = {**os.environ, "GCLOUD": fake.as_posix(), "FAKE_LOG": log.as_posix(),
           "FAKE_STATE": state.as_posix(), "FAKE_FAIL": fail, "FAKE_BUILDER": BUILDER}
    for name in ("PROJECT_ID", "REGION"):
        env.pop(name, None)
    result = subprocess.run([BASH, "deployment/setup_ci.sh", *args], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=60)
    return result, log.read_text().splitlines() if log.exists() else []


def changes(calls):
    return [call for call in calls if not any(read in call for read in READS)]


def bindings(calls, kind):
    """(resource, role) for each add-iam-policy-binding call of a kind (projects, iam, storage)."""
    found = []
    for call in calls:
        words = call.split()
        if "add-iam-policy-binding" in words and words[2] == kind:
            resource = words[words.index("add-iam-policy-binding") + 1]
            (role,) = [w.split("=", 1)[1] for w in words if w.startswith("--role=")]
            found.append((resource, role))
    return found


def test_script_is_executable_in_git():
    entry = subprocess.run(["git", "ls-files", "-s", "deployment/setup_ci.sh"], cwd=REPO,
                           capture_output=True, text=True).stdout
    assert entry.startswith("100755 ")


def test_dry_run_changes_nothing_and_prints_every_change(tmp_path):
    dry, dry_calls = setup_ci(tmp_path, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert changes(dry_calls) == []

    (tmp_path / "real").mkdir()
    real, real_calls = setup_ci(tmp_path / "real")
    assert real.returncode == 0, real.stderr
    printed = [line for line in dry.stdout.splitlines() if line.startswith("+ gcloud ")]
    assert len(printed) == len(changes(real_calls)) > 0


def test_deployer_gets_only_what_deploy_sh_needs(tmp_path):
    result, calls = setup_ci(tmp_path)
    assert result.returncode == 0, result.stderr

    member = f"--member=serviceAccount:{DEPLOYER}"
    assert all(member in call for call in calls if "add-iam-policy-binding" in call
               and "workloadIdentityUser" not in call)
    assert sorted(role for _, role in bindings(calls, "projects")) == [
        "roles/artifactregistry.reader", "roles/cloudbuild.builds.editor",
        "roles/run.developer", "roles/serviceusage.serviceUsageConsumer"]
    # Acts as only the runtime account and Cloud Build's account, not every account.
    assert sorted(bindings(calls, "iam")) == sorted([
        (f"navcare-run@{PROJECT}.iam.gserviceaccount.com", "roles/iam.serviceAccountUser"),
        (BUILDER, "roles/iam.serviceAccountUser"),
        (DEPLOYER, "roles/iam.workloadIdentityUser")])
    assert sorted(bindings(calls, "storage")) == [
        (f"gs://{PROJECT}_cloudbuild", "roles/storage.legacyBucketReader"),
        (f"gs://{PROJECT}_cloudbuild", "roles/storage.objectCreator")]
    assert not any("keys create" in call for call in calls)  # no key file, ever


def test_only_main_of_this_repository_can_sign_in(tmp_path):
    result, calls = setup_ci(tmp_path)
    assert result.returncode == 0, result.stderr

    (provider,) = [call for call in calls if "providers create-oidc" in call]
    assert "--issuer-uri=https://token.actions.githubusercontent.com" in provider
    condition = provider.split("--attribute-condition=", 1)[1]
    assert condition == ("assertion.repository_id == '1426436353' && "
                         "assertion.repository == 'HITC-NavCare/NavCare' && "
                         "assertion.ref == 'refs/heads/main'")
    (sign_in,) = [call for call in calls if "workloadIdentityUser" in call]
    assert ("--member=principalSet://iam.googleapis.com/projects/471599020734/locations/global/"
            "workloadIdentityPools/github/attribute.repository/HITC-NavCare/NavCare") in sign_in
    for name, value in (
            ("GCP_WORKLOAD_IDENTITY_PROVIDER", "projects/471599020734/locations/global/"
                                               "workloadIdentityPools/github/providers/navcare-main"),
            ("GCP_DEPLOYER_SA", DEPLOYER)):
        assert f"{name} = {value}" in " ".join(result.stdout.split())


def test_running_again_creates_nothing_and_updates_the_provider(tmp_path):
    result, calls = setup_ci(tmp_path, existing=("account", "pool", "provider"))
    assert result.returncode == 0, result.stderr

    assert not any(" create " in call or "create-oidc" in call for call in calls)
    assert len([call for call in calls if "providers update-oidc navcare-main" in call]) == 1


def test_a_failed_change_stops_the_script(tmp_path):
    result, calls = setup_ci(tmp_path, fail="roles/run.developer")

    assert result.returncode != 0
    assert not any("workload-identity-pools" in call for call in calls)
    assert "GCP_DEPLOYER_SA" not in result.stdout


def test_rejects_unknown_arguments(tmp_path):
    result, calls = setup_ci(tmp_path, "--yes")

    assert result.returncode == 2
    assert calls == []
