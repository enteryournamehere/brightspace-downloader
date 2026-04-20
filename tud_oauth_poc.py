"""
Brightspace course lister via the Pulse GraphQL API.
Works on any Brightspace tenant (not just TU Delft) because tenantId is
discovered at runtime from the site's domain.

===== How the constants below were obtained =====

The Pulse Android app (package com.d2l.brightspace.student) is a React Native
app; its JS bundle lives at assets/index.android.bundle and is Hermes bytecode
but contains plain-text strings.

  1. Get the APK from APKMirror (package: com.d2l.brightspace.student).
     .apkm bundles are just ZIPs - unzip, then unzip base.apk.
  2. strings -n 6 base/assets/index.android.bundle > pulse.strings

Constants surfaced this way:

  TENANT_DISCOVERY   strings hit:  "https://lms-disco.api.brightspace.com"
                                   ".../v1/tenants?domain="
                     The *working* host turned out to be landlord.brightspace.com
                     (lms-disco rejects /v1/tenants but serves /institutions).
                     Confirm:  curl 'https://landlord.brightspace.com/v1/tenants?domain=<your-brightspace-domain>'

  AUTH_HOST          strings hit:  "auth.brightspace.com", "/oauth2/auth",
                                   "/core/connect/token"
                     Kestrel server = IdentityServer4.

  OAUTH_CLIENT_ID    Found by enumerating GUID-shaped strings in the bundle
                     and probing /oauth2/auth with each until one returned
                     "Multi-tenant clients must specify tenant_id" instead of
                     "unauthorized_client". Two IDs matched; only the one
                     below also accepts redirect_uri=brightspacepulse://auth.

  OAUTH_SCOPE        strings hit:  "core:*:*", "core:*:*:read".
                     Only "core:*:*" is accepted at the authorize endpoint.

  OAUTH_REDIRECT     strings hit:  "brightspacepulse://".
                     Enumerating paths found brightspacepulse://auth is the
                     one the server whitelists for this client.

  GRAPHQL_ENDPOINT   strings hit:  "usergraph.api.brightspace.com/graphql".

  GRAPHQL_QUERY      extracted verbatim from bundle strings around the
                     "query EnrollmentPage" token.

===== Flow =====

  1. Look up tenantId from domain (landlord).
  2. Build authorize URL (auth code + PKCE + tenant_id).
  3. Open it in the user's browser. They log in via the tenant's SSO.
  4. The final redirect is brightspacepulse://auth?code=...&state=...
     The browser can't open that scheme, so the user copies the URL from
     the address bar and pastes it back here.
  5. Exchange code for access_token + refresh_token at /core/connect/token.
  6. Post the EnrollmentPage GraphQL query with the bearer token.

Refresh token is cached in ~/.cache/brightspace_pulse_poc/<tenantId>.json so
subsequent runs skip the browser step.
"""

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

DEBUG_GUI = False


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair():
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


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


def gui_capture_redirect(url, redirect_prefix, debug=False):
    """Open an embedded Qt webview and return the first URL beginning with
    redirect_prefix. Returns None if PyQt6 is not installed."""
    try:
        from PyQt6.QtCore import QUrl, QLoggingCategory, QTimer
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtWebEngineCore import (
            QWebEnginePage, QWebEngineProfile, QWebEngineUrlScheme,
            QWebEngineUrlRequestInterceptor, QWebEngineUrlSchemeHandler,
            QWebEngineUrlRequestJob,
        )
        from PyQt6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        return None

    def log(*a):
        if debug:
            print("[webview]", *a, file=sys.stderr)

    if debug:
        QLoggingCategory.setFilterRules("qt.webenginecontext.debug=true")

    # Register the custom scheme BEFORE QApplication is constructed.
    # Without this, Qt rejects 302 Location: brightspacepulse://... at the
    # network layer and acceptNavigationRequest never fires for it.
    scheme_name = redirect_prefix.split(":", 1)[0].encode()
    if QApplication.instance() is None:
        scheme = QWebEngineUrlScheme(scheme_name)
        # SecureScheme is essential - without it, Chromium blocks the
        # https -> brightspacepulse:// redirect as mixed/insecure content.
        scheme.setFlags(
            QWebEngineUrlScheme.Flag.SecureScheme
            | QWebEngineUrlScheme.Flag.LocalAccessAllowed
            | QWebEngineUrlScheme.Flag.CorsEnabled
        )
        QWebEngineUrlScheme.registerScheme(scheme)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    captured = {}
    extra_views = []

    profile = QWebEngineProfile("brightspace-pulse-poc", None)
    # Microsoft/Entra blocks embedded-browser user agents. Spoof desktop Chrome.
    profile.setHttpUserAgent(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    class Interceptor(QWebEngineUrlRequestInterceptor):
        # Fires for every request AND every redirect target, regardless of
        # scheme support. This is a backstop in case acceptNavigationRequest
        # misses the final brightspacepulse:// hop.
        def interceptRequest(self, info):
            u = info.requestUrl().toString()
            log("intercept ->", u)
            try_capture(u)

    # Hold a Python reference - PyQt won't keep the interceptor alive otherwise.
    interceptor = Interceptor()
    profile.setUrlRequestInterceptor(interceptor)
    log("interceptor installed")

    class SchemeHandler(QWebEngineUrlSchemeHandler):
        # Registering the scheme alone isn't enough - Chromium drops navigations
        # to it without a handler. installing this handler makes Chromium
        # actually *start the request*, so we see the full URL + query here.
        def requestStarted(self, job):
            u = job.requestUrl().toString()
            log("scheme handler ->", u)
            try_capture(u)
            job.fail(QWebEngineUrlRequestJob.Error.RequestAborted)

    scheme_handler = SchemeHandler()
    profile.installUrlSchemeHandler(scheme_name, scheme_handler)
    log("scheme handler installed for", scheme_name.decode())

    def close_all():
        for v in [view, *extra_views]:
            v.close()

    def try_capture(u):
        if u.startswith(redirect_prefix) and "url" not in captured:
            captured["url"] = u
            log("captured redirect:", u)
            # Defer close to UI thread - interceptor may run off it.
            QTimer.singleShot(0, close_all)
            return True
        return False

    class LoggingPage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):
            log(f"JS console [{level}] {source}:{line}: {message}")

        def certificateError(self, err):
            log("cert error:", err.description())
            return False

        def acceptNavigationRequest(self, url, _type, _isMainFrame):
            # Fires BEFORE Qt cancels navigation to unknown schemes like
            # brightspacepulse://. urlChanged never fires in that case.
            u = url.toString()
            log("nav request ->", u)
            if try_capture(u):
                return False
            return True

        def createWindow(self, _type):
            # Popup windows (common in SSO flows) get a real view so they render.
            sub = QWebEngineView()
            sub.setPage(LoggingPage(profile, sub))
            sub.page().urlChanged.connect(on_url_changed)
            sub.page().loadStarted.connect(lambda: log("popup load started"))
            sub.page().loadFinished.connect(lambda ok: log(f"popup load finished ok={ok}"))
            sub.resize(520, 760)
            sub.setWindowTitle("Brightspace login (popup)")
            sub.show()
            extra_views.append(sub)
            return sub.page()

    def on_url_changed(qurl):
        u = qurl.toString()
        log("url ->", u)
        try_capture(u)

    view = QWebEngineView()
    view.setPage(LoggingPage(profile, view))
    view.setWindowTitle("Brightspace login")
    view.resize(520, 760)
    view.page().urlChanged.connect(on_url_changed)
    view.page().loadStarted.connect(lambda: log("load started"))
    view.page().loadFinished.connect(lambda ok: log(f"load finished ok={ok}"))
    view.load(QUrl(url))
    view.show()
    app.exec()
    return captured.get("url")


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

    pasted = gui_capture_redirect(url, OAUTH_REDIRECT, debug=DEBUG_GUI)
    if not pasted:
        print("\n(PyQt6 not installed - falling back to manual paste.)")
        print("Open this URL, log in, and when the browser tries to open")
        print(f"'{OAUTH_REDIRECT}?...', copy that full URL from the address bar")
        print("and paste it below.\n")
        print(url)
        print()
        try:
            webbrowser.open(url)
        except Exception:
            pass
        pasted = input("Paste redirect URL: ").strip()
    parsed = urllib.parse.urlparse(pasted)
    q = urllib.parse.parse_qs(parsed.query)
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
    if "refresh_token" not in new:
        new["refresh_token"] = tok["refresh_token"]
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
    ap.add_argument("--domain", required=True,
                    help="Brightspace site domain, e.g. brightspace.tudelft.nl")
    ap.add_argument("--logout", action="store_true",
                    help="Delete cached token and re-authenticate")
    ap.add_argument("--debug-gui", action="store_true",
                    help="Log webview navigation, popups, JS console")
    args = ap.parse_args()

    global DEBUG_GUI
    DEBUG_GUI = args.debug_gui

    tenant_id = discover_tenant(args.domain)
    print(f"tenantId: {tenant_id}")

    if args.logout:
        p = CACHE_DIR / f"{tenant_id}.json"
        if p.exists():
            p.unlink()
        print("Cached token cleared.")

    token = get_access_token(tenant_id)
    enrollments = list_courses(token)

    print(f"\n{len(enrollments)} enrollments:\n")
    print(f"{'ORG_UNIT_ID':>12}  PIN  NAME")
    for e in enrollments:
        o = e["organization"]
        pin = "*" if e.get("pinned") else " "
        print(f"{o['id']:>12}  {pin}   {o['name']}")


if __name__ == "__main__":
    main()
