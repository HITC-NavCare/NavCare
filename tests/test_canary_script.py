"""Runs deployment/canary.sh with a fake gcloud that keeps the service's env vars in a file."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# describe prints the service from the state file; update applies --update-env-vars to it.
FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *" run services describe "*) cat "$FAKE_STATE" ;;
  *" run services update "*)
    [ "${FAKE_UPDATE_EXIT:-0}" = 0 ] || exit "$FAKE_UPDATE_EXIT"
    "$PYTHON" "$FAKE_APPLY" "$FAKE_STATE" "$@" ;;
  *) exit 1 ;;
esac
"""
APPLY = """
import json, sys
path, args = sys.argv[1], sys.argv[2:]
(flag,) = [a for a in args if a.startswith("--update-env-vars=")]
service = json.load(open(path))
env = service["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
for pair in flag.split("=", 1)[1].split(","):
    name, value = pair.split("=", 1)
    env[:] = [item for item in env if item["name"] != name] + [{"name": name, "value": value}]
json.dump(service, open(path, "w"))
"""
BASH = shutil.which("bash") if os.name != "nt" or os.environ.get("MSYSTEM") else None
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash (Git Bash on Windows)")


def service(**env):
    items = [{"name": "GOOGLE_MAPS_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest"}}}]
    items += [{"name": name, "value": value} for name, value in env.items()]
    return {"spec": {"template": {"spec": {"containers": [{"env": items}]}}}}


def canary(tmp_path, *args, state=None, update_exit=0, apply=APPLY, **env):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE_GCLOUD, newline="\n")
    fake.chmod(0o755)
    apply_file = tmp_path / "apply.py"
    apply_file.write_text(apply)
    state_file = tmp_path / "service.json"
    if not state_file.exists():
        state_file.write_text(json.dumps(state or service(V2_TRAFFIC_PERCENT="0")))
    log = tmp_path / "calls.log"
    run_env = {**os.environ, "GCLOUD": fake.as_posix(), "FAKE_LOG": log.as_posix(),
               "FAKE_STATE": state_file.as_posix(), "FAKE_APPLY": apply_file.as_posix(),
               "FAKE_UPDATE_EXIT": str(update_exit), "PYTHON": Path(sys.executable).as_posix(),
               **env}
    for name in ("PROJECT_ID", "ENV", "SERVICE", "REGION"):
        if name not in env:
            run_env.pop(name, None)
    result = subprocess.run([BASH, "deployment/canary.sh", *args], cwd=REPO, env=run_env,
                            capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls, json.loads(state_file.read_text())


def env_of(state):
    return {item["name"]: item.get("value")
            for item in state["spec"]["template"]["spec"]["containers"][0]["env"]}


def updates(calls):
    return [call for call in calls if " run services update " in call]


def test_script_is_executable_in_git():
    entry = subprocess.run(["git", "ls-files", "-s", "deployment/canary.sh"], cwd=REPO,
                           capture_output=True, text=True).stdout
    assert entry.startswith("100755 ")


def test_set_changes_only_the_share_and_prints_before_and_after(tmp_path):
    state = service(V2_TRAFFIC_PERCENT="10", ENGINE_HEADER_ENABLED="false")
    result, calls, after = canary(tmp_path, "set", "25", state=state)

    assert result.returncode == 0, result.stderr
    (update,) = updates(calls)
    for part in ("--project=project-69a1b17d-f37d-485c-912", "--quiet", "navcare-api-dev",
                 "--region=northamerica-northeast1", "--update-env-vars=V2_TRAFFIC_PERCENT=25"):
        assert part in update
    assert "--set-env-vars" not in update  # that would wipe the other variables
    assert env_of(after)["V2_TRAFFIC_PERCENT"] == "25"
    assert env_of(after)["ENGINE_HEADER_ENABLED"] == "false"
    before_part, after_part = result.stdout.split("After")
    assert "V2_TRAFFIC_PERCENT=10" in before_part
    assert "V2_TRAFFIC_PERCENT=25" in after_part
    assert "V2_FAULT_ENABLED=(unset)" in after_part


def test_after_is_read_back_from_the_service(tmp_path):
    # If the update does not take, "After" must show the real value, not the one asked for.
    result, calls, _ = canary(tmp_path, "set", "50", state=service(V2_TRAFFIC_PERCENT="10"),
                              apply="")
    assert result.returncode == 0, result.stderr
    assert len(updates(calls)) == 1
    assert "V2_TRAFFIC_PERCENT=10" in result.stdout.split("After")[1]


def test_rollback_sets_zero(tmp_path):
    result, calls, after = canary(tmp_path, "rollback", state=service(V2_TRAFFIC_PERCENT="50"))

    assert result.returncode == 0, result.stderr
    (update,) = updates(calls)
    assert "--update-env-vars=V2_TRAFFIC_PERCENT=0" in update
    assert env_of(after)["V2_TRAFFIC_PERCENT"] == "0"
    assert "every new request goes to v1" in result.stdout


@pytest.mark.parametrize("value", ["101", "-5", "12.5", "ten", "", "1e2"])
def test_set_refuses_bad_shares_without_calling_update(tmp_path, value):
    result, calls, after = canary(tmp_path, "set", value)

    assert result.returncode == 2
    assert "whole number from 0 to 100" in result.stderr
    assert updates(calls) == []
    assert env_of(after)["V2_TRAFFIC_PERCENT"] == "0"


def test_leading_zeros_are_not_octal(tmp_path):
    result, calls, _ = canary(tmp_path, "set", "010")
    assert result.returncode == 0, result.stderr
    assert "--update-env-vars=V2_TRAFFIC_PERCENT=10" in updates(calls)[0]


def test_status_changes_nothing(tmp_path):
    state = service(V2_TRAFFIC_PERCENT="10", V2_FAULT_ENABLED="true", V2_FAULT_MODE="error")
    result, calls, _ = canary(tmp_path, "status", state=state)

    assert result.returncode == 0, result.stderr
    assert updates(calls) == []
    assert "V2_TRAFFIC_PERCENT=10" in result.stdout
    assert "fault injection is ON" in result.stdout


def test_fault_modes(tmp_path):
    result, calls, after = canary(tmp_path, "fault", "error")
    assert result.returncode == 0, result.stderr
    assert "--update-env-vars=V2_FAULT_ENABLED=true,V2_FAULT_MODE=error" in updates(calls)[-1]
    assert "fault injection is ON" in result.stdout

    result, calls, after = canary(tmp_path, "fault", "slow")
    assert ("--update-env-vars=V2_FAULT_ENABLED=true,V2_FAULT_MODE=slow,"
            "V2_FAULT_SLOW_SECONDS=35") in updates(calls)[-1]

    result, calls, after = canary(tmp_path, "fault", "slow", "50")
    assert "V2_FAULT_SLOW_SECONDS=50" in updates(calls)[-1]

    result, calls, after = canary(tmp_path, "fault", "off")
    assert result.returncode == 0, result.stderr
    assert "--update-env-vars=V2_FAULT_ENABLED=false,V2_FAULT_MODE=off" in updates(calls)[-1]
    assert env_of(after)["V2_FAULT_ENABLED"] == "false"
    assert "fault injection is ON" not in result.stdout
    assert env_of(after)["V2_TRAFFIC_PERCENT"] == "0"  # the share is not touched


@pytest.mark.parametrize("args", [("fault", "slow", "0"), ("fault", "slow", "61"),
                                  ("fault", "slow", "abc"), ("fault", "broken"), ("fault",),
                                  ("set",), ("set", "10", "20"), ("rollback", "now"),
                                  ("promote",), ()])
def test_bad_commands_change_nothing(tmp_path, args):
    result, calls, _ = canary(tmp_path, *args)

    assert result.returncode == 2
    assert updates(calls) == []
    assert result.stderr


def test_service_follows_env_like_deploy(tmp_path):
    result, calls, _ = canary(tmp_path, "set", "10", ENV="prod", PROJECT_ID="other",
                              REGION="us-east1")
    assert result.returncode == 0, result.stderr
    update = updates(calls)[0]
    assert "navcare-api-prod" in update and "--project=other" in update
    assert "--region=us-east1" in update


def test_failed_update_fails_the_command(tmp_path):
    result, _, after = canary(tmp_path, "set", "10", update_exit=1)
    assert result.returncode != 0
    assert "After" not in result.stdout
    assert env_of(after)["V2_TRAFFIC_PERCENT"] == "0"
