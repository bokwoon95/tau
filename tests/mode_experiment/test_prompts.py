import pytest

from mode_experiment.prompts import experiment_prompt


@pytest.mark.parametrize("protocol", ["tools", "modes"])
def test_workspace_is_default_directory_not_access_boundary(tmp_path, protocol):
    prompt = experiment_prompt(protocol, [], tmp_path)

    assert "The workspace is a default working directory, not an access boundary" in prompt
    assert "User-requested operations may access paths outside the workspace" in prompt
    assert "Relative file paths use the workspace root" in prompt
    assert "Operate only on the selected workspace" not in prompt
    assert "do not access host secrets" in prompt
