"""Microsoft Graph client credentials, with certificate-based authentication.

Private keys remain on the server: never store or publish them in source control.
"""
from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx


class GraphError(Exception):
    """Safe-to-log Graph integration error without tokens or response bodies."""


def graph_configured() -> bool:
    base = all(os.getenv(k) for k in ("SOAST_GRAPH_TENANT_ID", "SOAST_GRAPH_CLIENT_ID", "SOAST_GRAPH_ORGANIZER"))
    cert = os.getenv("SOAST_GRAPH_CERT_PATH") and os.getenv("SOAST_GRAPH_KEY_PATH")
    return bool(base and (cert or os.getenv("SOAST_GRAPH_CLIENT_SECRET")))


@lru_cache(maxsize=2)
def _msal_app(tenant: str, client: str, cert_file: str, key_file: str, secret: str):
    import msal

    if cert_file and key_file:
        credential = {
            "private_key": Path(key_file).read_text(encoding="utf-8"),
            "public_certificate": Path(cert_file).read_text(encoding="utf-8"),
        }
    elif secret:
        credential = secret
    else:
        raise GraphError("Credenciales de Graph no configuradas")
    return msal.ConfidentialClientApplication(
        client_id=client,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=credential,
    )


def _get_token() -> str:
    tenant = os.getenv("SOAST_GRAPH_TENANT_ID", "")
    client = os.getenv("SOAST_GRAPH_CLIENT_ID", "")
    cert = os.getenv("SOAST_GRAPH_CERT_PATH", "")
    key = os.getenv("SOAST_GRAPH_KEY_PATH", "")
    secret = os.getenv("SOAST_GRAPH_CLIENT_SECRET", "")
    if not graph_configured():
        raise GraphError("Microsoft Graph no configurado")
    try:
        app = _msal_app(tenant, client, cert, key, secret)
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if result.get("access_token"):
            return result["access_token"]
        raise GraphError(f"Autenticación de Graph rechazada: {result.get('error', 'sin código')}")
    except (OSError, ValueError) as exc:
        raise GraphError(f"Certificado de Graph inaccesible o inválido: {type(exc).__name__}") from exc


async def graph_request(method: str, path: str, **kwargs) -> httpx.Response:
    # MSAL's token acquisition may perform blocking network I/O.
    token = await asyncio.to_thread(_get_token)
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'IdType="ImmutableId"'}
    headers.update(kwargs.pop("headers", {}))
    try:
        async with httpx.AsyncClient(base_url="https://graph.microsoft.com/v1.0", timeout=12.0) as http:
            response = await http.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise GraphError(f"Microsoft Graph no disponible: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise GraphError(f"Microsoft Graph HTTP {response.status_code}")
    return response


def events_path() -> str:
    organizer = os.getenv("SOAST_GRAPH_ORGANIZER", "")
    if not organizer:
        raise GraphError("Organizador de Microsoft Graph no configurado")
    return f"/users/{quote(organizer, safe='')}/events"