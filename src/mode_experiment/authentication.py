"""Reuse Tau's subscription OAuth, credential format and refresh serialization."""

from collections.abc import Awaitable, Callable

import httpx

from mode_experiment.tracing import Output
from tau_ai.openai_codex import OpenAICodexCredentials
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.oauth import login_openai_codex
from tau_coding.provider_config import OpenAICodexProviderConfig
from tau_coding.provider_runtime import OpenAICodexCredentialResolver

LOGIN_HINT = "Run: uv run python -m mode_experiment login"


def remember_credentials(store: FileCredentialStore, output: Output) -> OAuthCredential | None:
    credential = store.get_oauth("openai-codex")
    if credential is not None:
        for value in (credential.access, credential.refresh, credential.account_id):
            output.redactor.remember(value)
    return credential


async def login(
    store: FileCredentialStore,
    client: httpx.AsyncClient,
    output: Output,
    prompt: Callable[[str], Awaitable[str]],
    *,
    open_browser: bool = True,
) -> None:
    try:
        credential = await login_openai_codex(
            on_auth=lambda info: output.auth_link(info.url),
            on_prompt=lambda info: prompt(info.message),
            on_manual_code_input=lambda: prompt(
                "Paste the redirect URL/code if browser callback does not finish:"
            ),
            on_progress=lambda text: output.emit("[harness: login] " + text),
            client=client,
            open_browser=open_browser,
        )
        store.set_oauth("openai-codex", credential)
        remember_credentials(store, output)
    except Exception:
        # OAuth helper errors can embed arbitrary token endpoint bodies.
        raise RuntimeError(
            "Codex login failed; check the redacted HTTP trace and retry login."
        ) from None
    output.emit("[harness: login] Saved Tau's openai-codex subscription credential.")


def credential_resolver(
    store: FileCredentialStore,
    client: httpx.AsyncClient,
    output: Output,
) -> Callable[[], Awaitable[OpenAICodexCredentials]]:
    if remember_credentials(store, output) is None:
        raise RuntimeError("Missing Codex subscription credentials. " + LOGIN_HINT)
    resolver = OpenAICodexCredentialResolver(
        OpenAICodexProviderConfig(), credential_store=store, client=client
    )

    async def resolve() -> OpenAICodexCredentials:
        if remember_credentials(store, output) is None:
            raise RuntimeError("Missing Codex subscription credentials. " + LOGIN_HINT)
        try:
            credential = await resolver()
        except Exception:
            raise RuntimeError("Codex authentication/refresh failed. " + LOGIN_HINT) from None
        remember_credentials(store, output)
        return credential

    return resolve
