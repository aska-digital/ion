"""Opt-in GitHub App API authentication. No shell credential or PAT fallback."""
from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .ghclient import GHError

API = "https://api.github.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method, path, bearer, body=None):
    # Only this fixed origin receives credentials, including paginated requests.
    if not path.startswith("/") or path.startswith("//") or urllib.parse.urlsplit(path).netloc:
        raise GHError("App API path must be relative to api.github.com")
    request = urllib.request.Request(API + path, method=method,
        data=None if body is None else json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
            "User-Agent": "ion-github-app"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=120) as response:
            raw = response.read()
            return (json.loads(raw) if raw else None), dict(response.headers)
    except urllib.error.HTTPError as e:
        # Do not include response bodies, authorization headers or exception reprs.
        raise GHError(f"GitHub App API HTTP {e.code}", status=e.code) from None
    except Exception:
        raise GHError("GitHub App API request failed") from None


def _sign(payload: bytes, key_path: Path) -> bytes:
    try:
        result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)],
                                input=payload, capture_output=True, timeout=15)
        if result.returncode or not result.stdout:
            raise ValueError("signing failed")
        return result.stdout
    except Exception:
        raise GHError("GitHub App JWT signing failed (check RSA key and openssl)") from None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class AppGH:
    """API-only backend. Installation tokens stay in memory and refresh before expiry."""
    is_app = True
    supports_git = False

    def __init__(self, config, *, transport=None, signer=None, clock=None):
        self.client_id = config["client_id"]
        self.installation_id = config["installation_id"]
        self.key_path = Path(config["private_key_path"]).expanduser()
        self._transport = transport or _http
        self._signer = signer or _sign
        self._clock = clock or time.time
        self._token = None
        self._expires = 0
        self._login = None

    def _jwt(self):
        now = int(self._clock())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.client_id},
                                separators=(",", ":")).encode())
        unsigned = header + "." + claims
        try:
            signature = self._signer(unsigned.encode("ascii"), self.key_path)
        except Exception:
            raise GHError("GitHub App JWT signing failed") from None
        return unsigned + "." + _b64(signature)

    def _request(self, method, path, bearer, body=None):
        try:
            return self._transport(method, path, bearer, body)
        except GHError as e:
            # A custom transport must not leak token/key material through error text.
            raise GHError("GitHub App API request failed" + (f" (HTTP {e.status})" if e.status else ""), e.status) from None
        except Exception:
            raise GHError("GitHub App API request failed") from None

    def _installation_token(self):
        if self._token and self._clock() < self._expires - 60:
            return self._token
        self._token, self._expires = None, 0
        data, _ = self._request("POST", f"/app/installations/{self.installation_id}/access_tokens", self._jwt(), {})
        try:
            expires = dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            token = data["token"]
            if not isinstance(token, str) or not token or expires <= self._clock() + 60:
                raise ValueError("invalid token")
        except Exception:
            raise GHError("GitHub App returned an invalid installation token") from None
        self._token, self._expires = token, expires
        return token

    def _api(self, method, path, body=None):
        token = self._installation_token()
        try:
            return self._request(method, path, token, body)
        except GHError as e:
            if e.status == 401:
                self._token, self._expires = None, 0
            # Do not automatically replay mutating requests after uncertain failures.
            raise

    def rest(self, method, path, body=None):
        return self._api(method, path, body)[0]

    def graphql(self, query, variables=None):
        body, _ = self._api("POST", "/graphql", {"query": query, "variables": variables or {}})
        if not isinstance(body, dict) or "data" not in body:
            raise GHError("GitHub App GraphQL returned no data")
        return body

    def paginate(self, path):
        result, seen = [], set()
        while path:
            if path in seen:
                raise GHError("GitHub App pagination cursor repeated")
            seen.add(path)
            data, headers = self._api("GET", path)
            if not isinstance(data, list):
                raise GHError("GitHub App expected a paginated array")
            result.extend(data)
            link = next((v for k, v in headers.items() if k.lower() == "link"), "")
            next_page = re.search(r'<([^>]+)>;\s*rel="next"', link)
            path = None
            if next_page:
                url = urllib.parse.urlsplit(next_page.group(1))
                if url.scheme != "https" or url.netloc != "api.github.com":
                    raise GHError("GitHub App refused pagination outside api.github.com")
                path = url.path + ("?" + url.query if url.query else "")
        return result

    def token_login(self):
        if self._login is None:
            data, _ = self._request("GET", "/app", self._jwt())
            slug = (data or {}).get("slug")
            if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9-]+", slug):
                raise GHError("GitHub App identity could not be verified")
            self._login = slug.lower() + "[bot]"
        return self._login
