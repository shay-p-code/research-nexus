"""Single-owner OAuth: SDK handles protocol/PKCE; this module persists grants."""
import html
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import delete, select
from starlette.responses import HTMLResponse, RedirectResponse

from nexus.db import AuthRecord
from nexus.service import Problem, digest


class OwnerOAuth:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings
        self.resource = settings.public_url + "/mcp"

    async def get_client(self, client_id):
        if client_id != self.settings.oauth_client_id:
            return None
        return OAuthClientInformationFull(
            client_id=client_id, client_secret=self.settings.oauth_client_secret,
            client_name="Research Nexus — private owner connection",
            redirect_uris=self.settings.oauth_redirect_uris,
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            scope="nexus", client_secret_expires_at=0,
        )

    async def register_client(self, client_info):
        raise NotImplementedError("Use the configured private OAuth client")

    async def authorize(self, client, params):
        if params.resource != self.resource:
            raise AuthorizeError(error="invalid_target", error_description="Incorrect resource")
        ticket = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            s.add(AuthRecord(key=digest(ticket), kind="pending", expires_at=time.time()+300,
                data={"client_id": client.client_id, "params": params.model_dump(mode="json"), "attempts": 0}))
        return self.settings.public_url + "/connect?ticket=" + ticket

    def read_record(self, raw, kind):
        with self.sessions() as s:
            row = s.get(AuthRecord, digest(raw))
            if row and row.kind == kind and row.expires_at > time.time():
                return row.data

    async def load_authorization_code(self, client, authorization_code):
        data = self.read_record(authorization_code, "code")
        if data and data["client_id"] == client.client_id:
            return AuthorizationCode(code=authorization_code, **data)

    def mint(self, s, client_id, scopes, resource):
        access, refresh, grant = (secrets.token_urlsafe(32) for _ in range(3))
        common = {"client_id": client_id, "scopes": scopes, "resource": resource,
                  "subject": "owner", "grant": grant}
        for raw, kind, seconds in ((access, "access", 3600), (refresh, "refresh", 30*86400)):
            expiry = int(time.time()) + seconds
            s.add(AuthRecord(key=digest(raw), kind=kind, expires_at=expiry,
                             data={**common, "expires_at": expiry}))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    def consume(self, s, raw, kind, client_id):
        row = s.execute(delete(AuthRecord).where(AuthRecord.key == digest(raw),
            AuthRecord.kind == kind, AuthRecord.expires_at > time.time()).returning(AuthRecord.data)).scalar_one_or_none()
        if not row or row["client_id"] != client_id:
            raise TokenError(error="invalid_grant", error_description="Grant expired or already used")
        return row

    async def exchange_authorization_code(self, client, authorization_code):
        with self.sessions.begin() as s:
            row = self.consume(s, authorization_code.code, "code", client.client_id)
            return self.mint(s, client.client_id, row["scopes"], row["resource"])

    async def load_refresh_token(self, client, refresh_token):
        data = self.read_record(refresh_token, "refresh")
        if data and data["client_id"] == client.client_id:
            return RefreshToken(token=refresh_token, **{k: v for k, v in data.items() if k != "grant"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self.sessions.begin() as s:
            row = self.consume(s, refresh_token.token, "refresh", client.client_id)
            if not set(scopes) <= set(row["scopes"]):
                raise TokenError(error="invalid_scope", error_description="Scope escalation denied")
            self.revoke_grant(s, row["grant"])
            return self.mint(s, client.client_id, scopes, row["resource"])

    async def load_access_token(self, token):
        data = self.read_record(token, "access")
        if data and data.get("resource") == self.resource:
            return AccessToken(token=token, **{k: v for k, v in data.items() if k != "grant"})

    @staticmethod
    def revoke_grant(s, grant):
        s.execute(delete(AuthRecord).where(AuthRecord.data["grant"].as_string() == grant))

    async def revoke_token(self, token):
        data = self.read_record(token.token, "access") or self.read_record(token.token, "refresh")
        if data:
            with self.sessions.begin() as s:
                self.revoke_grant(s, data["grant"])

    def login_page(self, ticket):
        csrf = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            row = s.scalar(select(AuthRecord).where(AuthRecord.key == digest(ticket)).with_for_update())
            if not row or row.kind != "pending" or row.expires_at < time.time() or row.data["attempts"] >= 5:
                raise Problem(400, "Connection expired. Start linking again in ChatGPT.")
            row.data = {**row.data, "csrf": digest(csrf)}
        response = HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Research Nexus</title>
<body><main><h1>Connect Research Nexus</h1><p>This private connection can read saved research and start or stop research jobs.</p>
<form method="post" action="/connect"><input type="hidden" name="ticket" value="{html.escape(ticket, quote=True)}">
<input type="hidden" name="csrf" value="{csrf}"><label>Owner password <input name="password" type="password" required autocomplete="current-password" maxlength="256"></label>
<button type="submit">Connect to ChatGPT</button></form></main></body></html>""",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        response.set_cookie("nexus_csrf", csrf, httponly=True, secure=self.settings.public_url.startswith("https"),
                            samesite="lax", path="/connect", max_age=300)
        return response

    def approve(self, ticket, csrf, cookie, password, origin):
        if origin and origin != self.settings.public_url:
            raise Problem(403, "Incorrect origin")
        error = None
        with self.sessions.begin() as s:
            row = s.scalar(select(AuthRecord).where(AuthRecord.key == digest(ticket)).with_for_update())
            if not row or row.kind != "pending" or row.expires_at < time.time() or row.data["attempts"] >= 5:
                raise Problem(400, "Connection expired. Start linking again in ChatGPT.")
            if not csrf or not cookie or not secrets.compare_digest(csrf, cookie) or not secrets.compare_digest(digest(csrf), row.data.get("csrf", "")):
                raise Problem(403, "Invalid connection form")
            if not secrets.compare_digest(password, self.settings.owner_password):
                row.data = {**row.data, "attempts": row.data["attempts"]+1}
                error = Problem(401, "Incorrect owner password")
            else:
                params = row.data["params"]
                code = secrets.token_urlsafe(32)
                s.add(AuthRecord(key=digest(code), kind="code", expires_at=time.time()+120,
                    data={"scopes": params["scopes"] or ["nexus"], "expires_at": time.time()+120,
                          "client_id": row.data["client_id"], "code_challenge": params["code_challenge"],
                          "redirect_uri": params["redirect_uri"], "resource": params["resource"],
                          "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"], "subject": "owner"}))
                s.delete(row)
        if error:
            raise error
        u = urlsplit(params["redirect_uri"])
        query = parse_qsl(u.query) + [("code", code)]
        if params.get("state") is not None:
            query.append(("state", params["state"]))
        response = RedirectResponse(urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), "")), status_code=303,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.delete_cookie("nexus_csrf", path="/connect")
        return response
