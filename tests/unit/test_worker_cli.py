import argparse
import json

from pytest import CaptureFixture

from smm_agent.worker.main import WorkerRuntime, _parser, main


def test_production_worker_does_not_expose_stability_override() -> None:
    option_strings = {
        option
        for action in _parser()._actions
        for option in action.option_strings
    }

    assert "--stable-seconds" not in option_strings


class FakeRuntimeFactory:
    def __init__(self) -> None:
        self.arguments: argparse.Namespace | None = None

    def create(self, args: argparse.Namespace) -> WorkerRuntime:
        self.arguments = args
        return _FakeRuntime()  # type: ignore[return-value]


class _FakeRuntime:
    def run_once(self) -> dict[str, object]:
        return {"schema_version": "1.0", "outcome": "idle"}


def test_config_worker_mode_uses_injected_runtime_and_never_implies_replay(
    capsys: CaptureFixture[str],
) -> None:
    factory = FakeRuntimeFactory()

    main(["--config", r"C:\VeselkovSmm\config\smm-agent.toml", "--once"], runtime_factory=factory)

    assert factory.arguments is not None
    assert str(factory.arguments.config).endswith("smm-agent.toml")
    assert factory.arguments.publication_replay is False
    assert json.loads(capsys.readouterr().out) == {"schema_version": "1.0", "outcome": "idle"}
