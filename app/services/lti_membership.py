"""LTI 1.1 Names & Role Provisioning (Memberships extension).

When the Moodle tool has the "IMS LTI Names and Role Provisioning"
service enabled, the launch carries a ``custom_context_memberships_url``.
This module fetches the full course roster from that endpoint with an
OAuth 1.0 signed GET (body-hash of the empty body, as Moodle's
``handle_oauth_body_post`` requires) and returns the parsed members.
"""

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse

import requests

# Moodle emits the membership URL against its public wwwroot
# (http://localhost:8081). From inside the backend container "localhost"
# is the container itself, so we rewrite it to reach the Docker host.
# The signature base string uses the SAME rewritten host because Moodle
# reconstructs the URL from the incoming Host header (OAuth.php
# from_request), not from wwwroot.
def _rewrite_host(url: str, replacement_host: str) -> str:
    for local in ("localhost", "127.0.0.1"):
        url = url.replace(f"://{local}:", f"://{replacement_host}:")
        url = url.replace(f"://{local}/", f"://{replacement_host}/")
    return url


def _oauth_header(url: str, consumer_key: str, consumer_secret: str) -> str:
    # Body hash of the empty GET body — Moodle rejects the request
    # without a matching oauth_body_hash.
    body_hash = base64.b64encode(hashlib.sha1(b"").digest()).decode()

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
        "oauth_body_hash": body_hash,
    }

    # Signature base string: strip any query string from the URL for the
    # base URI, fold query params into the signed parameter set.
    parsed = urllib.parse.urlparse(url)
    base_uri = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
    )
    query_params = dict(urllib.parse.parse_qsl(parsed.query))

    all_params = {**oauth_params, **query_params}
    normalized = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(all_params.items())
    )
    base_string = "&".join([
        "GET",
        urllib.parse.quote(base_uri, safe=""),
        urllib.parse.quote(normalized, safe=""),
    ])
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = signature

    header = ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in oauth_params.items()
    )
    return f"OAuth {header}"


def fetch_members(
    memberships_url: str,
    consumer_key: str,
    consumer_secret: str,
    internal_host: str = "host.docker.internal",
    timeout: int = 10,
) -> list[dict]:
    """Return the course roster from an LTI 1.1 memberships endpoint.

    Each member dict has keys: ``name``, ``given_name``, ``family_name``,
    ``email``, ``roles`` (list of short IMS role names like "Instructor"
    / "Learner"). Missing fields are omitted by Moodle depending on the
    tool's privacy settings.

    Networking note: Moodle enforces its canonical wwwroot host. If we
    connect with a Host header it doesn't recognise it 302-redirects to
    wwwroot (``localhost:8081``), which is unreachable from inside this
    container. So we connect to the Docker host physically but keep the
    original ``Host`` header — Moodle then sees its own wwwroot, skips
    the redirect, and reconstructs the SAME signature base URL we sign
    below (it derives the URL from the Host header, not from wwwroot).
    """
    parsed = urllib.parse.urlparse(memberships_url)
    original_netloc = parsed.netloc  # e.g. localhost:8081

    # Physical connection target: swap only the hostname, keep the port.
    connect_url = _rewrite_host(memberships_url, internal_host)

    # Sign against the ORIGINAL url — that's what Moodle rebuilds from
    # the (preserved) Host header.
    auth = _oauth_header(memberships_url, consumer_key, consumer_secret)

    resp = requests.get(
        connect_url,
        headers={
            "Host": original_netloc,
            "Authorization": auth,
            # Ask for the flat v1 container — easiest to parse.
            "Accept": "application/vnd.ims.lis.v2.membershipcontainer+json",
        },
        timeout=timeout,
        allow_redirects=False,
    )
    resp.raise_for_status()
    data = resp.json()

    # Moodle's v1 container is JSON-LD, not a flat list:
    #   pageOf.membershipSubject.membership[] → { role: [...], member: {...} }
    # Normalise each entry to a flat dict with the keys the caller wants.
    raw = (
        data.get("pageOf", {})
        .get("membershipSubject", {})
        .get("membership", [])
    )
    members = []
    for entry in raw:
        person = entry.get("member", {})
        members.append({
            "name": person.get("name"),
            "given_name": person.get("givenName"),
            "family_name": person.get("familyName"),
            "email": person.get("email"),
            "ext_user_username": person.get("ext_user_username"),
            "roles": entry.get("role", []),
        })
    return members
