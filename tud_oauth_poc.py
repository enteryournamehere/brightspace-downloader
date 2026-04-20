import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

LANDLORD = "https://landlord.brightspace.com/v1/tenants"
INSTITUTION_SEARCH = "https://lms-disco.api.brightspace.com/institutions"
AUTH_HOST = "https://auth.brightspace.com"
AUTHORIZE = f"{AUTH_HOST}/oauth2/auth"
TOKEN = f"{AUTH_HOST}/core/connect/token"
GRAPHQL_ENDPOINT = "https://usergraph.api.brightspace.com/graphql"

OAUTH_CLIENT_ID = "73b7099f-d148-46f7-95cc-4b957cdf0f75"
OAUTH_REDIRECT = "brightspacepulse://auth"
OAUTH_SCOPE = "core:*:*"

ENROLLMENT_QUERY = """
query EnrollmentPage($id: String) {
  enrollmentPage(id: $id) {
    enrollments {
      id
      pinned
      startDate
      endDate
      organization {
        id
        name
        code
        homeUrl
        isActive
        semester { name }
      }
    }
    next
  }
}
"""

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "brightspace_pulse_poc"


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def search_institutions(query):
    """Return list of (name, domain) tuples matching query."""
    try:
        r = requests.get(INSTITUTION_SEARCH, params={"contains": query}, timeout=5)
        r.raise_for_status()
    except requests.RequestException:
        return []
    out = []
    for e in r.json().get("entities", []):
        name = e.get("properties", {}).get("name", "")
        for link in e.get("links", []):
            if "lms" in link.get("rel", []):
                out.append((name, urllib.parse.urlparse(link["href"]).netloc))
                break
    return out


def choose_domain():
    """Prompt the user for a Brightspace domain with live-search autocomplete
    against the institution discovery API. Falls back to plain input() if
    prompt_toolkit is unavailable."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
    except ImportError:
        return input("Brightspace domain (e.g. brightspace.tudelft.nl): ").strip()

    cache = {}

    class InstitutionCompleter(Completer):
        def get_completions(self, document, _event):
            q = document.text_before_cursor.strip()
            if len(q) < 2:
                return
            if q not in cache:
                cache[q] = search_institutions(q)
            for name, domain in cache[q]:
                yield Completion(
                    domain,
                    start_position=-len(document.text_before_cursor),
                    display=f"{domain}  ({name})",
                )

    session = PromptSession()
    return session.prompt(
        "Brightspace domain (type to search): ",
        completer=InstitutionCompleter(),
        complete_while_typing=True,
    ).strip()


def discover_tenant(domain):
    r = requests.get(LANDLORD, params={"domain": domain}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        sys.exit(f"No tenant found for {domain}")
    return data[0]["tenantId"]


def load_cached_token(tenant_id):
    path = CACHE_DIR / f"{tenant_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_cached_token(tenant_id, tok):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{tenant_id}.json").write_text(json.dumps(tok))


def gui_capture_redirect(url, redirect_prefix):
    """Open an embedded Qt webview and return the first URL beginning with
    redirect_prefix. Returns None if PyQt6 is not installed."""
    try:
        from PyQt6.QtCore import QUrl, QTimer
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile,
            QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
            QWebEngineUrlRequestJob,
        )
        from PyQt6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        return None

    scheme_name = redirect_prefix.split(":", 1)[0].encode()

    # Custom scheme must be registered before QApplication is constructed.
    # SecureScheme is required or Chromium blocks the https -> custom redirect
    # as mixed content and drops it silently.
    if QApplication.instance() is None:
        scheme = QWebEngineUrlScheme(scheme_name)
        scheme.setFlags(
            QWebEngineUrlScheme.Flag.SecureScheme
            | QWebEngineUrlScheme.Flag.CorsEnabled
        )
        QWebEngineUrlScheme.registerScheme(scheme)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    captured = {}

    profile = QWebEngineProfile("brightspace-pulse-poc", None)

    class SchemeHandler(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):
            u = job.requestUrl().toString()
            if u.startswith(redirect_prefix) and "url" not in captured:
                captured["url"] = u
                QTimer.singleShot(0, view.close)
            job.fail(QWebEngineUrlRequestJob.Error.RequestAborted)

    # Hold a Python reference - PyQt won't keep the handler alive otherwise.
    handler = SchemeHandler()
    profile.installUrlSchemeHandler(scheme_name, handler)

    view = QWebEngineView()
    view.setPage(QWebEnginePage(profile, view))
    view.setWindowTitle("Brightspace login")
    view.resize(520, 760)
    view.load(QUrl(url))
    view.show()
    app.exec()
    return captured.get("url")


def paste_capture_redirect(url):
    print("\nNOTICE: PyQt6 is not installed. The login process is much easier if you install it.")
    print("\nManual login:Open this URL, log in, and when the browser tries to open")
    print(f"'{OAUTH_REDIRECT}?...', copy that full URL from the address bar")
    print("and paste it below.\n")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return input("Paste redirect URL: ").strip()


def interactive_auth(tenant_id):
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT,
        "scope": OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "tenant_id": tenant_id,
    }
    url = f"{AUTHORIZE}?{urllib.parse.urlencode(params)}"

    pasted = gui_capture_redirect(url, OAUTH_REDIRECT) or paste_capture_redirect(url)

    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if q.get("state", [None])[0] != state:
        sys.exit("State mismatch - aborting")
    code = q.get("code", [None])[0]
    if not code:
        sys.exit(f"No code in redirect: {q}")

    r = requests.post(TOKEN, data={
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": OAUTH_REDIRECT,
        "code_verifier": verifier,
    }, timeout=15)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed: {r.status_code} {r.text}")
    tok = r.json()
    tok["_obtained_at"] = int(time.time())
    return tok


def refresh_token(tok):
    r = requests.post(TOKEN, data={
        "grant_type": "refresh_token",
        "client_id": OAUTH_CLIENT_ID,
        "refresh_token": tok["refresh_token"],
    }, timeout=15)
    if r.status_code != 200:
        return None
    new = r.json()
    new["_obtained_at"] = int(time.time())
    new.setdefault("refresh_token", tok["refresh_token"])
    return new


def get_access_token(tenant_id):
    tok = load_cached_token(tenant_id)
    if tok:
        age = time.time() - tok.get("_obtained_at", 0)
        if age < tok.get("expires_in", 0) - 60:
            return tok["access_token"]
        refreshed = refresh_token(tok)
        if refreshed:
            save_cached_token(tenant_id, refreshed)
            return refreshed["access_token"]
    tok = interactive_auth(tenant_id)
    save_cached_token(tenant_id, tok)
    return tok["access_token"]


def list_courses(access_token):
    enrollments = []
    next_id = None
    while True:
        r = requests.post(
            GRAPHQL_ENDPOINT,
            json={"query": ENROLLMENT_QUERY, "variables": {"id": next_id}},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if r.status_code != 200:
            sys.exit(f"GraphQL failed: {r.status_code} {r.text[:500]}")
        body = r.json()
        if "errors" in body:
            sys.exit(f"GraphQL errors: {body['errors']}")
        page = body["data"]["enrollmentPage"]
        enrollments.extend(page["enrollments"])
        next_id = page.get("next")
        if not next_id:
            break
    return enrollments


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain",
                    help="Brightspace site domain, e.g. brightspace.tudelft.nl. "
                         "If omitted, you'll be prompted with live search.")
    ap.add_argument("--logout", action="store_true",
                    help="Delete cached token and re-authenticate")
    args = ap.parse_args()

    domain = args.domain or choose_domain()
    if not domain:
        sys.exit("No domain provided.")

    tenant_id = discover_tenant(domain)
    print(f"tenantId: {tenant_id}")

    if args.logout:
        p = CACHE_DIR / f"{tenant_id}.json"
        if p.exists():
            p.unlink()
        print("Cached token cleared.")

    token = get_access_token(tenant_id)
    enrollments = list_courses(token)

    print(f"\n{len(enrollments)} enrollments:\n")
    print(f"{'ORG_UNIT_ID':>12}  Pinned?  Name")
    for e in enrollments:
        org_id = e["organization"]["id"].rsplit("/", 1)[-1]
        pin = "📌" if e.get("pinned") else "  "
        print(f"{org_id:>12}  {pin}       {e['organization']['name']}")


if __name__ == "__main__":
    main()
