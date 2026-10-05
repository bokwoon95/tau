from io import StringIO
from unittest.mock import Mock

import httpx
import pytest

from mode_experiment import authentication, tracing


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message,detail",
    [
        ("[SSL: CERTIFICATE_VERIFY_FAILED] secret-code", "TLS certificate verification failed"),
        ("connection failed: secret-code", "HTTP transport failure (ConnectError)"),
    ],
)
async def test_login_transport_errors_are_useful_without_leaking(monkeypatch, message, detail):
    async def fail(**kwargs):
        raise httpx.ConnectError(message)

    monkeypatch.setattr(authentication, "login_openai_codex", fail)
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError) as caught:
            await authentication.login(Mock(), client, tracing.Output(StringIO()), Mock())
    assert detail in str(caught.value)
    assert "secret-code" not in str(caught.value)


def test_windows_client_uses_system_ca_trust_and_preserves_explicit_verify(monkeypatch):
    monkeypatch.setattr(tracing.sys, "platform", "win32")
    context = Mock()
    factory = Mock(return_value=context)
    client_factory = Mock()
    monkeypatch.setattr(tracing.ssl, "create_default_context", factory)
    monkeypatch.setattr(tracing, "create_async_client", client_factory)
    trace = tracing.Trace(tracing.Output(StringIO()))
    trace.client(timeout=60)
    factory.assert_called_once_with()
    assert client_factory.call_args.kwargs["verify"] is context
    explicit_context = Mock()
    trace.client(verify=explicit_context)
    assert client_factory.call_args.kwargs["verify"] is explicit_context
    factory.assert_called_once_with()
