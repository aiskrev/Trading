"""AT-20: the regression eval gate passes at 100%."""
from evals import runner


def test_all_eval_cases_pass(capsys):
    assert runner.main() == 0
    assert "eval cases passed" in capsys.readouterr().out
