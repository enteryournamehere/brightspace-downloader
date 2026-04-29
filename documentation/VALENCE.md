# Brightspace Valence REST API

The Pulse app uses two APIs: the central GraphQL service at
`usergraph.api.brightspace.com/graphql` (see [GRAPHQL.md](GRAPHQL.md)) and
the tenant's own **Valence** REST API at
`https://<your-brightspace-domain>/d2l/api/...`. GraphQL covers navigation
and metadata; whenever Pulse needs the actual bytes of a file, the numeric
value of a grade, or anything the GraphQL schema doesn't expose for the
current user, it falls back to Valence.

Official Valence docs: <https://docs.valence.desire2learn.com/>. This file
only notes the endpoints that matter for a Pulse-scoped token, plus some quirks that came up while building this tool.

## Authentication

Valence accepts the same OAuth bearer token issued to the Pulse client
(see `auth.py` in this repo). Pulse requests three scopes:

```
core:*:*  content:topics:read  content:file:read
```

That covers content download and everything documented below. Many other
Valence endpoints return **403 "No scopes defined for specified
requests"** with this token, e.g. `/dropbox/.../submissions/mysubmissions/`,
`/quizzes/`, `/discussions/`, `/classlist/`. They'd need additional
scopes (`dropbox:*:read`, `quizzing:*:read`, ...) that the Pulse client
isn't whitelisted for.

## API versioning

Every path is prefixed with a product code and version:

* `lp` (Learning Platform): users, enrollments, org units, etc.
* `le` (Learning Environment): content, grades, dropbox, news, calendar.
* `bas`, `ep`, `customization`, ... less commonly needed.

Discover the live versions with:

```
GET /d2l/api/versions/
```

At time of writing, TU Delft's server reports `lp` 1.58 and `le` 1.92 as
the latest. Older versions continue to work; Pulse itself pins to `1.40`
/ `1.43`, and everything in this file works against `1.40`+ unless noted.

An unstable channel also exists (`/d2l/api/le/unstable/...`) and tends to
expose extra fields (e.g. `UserId`/`OrgUnitId` on grade values). Use the
pinned version in production code.

---

## Endpoints that work with a Pulse-scoped learner token

### Identity

```
GET /d2l/api/lp/1.43/users/whoami
```

Returns `{ Identifier, FirstName, LastName, UniqueName, ProfileIdentifier, ... }`.
`Identifier` is the numeric D2L user id (same as the JWT `sub`).

### Enrollments

```
GET /d2l/api/lp/1.43/enrollments/myenrollments/?pageSize=...&bookmark=...
```

Paginated. Response shape: `{ PagingInfo: { Bookmark, HasMoreItems }, Items: [...] }`.
Each item has `{ OrgUnit, Access, PinDate }`, where `OrgUnit` includes the
numeric id, a `Type` (e.g. `{ Id: 3, Code: "Course Offering" }`), code,
name, and start/end dates.

Equivalent to GraphQL's `enrollmentPage`, but includes staff roles and
does not require `isLearner=true`.

### Course content

GraphQL is the more ergonomic path here (see `graphql.py`), but the
Valence equivalents for individual topics work and are what Pulse uses to
download bytes:

```
GET /d2l/api/le/1.40/<ouId>/content/topics/<topicId>        # JSON metadata
GET /d2l/api/le/1.40/<ouId>/content/topics/<topicId>/file   # raw bytes
```

The `TypeIdentifier` field on the metadata object tells you how to handle
the topic: `"File"` (stream `/file`), `"Link"` (write a shortcut to the
`Url` field), `"HTML"` (render inline), ...

`/d2l/api/le/.../content/root/` and `/.../content/modules/` return **403**
for the Pulse scope. Walk the tree via GraphQL instead.

### Grades

```
GET /d2l/api/le/1.40/<ouId>/grades/values/myGradeValues/
GET /d2l/api/le/1.40/<ouId>/grades/categories/
```

Returns a list of `{ PointsNumerator, PointsDenominator, DisplayedGrade,
GradeObjectIdentifier, GradeObjectName, GradeObjectType, GradeObjectTypeName,
Comments, PrivateComments, LastModified, ReleasedDate }`.

`GradeObjectType` is an enum (`1 = Numeric`, `2 = PassFail`, `3 = SelectBox`,
`4 = Text`, `9 = Category`, ...). `Comments.Html` often contains the
instructor's feedback rendered as HTML.

This is how Pulse actually fetches grades, even when the GraphQL
`userGrades` field *should* work. On at least TU Delft, the GraphQL
field returns `{"data": null}` for every course with
`hasGradesEnabled=true` (see GRAPHQL.md). It isn't clear whether that's
specific to TU Delft's configuration or a wider Pulse backend issue;
other tenants may behave differently. The Valence endpoint above works
reliably on both released and unreleased grades (`ReleasedDate` is
exposed per entry so you can filter if you want).

### Assignments (dropbox folders)

```
GET /d2l/api/le/1.74/<ouId>/dropbox/folders/
```

Returns every assignment folder in the course with due dates, allowed
file types, submission/completion types, `ActivityId`, `GradeItemId`, and
totals. Useful for surfacing due-dates without touching GraphQL's
`activities` query (which is also `isLearner`-gated).

Submission endpoints (`.../submissions/mysubmissions/`) require extra
scopes not granted to Pulse and return 403.

### Announcements (news)

```
GET /d2l/api/le/1.74/<ouId>/news/
```

Returns `[ { Id, Title, Body, Attachments, CreatedBy, CreatedDate,
LastModifiedDate, StartDate, EndDate, IsPinned, IsHidden, IsGlobal,
IsPublished, ... } ]`. This is the full feed per course. GraphQL has
`announcement(id)` for lookups but no list query.

### Calendar

```
GET /d2l/api/le/1.74/<ouId>/calendar/events/
```

Returns every event visible in the course calendar:
`{ CalendarEventId, OrgUnitId, Title, Description, StartDateTime,
EndDateTime, IsAllDay, RecurrenceInfo, ... }`.

Per-user views (`.../myCalendarEvents/`) 404 on at least TU Delft. Prefer
the GraphQL `events(...)` / `eventsWithOccurrences(...)` queries if you
want a global feed across all enrollments in one call.

---

## Endpoints that return 403 with the Pulse scope

These appear in the Valence docs and might be tempting, but will not
work with a Pulse-issued token. Listed so you don't re-discover each the
hard way:

| Path                                             | Status | Why        |
|--------------------------------------------------|--------|------------|
| `/lp/{v}/courses/<id>`                           | 403    | Instructor scope |
| `/lp/{v}/enrollments/orgUnits/<ou>/users/`       | 403    | Instructor scope |
| `/le/{v}/<ou>/content/root/` / `content/toc`     | 403    | Instructor scope |
| `/le/{v}/<ou>/dropbox/.../submissions/mysubmissions/` | 403 | Needs `dropbox:*:read` |
| `/le/{v}/<ou>/quizzes/`                          | 403    | Needs `quizzing:*:read` |
| `/le/{v}/<ou>/discussions/forums/`               | 403    | Needs `discussions:*:read` |
| `/le/{v}/<ou>/classlist/`                        | 403    | Instructor scope |
| `/le/{v}/<ou>/checklists/`                       | 403    | Needs `checklists:*:read` |

If you need these, you have two options:

1. Ship your own OAuth client registered with D2L for the right scopes.
2. Reuse the session cookie of a logged-in browser (what `/d2l/le/...`
   uses). That works for anything the user sees in the web UI, but
   conflates auth with the browser and is messier to automate.

---

## Error shape

Non-200 responses are mostly JSON. Common shapes:

```json
{"type": "http://docs.valence.desire2learn.com/res/apiprop.html#invalid-parameters",
 "title": "Invalid Parameters", "status": 400, "detail": "..."}
```

or

```json
{"Errors": [{"Message": "No scopes defined for specified requests."}]}
```

HTTP 404 sometimes indicates a valid path with no data (e.g. no calendar
events) and sometimes a wrong path. Don't rely on status codes alone; the
body usually clarifies.

## Notes / tips

* **Trailing slash matters** on some paths. `/myGradeValues/` works,
  `/myGradeValues` sometimes 404s depending on the version.
* **IDs are numeric**, unlike GraphQL's opaque URL ids. If you already
  hold the GraphQL id for a topic, parse the last path segment to get
  the Valence id; the preceding segment is the `ouId`.
* **Pagination** uses `bookmark` query params on `lp/*` and varies
  elsewhere. Always check for `PagingInfo.HasMoreItems`.
* **`unstable` vs pinned version**: unstable may expose extra fields but
  can break without notice. The Pulse app pins specific versions; we do
  the same.
