import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from nexus.db import AuthRecord


def login(rig, verifier="v"*64, wrong_password=False):
    _, _, client, settings = rig
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect = settings.oauth_redirect_uris[0]
    response = client.get("/authorize", params={"response_type": "code", "client_id": settings.oauth_client_id,
        "redirect_uri": redirect, "scope": "nexus", "state": "state-123", "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": settings.public_url+"/mcp"}, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    ticket = parse_qs(urlsplit(location).query)["ticket"][0]
    page = client.get(location)
    assert page.status_code == 200
    csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
    response = client.post("/connect", data={"ticket": ticket, "csrf": csrf,
        "password": "wrong" if wrong_password else settings.owner_password}, follow_redirects=False)
    if wrong_password:
        return response
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["state-123"]
    return query["code"][0]


def exchange(rig, code, verifier="v"*64):
    _, _, client, settings = rig
    return client.post("/token", data={"grant_type": "authorization_code", "code": code,
        "client_id": settings.oauth_client_id, "client_secret": settings.oauth_client_secret,
        "code_verifier": verifier, "redirect_uri": settings.oauth_redirect_uris[0],
        "resource": settings.public_url+"/mcp"})


def test_pkce_login_token_refresh_and_mcp_transport(rig):
    s, _, client, settings = rig
    code = login(rig)
    response = exchange(rig, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    headers = {"Authorization": "Bearer "+tokens["access_token"], "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
        "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}}})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "Research Nexus"
    tools = client.post("/mcp", headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).json()["result"]["tools"]
    assert {t["name"] for t in tools} >= {"inspect_research", "start_research", "answer_from_library"}
    answered = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3,
        "method": "tools/call", "params": {"name": "answer_from_library", "arguments": {"question": "What is in the library?"}}})
    assert answered.status_code == 200 and not answered.json()["result"].get("isError", False)
    refresh = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": settings.oauth_client_id, "client_secret": settings.oauth_client_secret})
    assert refresh.status_code == 200
    assert refresh.json()["access_token"] != tokens["access_token"]
    assert client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}).status_code == 401
    with s.sessions() as db:
        records = list(db.scalars(select(AuthRecord)))
        assert all(tokens["access_token"] not in str(r.data) for r in records)


def test_authorization_code_cannot_be_replayed(rig):
    code = login(rig)
    assert exchange(rig, code).status_code == 200
    assert exchange(rig, code).status_code == 400


def test_bad_pkce_verifier_rejected(rig):
    code = login(rig)
    assert exchange(rig, code, "wrong"*15).status_code == 400
    assert exchange(rig, code).status_code == 200


def test_bad_owner_password_and_unauthed_mcp_rejected(rig):
    assert login(rig, wrong_password=True).status_code == 401
    _, _, client, _ = rig
    assert client.post("/mcp", json={}).status_code == 401


def test_unregistered_callback_never_receives_code(rig):
    _, _, client, settings = rig
    response = client.get("/authorize", params={"response_type": "code", "client_id": settings.oauth_client_id,
        "redirect_uri": "https://evil.example/steal", "code_challenge": "x"*43,
        "code_challenge_method": "S256", "resource": settings.public_url+"/mcp"}, follow_redirects=False)
    assert response.status_code == 400
    assert "location" not in response.headers


def test_auth_metadata_advertises_pkce_and_resource(rig):
    _, _, client, settings = rig
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == settings.public_url+"/mcp"
    assert "registration_endpoint" not in metadata  # fixed private client; no public registration
