"""Checks the rules of .github/workflows/deploy.yml (#30) that a mistake could quietly break."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
TEXT = (WORKFLOWS / "deploy.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)
DEPLOY = WORKFLOW["jobs"]["deploy"]


def triggers(workflow):
    return workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as True


def step(name_or_uses):
    (found,) = [s for s in DEPLOY["steps"]
                if name_or_uses in (s.get("name", ""), s.get("uses", "")) or
                name_or_uses in s.get("run", "")]
    return found


def test_runs_only_on_main_and_by_hand():
    on = triggers(WORKFLOW)
    assert on["push"] == {"branches": ["main"]}
    assert set(on) == {"push", "workflow_dispatch"}  # never for a pull request
    assert "refs/heads/main" in DEPLOY["if"]


def test_deploys_only_after_the_same_checks_as_every_push():
    assert WORKFLOW["jobs"]["checks"]["uses"] == "./.github/workflows/lint.yml"
    assert DEPLOY["needs"] == "checks"
    lint = yaml.safe_load((WORKFLOWS / "lint.yml").read_text())
    assert "workflow_call" in triggers(lint)
    assert {"ruff", "tests"} <= set(lint["jobs"])


def test_pause_switch_skips_the_deploy_but_not_the_checks():
    assert "vars.DEPLOY_PAUSED != 'true'" in DEPLOY["if"]
    assert "if" not in WORKFLOW["jobs"]["checks"]


def test_deploys_never_overlap_or_get_cancelled():
    assert DEPLOY["concurrency"] == {"group": "deploy-dev", "cancel-in-progress": False}


def test_signs_in_without_a_key_and_only_the_deploy_job_can():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert DEPLOY["permissions"] == {"contents": "read", "id-token": "write"}
    auth = [s for s in DEPLOY["steps"] if "google-github-actions/auth" in s.get("uses", "")]
    assert auth and all("workload_identity_provider" in s["with"] for s in auth)
    for word in ("credentials_json", "secrets.", "private_key"):
        assert word not in TEXT


def test_deploys_dev_with_deploy_sh_and_the_ci_build_options():
    deploy = step("Deploy")
    assert deploy["run"] == "deployment/deploy.sh"
    assert deploy["env"]["ENV"] == "dev"
    assert deploy["env"]["BUILD_LOGS"] == "false"
    assert deploy["env"]["STAGING_BUCKET"] == "${{ env.PROJECT_ID }}_cloudbuild"
    assert DEPLOY["env"]["SERVICE"] == "navcare-api-dev"
    assert "ENGINE_HEADER_ENABLED" not in {**DEPLOY["env"], **deploy["env"]}  # stays off


def test_checks_the_image_then_smoke_tests_v1_only():
    names = [s.get("name") for s in DEPLOY["steps"]]
    assert names.index("Deploy") < names.index("Check the serving image is this commit")
    assert "git rev-parse --short HEAD" in step("Check the serving image is this commit")["run"]
    smoke = step("python -m deployment.smoke_test")
    assert smoke["run"] == 'python -m deployment.smoke_test "$URL"'
    assert smoke["env"]["ID_TOKEN"] == "${{ steps.token.outputs.id_token }}"
    assert "--v2" not in TEXT  # a forced v2 request costs Gemini calls on every merge
