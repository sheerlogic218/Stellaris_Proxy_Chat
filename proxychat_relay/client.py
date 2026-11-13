"""Talking to the central server: pairing and signed requests, over HTTPS only.

Standard library only (urllib), so the relay needs just two packages. TLS
certificates are always checked. Redirects are never followed: a redirect could
send signed requests somewhere else. Plain http:// is refused except for
localhost with allow_insecure_local (tests and local trials).
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from proxychat_core import signing

from . import __version__

TIMEOUT = 15


class RelayError(Exception):
    """Something the host should be told about in plain words."""


class Revoked(RelayError):
    pass


class UpdateNeeded(RelayError):
    pass


class Busy(RelayError):
    """Rate limited or in maintenance: try again later."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class Unreachable(RelayError):
    pass


def check_url(url, allow_insecure_local=False):
    """The server URL without a trailing slash, or RelayError if it isn't acceptable."""
    if not isinstance(url, str) or len(url) > 300:
        raise RelayError("That isn't a server address.")
    parts = urllib.parse.urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme == "http" and local and allow_insecure_local:
        pass
    elif parts.scheme != "https" or not parts.hostname:
        raise RelayError("The server address must start with https://")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise RelayError("The server address can't contain a query, fragment or login.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


def _opener():
    context = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirects())


class Client:
    def __init__(self, server_url, key_id=None, private_key=None, allow_insecure_local=False):
        self.base = check_url(server_url, allow_insecure_local)
        self.key_id = key_id
        self.private_key = private_key
        self.opener = _opener()

    def _post(self, path, payload, signed=True):
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"proxychat-relay/{__version__}"}
        if signed:
            headers.update(signing.sign_headers(self.private_key, self.key_id, path, body))
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                return json.loads(response.read(64 * 1024) or b"{}")
        except urllib.error.HTTPError as e:
            raise self._error(e) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Unreachable(f"Can't reach the server: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise RelayError("The server sent something unexpected.") from None

    @staticmethod
    def _error(e):
        code = e.code
        if code == 401:
            return Revoked("The server didn't accept this relay. It may have been revoked, or the pairing "
                           "code was wrong or expired. Ask an admin for a new code with /proxychat host.")
        if code == 426:
            return UpdateNeeded("This relay is too old for the server. Please update it.")
        if code in (429, 503):
            retry = e.headers.get("Retry-After") if e.headers else None
            return Busy("The server is busy or in maintenance.", int(retry) if retry and retry.isdigit() else None)
        if 300 <= code < 400:
            return RelayError("The server tried to redirect; refusing. Check the server address.")
        return RelayError(f"The server refused the request ({code}).")

    # ---- the four calls ----

    def pair(self, code, public_key_bytes, label):
        return self._post("/v1/pair", {"code": code, "public_key": signing.b64e(public_key_bytes),
                                       "label": label, "relay_version": __version__}, signed=False)

    def events(self, launch, seq_from, records):
        return self._post("/v1/events", {"launch": launch, "seq_from": seq_from, "records": records})

    def heartbeat(self):
        return self._post("/v1/heartbeat", {})

    def release(self):
        return self._post("/v1/release", {})
