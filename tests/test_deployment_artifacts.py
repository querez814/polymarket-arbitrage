from pathlib import Path
from importlib import metadata

from scripts.verify_locked_environment import locked_versions, verify

ROOT = Path(__file__).parents[1]


def test_systemd_unit_enforces_single_owner_and_fail_closed_startup():
    unit = (ROOT / "deploy/systemd/nightwatch.service").read_text(encoding="utf-8")

    for required in (
        "User=nightwatch",
        "UMask=0077",
        "ExecStartPre=",
        "scripts/production_preflight.py",
        "--phase ${NIGHTWATCH_PREFLIGHT_PHASE}",
        "NIGHTWATCH_PREFLIGHT_PHASE=deployment",
        "--state-directory /var/lib/nightwatch",
        "--config-owner-uid 0",
        "--secret-file /etc/nightwatch/nightwatch.env",
        "scripts/verify_locked_environment.py",
        "ExecStartPost=",
        "/health/live",
        "Restart=on-failure",
        "KillSignal=SIGTERM",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/nightwatch /var/log/nightwatch",
    ):
        assert required in unit
    assert unit.count("ExecStart=") == 1
    assert "0.0.0.0" not in unit


def test_production_dependency_lock_is_exactly_pinned():
    content = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    lines = content.splitlines()
    requirements = [line for line in lines if line and not line.startswith(("#", " "))]

    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert content.count("--hash=sha256:") >= len(requirements)
    assert any(line.startswith("py-clob-client-v2==") for line in requirements)
    assert any(line.startswith("uvicorn==") for line in requirements)


def test_environment_template_names_secrets_without_values():
    lines = (
        (ROOT / "deploy/systemd/nightwatch.env.example")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assignments = {
        key: value
        for key, value in (line.split("=", 1) for line in lines if "=" in line)
    }

    for name in (
        "POLYMARKET_PRIVATE_KEY",
        "NIGHTWATCH_OPERATOR_TOKEN",
        "NIGHTWATCH_ALERT_WEBHOOK_TOKEN",
    ):
        assert name in assignments
        assert assignments[name] == ""


def test_locked_environment_verifier_parses_hashed_lock_and_matches_runtime(tmp_path):
    pins = locked_versions(ROOT / "requirements.lock")
    fixture = tmp_path / "requirements.lock"
    fixture.write_text(
        f"pytest=={metadata.version('pytest')} \\\n"
        "    --hash=sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )

    assert pins["py-clob-client-v2"] == "1.0.2"
    installed = {"pytest": metadata.version("pytest")}
    assert verify(fixture, installed) == ()
    assert verify(fixture, {**installed, "unexpected-package": "1.0"}) == (
        "unexpected-package:unexpected",
    )


def test_deployment_uses_release_local_environment_and_private_secret_contract():
    unit = (ROOT / "deploy/systemd/nightwatch.service").read_text(encoding="utf-8")
    readme = (ROOT / "deploy/systemd/README.md").read_text(encoding="utf-8")

    assert "/opt/nightwatch/current/.venv/bin/python" in unit
    assert "root:root" in readme
    assert "root:nightwatch" in readme
    assert "0600" in readme
    assert "/opt/nightwatch/venv" not in unit
