import pytest

from mode_experiment.protocols import ProtocolError, classify, file_action


@pytest.mark.parametrize("mode", [None, "bash", "python"])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_combined_switch_is_one_literal_action(mode, ending):
    payload = '  x = "@@tau exit"\r\n\n'
    decision = classify("@@tau mode python" + ending + payload, mode)
    assert decision.mode == "python" and decision.transition
    assert decision.action.arguments == {"code": payload}


@pytest.mark.parametrize(
    "text", ["@@tau mode python", "@@tau mode python\n", "@@tau mode python\r\n"]
)
def test_optional_entry(text):
    assert classify(text, None).action is None


@pytest.mark.parametrize("text", ["@@tau exit", "@@tau exit\n", "@@tau exit\r\n"])
def test_whole_exit(text):
    assert classify(text, "python").mode is None
    with pytest.raises(ProtocolError):
        classify(text, None)


@pytest.mark.parametrize(
    "text,mode",
    [
        ("hello", None),
        ("@@tau mode Python\nx", None),
        ("@@tau mode sqlite\nx", "bash"),
        ("@@tau final\nanswer", "python"),
        ("@@tau final", None),
        ("@@tau literal\nx", None),
        ("@@tau exit\n\n", "python"),
        ("@@tau exit ", "python"),
        ("@@tau mode python\r", None),
        ("@@tau literal", "bash"),
        ("@@tau mystery", "bash"),
    ],
)
def test_protocol_errors(text, mode):
    with pytest.raises(ProtocolError):
        classify(text, mode)


@pytest.mark.parametrize(
    "text", ['print("@@tau exit")', ".mode csv", "# code\n@@tau exit", " ```python\nx\n```"]
)
def test_no_marker_extraction_or_fence_repair(text):
    assert classify(text, "python").action.arguments["code"] == text


@pytest.mark.parametrize(
    "payload", ["@@tau exit", "@@tau literal\nx", "", "  \r\n\n", "@@tau mode bash\necho x"]
)
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_literal_round_trip(payload, ending):
    assert (
        classify("@@tau literal" + ending + payload, "python").action.arguments["code"] == payload
    )
    # Combined mode entry removes only its own header, never literal/command headers inside.
    assert (
        classify("@@tau mode python" + ending + payload, None).action is not None
        if payload
        else True
    )
    if payload:
        assert (
            classify("@@tau mode python" + ending + payload, None).action.arguments["code"]
            == payload
        )


def test_final_and_file_newlines():
    assert classify("@@tau final\r\n  answer\r\n", None).final == "  answer\r\n"
    assert file_action("read", "name\r\n").arguments == {"path": "name"}
    assert file_action("write", "name\r\n\r\n exact\r\n").arguments == {
        "path": "name",
        "content": "\r\n exact\r\n",
    }
    assert file_action("write", "name\n").arguments["content"] == ""
    with pytest.raises(ProtocolError):
        file_action("read", "name\n\n")


def test_edit_delimiter_escaping_and_no_terminal_newline():
    action = file_action(
        "edit",
        "f\r\n@@tau old\r\na\r\n\\@@tau new\r\n\\\\n\r\n@@tau new\r\nnew\r\n\\n\r\n@@tau end\r\n",
    )
    assert action.arguments == {
        "path": "f",
        "old_text": "a\r\n@@tau new\r\n\\n\r\n",
        "new_text": "new",
    }
    assert file_action("edit", "f\n@@tau old\na\n@@tau new\n@@tau end").arguments["new_text"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        "f\n@@tau old\na\n@@tau new\nb\n@@tau end\nextra",
        "f\n@@tau old\na\n@@tau end",
        "f\n@@tau old\n\\bad\n@@tau new\n@@tau end",
        "f\n@@tau old\n\\n\n@@tau new\n@@tau end",
    ],
)
def test_edit_malformed(payload):
    with pytest.raises(ProtocolError):
        file_action("edit", payload)
