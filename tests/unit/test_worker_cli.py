from smm_agent.worker.main import _parser


def test_production_worker_does_not_expose_stability_override() -> None:
    option_strings = {
        option
        for action in _parser()._actions
        for option in action.option_strings
    }

    assert "--stable-seconds" not in option_strings
