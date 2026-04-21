# Universal Brightspace Downloader

Terminal tool to download course files from any Brightspace / D2L Learning Environment. Download your lectures, slides, PDFs, and other course files to your local machine. Pick the folders and files you care about using the interactive terminal interface, then run `download.py` to update your local copy.

_Should_ work against any Brightspace instance, but I've only been able to test it against TU Delft. If you try it for another institution, please open an issue to tell me whether it works, and, in case it doesn't, what errors you get.

This tool authenticates as the **Brightspace Pulse** mobile app using OAuth2 + PKCE, so login goes through your institution's normal SSO flow.

Largly built using Claude Code. I have not thoroughly manually verified the documentation it generated for the GraphQL and Valence APIs, but the tool works, and that was the main goal here. So if you find any inaccuracies in the docs feel free to open an issue or PR.

---

## Quick start

```bash
pip install -r requirements.txt
python3 configure.py
```

On first run you'll be prompted for your Brightspace domain (e.g. `brightspace.tudelft.nl`, `brightspace.universiteitleiden.nl`). A browser window opens for SSO; after logging in, the main menu appears:

```
=== Brightspace downloader ===

  Domain : brightspace.tudelft.nl
  Marked : 0 course(s), 0 folder(s), 0 file(s)
  Config : /home/you/.config/brightspace_downloader/config.json

[e]dit selection  [d]ownload  [q]uit >
```

- **e**: opens a TUI. Filter courses, press enter to open one, navigate the content tree with arrow keys, press **space** to mark a folder (recursive) or individual file for download.
- **d**: downloads everything marked into `./brightspace_sync/<course>/<folder>/...`. Re-runs skip files whose size already matches.
- **q**: quit.

You can also run the downloader directly:

```bash
python3 download.py --out ~/Courses
```

## Files written

```
<out>/
  <Course Name>/
    <Module>/<Submodule>/<file.pdf>
    <Module>/<Link Topic>.url
```

Filenames are sanitised (`<>:"/\|?*` and control chars replaced with `_`).

## Configuration

- **Domain + marked items**: `~/.config/brightspace_downloader/config.json`
- **Access/refresh token** (per tenant): `~/.cache/brightspace_downloader/<tenantId>.json`

Deleting the cache file forces a fresh login.

## Requirements

- Python 3.9+
- `requests`, `textual`, `prompt_toolkit`
- `PyQt6` + `PyQt6-WebEngine` *(optional but highly recommended)* for the embedded login window. Without PyQt6 you'll be asked to paste the redirect URL from a normal browser; everything still works.

## Project layout

| File            | Purpose                                                           |
|-----------------|-------------------------------------------------------------------|
| `configure.py`  | Main entry point: menu + Textual TUI for picking what to download |
| `download.py`   | Walks marked folders, downloads files, also runnable standalone   |
| `auth.py`       | OAuth2 + PKCE login, tenant discovery, token cache                |
| `graphql.py`    | Brightspace Pulse GraphQL client and queries                      |
| `config.py`     | Config load/save and target normalisation                         |
| `GRAPHQL.md`    | Reference for the Pulse GraphQL schema (as observed)              |
| `VALENCE.md`    | Notes on the tenant's Valence REST API (what Pulse falls back to) |

---

# How it works

Included in case you want to make your own client...

## Endpoints

| Purpose             | Host / path                                                           |
|---------------------|-----------------------------------------------------------------------|
| Institution search  | `GET https://lms-disco.api.brightspace.com/institutions?contains=...` |
| Tenant lookup       | `GET https://landlord.brightspace.com/v1/tenants?domain=<domain>`     |
| Authorize           | `https://auth.brightspace.com/oauth2/auth`                            |
| Token exchange      | `POST https://auth.brightspace.com/core/connect/token`                |
| GraphQL (Pulse)     | `POST https://usergraph.api.brightspace.com/graphql`                  |
| File metadata       | `GET https://<domain>/d2l/api/le/1.40/<ouId>/content/topics/<topicId>` |
| File bytes          | `GET https://<domain>/d2l/api/le/1.40/<ouId>/content/topics/<topicId>/file` |

The auth, tenant-lookup, and GraphQL hosts are the same for every Brightspace tenant. Your specific tenant is identified by a `tenant_id` passed on the authorize request and baked into every id returned by the API.

## OAuth client

This tool authenticates as the Pulse mobile app:

| Field          | Value                                                          |
|----------------|----------------------------------------------------------------|
| Client ID      | `73b7099f-d148-46f7-95cc-4b957cdf0f75`                         |
| Redirect URI   | `brightspacepulse://auth`                                      |
| Grant          | Authorization Code + PKCE (`S256`)                             |
| Scope          | `core:*:* content:topics:read content:file:read`               |
| Extra param    | `tenant_id=<tenantId>` on `/oauth2/auth`                       |

D2L scopes follow the format `service:resource:action`. `core:*:*` alone is enough for GraphQL; the two `content:*` scopes are only required for the Valence file-download endpoint (without them it returns 403 *Insufficient scope*).

## Login flow

1. Resolve the user's Brightspace domain to a `tenant_id` via `landlord.brightspace.com`.
2. Build an authorize URL with PKCE and the `tenant_id` query parameter.
3. An embedded Qt `WebEngineView` loads it; the user completes SSO at their institution. The final 302 points at `brightspacepulse://auth?code=...`, caught via a registered custom URL scheme handler. *(Chromium blocks the `https → custom-scheme` redirect as mixed content unless the scheme is registered with `SecureScheme` before `QApplication` is constructed.)*
4. Exchange the code for an access token + refresh token at `/core/connect/token`.
5. Cache the tokens; refresh silently on the next run.

If PyQt6 isn't installed, the authorize URL is opened in your default browser and you paste the final `brightspacepulse://auth?code=...` URL back into the terminal.

## Reading the course tree

GraphQL (`usergraph.api.brightspace.com/graphql`) serves enrollments and content metadata. The relevant shape:

- `enrollmentPage(id)`: paginated list of the user's courses. `id` is a pagination cursor; pass `null` for the first page.
- `contentRoot(organizationId)`: top-level modules of a course. Returns one level of children.
- `contentModule(moduleId)`: a folder and its direct children (sub-modules + topics). Not recursive; you have to walk it yourself.
- `ContentTopic`: a leaf (file, link, HTML page, ...). Fields `viewHref`, `downloadHref`, `pdfHref`, `type`, `fileName`. `downloadHref` is frequently `null` even for files.

Entity ids are fully-qualified URLs containing the tenant id and numeric D2L id. Treat them as opaque and pass them back unchanged.

See [GRAPHQL.md](GRAPHQL.md) for the full schema reference.

## When GraphQL isn't enough

The Pulse app uses two APIs in parallel: the central GraphQL service for navigation and metadata, and the tenant's own **Valence REST API** (`https://<your-domain>/d2l/api/...`) for actual file bytes and for anything the GraphQL resolvers don't return reliably. With a Pulse-scoped token you can also reach grades, announcements, assignments (dropbox folders), and the course calendar over REST. In particular, GraphQL's `userGrades` returns `{"data": null}` on at least TU Delft for every course with grades enabled, so Pulse falls back to `/d2l/api/le/<v>/<ouId>/grades/values/myGradeValues/`. Full list of verified endpoints, behaviour, and scope limitations in [VALENCE.md](VALENCE.md).

## Downloading files

The Pulse GraphQL schema exposes metadata but not the actual bytes. For the download we switch to the Valence REST API on the user's Brightspace domain:

```
GET https://<domain>/d2l/api/le/1.40/<ouId>/content/topics/<topicId>          # metadata (JSON)
GET https://<domain>/d2l/api/le/1.40/<ouId>/content/topics/<topicId>/file     # raw bytes
```

Topic ids from GraphQL embed both `ouId` and `topicId` in the URL path, so no extra lookup is needed. The metadata response carries a `TypeIdentifier` field. If it's `"Link"` there's no `/file` endpoint; for those we write a `.url` Windows-style shortcut using the `Url` field. For everything else we stream `/file` and take the filename from the `Content-Disposition` header.

Re-runs are idempotent: if the local file's size already matches the `Content-Length`, the download is skipped.

## Related reading

- [GRAPHQL.md](GRAPHQL.md): observed Pulse GraphQL schema and queries.
- [VALENCE.md](VALENCE.md): Valence REST endpoints that work with a Pulse-scoped token (and which ones 403).
- D2L Valence docs: <https://docs.valence.desire2learn.com/>.
