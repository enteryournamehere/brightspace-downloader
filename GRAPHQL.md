# Brightspace GraphQL API

This document describes the undocumented GraphQL API used by the official
Brightspace Pulse mobile app. It is hosted centrally by D2L (not per-tenant)
and exposes course enrollments, content hierarchy, grades, activities, events,
announcements, the class-stream activity feed, and push-notification settings.

## Endpoint

```
POST https://usergraph.api.brightspace.com/graphql
Content-Type: application/json
Authorization: Bearer <access_token>
```

The endpoint is the same for every Brightspace tenant. Which tenant the call
resolves to is determined purely by the access token: tokens are issued for
one `tenant_id`, and every id returned by the API is a tenant-scoped URL
containing that tenant id (e.g.
`https://<tenantId>.organizations.api.brightspace.com/12345`).

Introspection is enabled, so the schema can be refetched at any time with the
standard `IntrospectionQuery`.

## Authentication

Tokens are obtained via OAuth2 Authorization Code + PKCE against
`https://auth.brightspace.com` using the Pulse client. See `auth.py` in
this directory for a runnable flow. The relevant constants:

| Field           | Value                                                                     |
|-----------------|---------------------------------------------------------------------------|
| Authorize URL   | `https://auth.brightspace.com/oauth2/auth`                                |
| Token URL       | `https://auth.brightspace.com/core/connect/token`                         |
| Client ID       | `73b7099f-d148-46f7-95cc-4b957cdf0f75` (Pulse)                            |
| Redirect URI    | `brightspacepulse://auth`                                                 |
| Scope           | `core:*:* content:topics:read content:file:read` (format `service:resource:action`). The `content:*` scopes are required for the Valence `/content/topics/.../file` download endpoint; the GraphQL endpoint itself is happy with `core:*:*` alone. |
| Extra param     | `tenant_id=<tenantId>` on the `/oauth2/auth` request                      |
| PKCE            | required (`code_challenge_method=S256`)                                   |

The tenant id for a given Brightspace domain is resolved via
`GET https://landlord.brightspace.com/v1/tenants?domain=<domain>`.
Institution/domain discovery is at
`GET https://lms-disco.api.brightspace.com/institutions?contains=<query>`.

Tokens are short-lived (1 hour) and refreshable.

## Overall shape

* One root `Query` and one root `Mutation`. No subscriptions.
* Every entity id is a **fully-qualified URL** containing the tenant id
  and numeric D2L id. The URLs are opaque; pass them back as-is when a
  field expects an id, don't try to construct them yourself.
* Nullability is meaningful: many fields on `ContentTopic` (`type`,
  `fileName`, `downloadHref`, `pdfHref`, `modifiedDate`) are frequently
  `null` in practice even for files. The Pulse app falls back to
  `viewHref` (a regular `/d2l/le/content/.../View` web URL) in that case.
* Several query fields listed in the schema as nullable (`id:String`) are
  enforced as required server-side. Pass `null` rather than omitting.

---

## Queries

Queries are grouped by purpose. Argument types follow the schema exactly
(`!` = non-null).

### Identity & organization

| Query | Returns | Notes |
|---|---|---|
| `user` | `User` | The authenticated user (id, displayName, firstName, lastName, imageUrl, isLearner, isParent, parentPortalPath). |
| `rootOrganization` | `Organization` | The tenant's root org unit (e.g. "Delft University of Technology"). |
| `organization(id: String!)` | `Organization` | Fetch a specific org unit by URL id. |
| `consortiumOrganizations` | `[ConsortiumOrganization!]!` | For users who belong to multiple tenants via a consortium. |

### Enrollments (courses)

| Query | Returns | Notes |
|---|---|---|
| `enrollmentPage(id: String)` | `UserEnrollmentPage` | Paginated list of the user's enrollments. Pass `null` to get the first page, then pass the returned `next` cursor. |
| `enrollment(id: String!)` | `UserEnrollment` | A single enrollment. |

An `Organization` returned by an enrollment is the course itself. Its `id`
is what you pass to content/grade queries.

### Course content (folders, files, links)

The content tree is **three typed layers**:

```
ContentRoot             (per course)
  └─ ContentModule      (folder)     ← can nest recursively
       └─ ContentItem   (union: ContentModule | ContentTopic)
            └─ ContentTopic  (leaf: file, link, HTML page, SCORM, etc.)
```

| Query | Returns | Notes |
|---|---|---|
| `contentRoot(organizationId: String!)` | `ContentRoot!` | Top-level modules for a course. `organizationId` is the course's `Organization.id`. |
| `contentModule(moduleId: String!)` | `ContentModule!` | A folder and its direct `children` (sub-modules + topics). **Not recursive**; you must walk it yourself. |
| `contentTopic(topicId: String!)` | `ContentTopic!` | A single leaf item (file/link/page). |

`ContentModule.children` is typed as `[ContentItem!]!`, where `ContentItem`
is an interface with two implementers:

* `ContentModule` is a nested folder. Same fields as above. Use a GraphQL
  inline fragment `... on ContentModule { id title }` and recurse via
  `contentModule(moduleId: ...)` to descend.
* `ContentTopic` is a leaf. Key fields:
  * `viewHref` / `viewUrl`: web URL of the form
    `https://<domain>/d2l/le/content/<ouId>/viewContent/<topicId>/View`.
    Always populated; works for every topic type.
  * `downloadHref`: direct download URL. **Often `null`** even for files;
    when present it points to the raw asset.
  * `pdfHref`: URL of a PDF rendering, when D2L has one.
  * `type`, `fileName`, `modifiedDate`: frequently `null`; don't rely
    on them for classification. Use `iconHref` as a weak hint or parse
    the `viewHref` target.
  * `isComplete`: completion state for the current user.

### Grades

| Query | Returns | Notes |
|---|---|---|
| `userGrades(organization: String!)` | `[UserGrade!]!` | All of the current user's grade entries for a course. **Unreliable in practice, tenant-dependent**, see warning below. |
| `userGrade(gradeId: String!)` | `UserGrade` | A single grade item (by GraphQL id). Same caveat. |

> **Heads up:** On at least one tenant (TU Delft) `userGrades` returns
> `{"data": null}` for every course where `organization.hasGradesEnabled`
> is `true`, regardless of whether the user actually has grade entries. Courses with `hasGradesEnabled=false`
> correctly return `[]`, so the query itself is valid.
>
> The Pulse app on the same account shows grades fine because Pulse
> doesn't actually use this field, it reads
> `/d2l/api/le/<v>/<ouId>/grades/values/myGradeValues/` over the tenant's
> Valence REST API. See [VALENCE.md](VALENCE.md) for that endpoint.
>
> I don't know whether this is a TU Delft misconfiguration, a bug in
> the Pulse GraphQL backend, or something more general. If you're
> targeting another tenant, test both paths; `userGrades` may work
> there. If it does and you have time to report back, please open an issue or pull request.

`UserGrade` fields: `id`, `name`, `value` (a string; could be a number, a
letter grade, a rubric level, etc.), `userActivityUsageLink`,
`activity: Activity`, `feedback: Feedback`.

`Feedback` has `text`, `textHtml`, `textHtmlRichContent`, `viewUrl`.

### Activities (assignments / quizzes / discussions / etc.)

| Query | Returns | Notes |
|---|---|---|
| `activities(start: String!, end: String!, strict: Boolean)` | `[Activity!]!` | All activities across all the user's courses in a date range. ISO-8601 strings for `start`/`end`. |
| `activity(id: String!)` | `Activity` | One activity. |

`Activity` has `id`, `startDate`, `endDate`, `dueDate`, `completed`,
`completionDate`, `organization`, `source: ActivitySource`,
`gradeInfo: GradeInfo`, `feedback: Feedback`.

`ActivitySource` is an **interface**. Implementations include
`Assignment`, `Quiz`, `Survey`, `Content`, `Topic`, `ChecklistItem`,
`CourseOfferingActivity`. Use inline fragments to pull per-type fields:

```graphql
source {
  __typename
  id
  name
  url
  description
  descriptionHtml
  ... on Assignment { outOf addToGrades draft submissionType completionType }
}
```

### Calendar events

| Query | Returns | Notes |
|---|---|---|
| `events(start: String!, end: String!)` | `[Event!]!` | Calendar events in range. |
| `eventsWithOccurrences(start: String!, end: String!)` | `[EventWithOccurrences!]!` | Same, but recurring events are expanded into `occurrences`. |
| `event(id: String!)` | `Event` | Single event. |

`Event` carries `startDate`, `endDate`, `allDay`, `location`,
`isRecurring`, `recurrenceInfo: RecurrenceInfo`. `RecurrenceInfo`
has `repeatType`, `repeatEvery`, `repeatOnInfo` (day-of-week flags),
`repeatUntilDate`.

### Announcements

| Query | Returns |
|---|---|
| `announcement(id: String!)` | `Announcement` |

`Announcement` has `title`, `body`, `bodyHtml`, `bodyHtmlRichContent`,
`date`, `startDate`, `viewUrl`. Note there is **no** `announcements(...)`
list query; announcements surface through the alerts and activity-feed
APIs.

### Alerts (notification inbox)

Three parallel streams, each with the same shape:

| Query | Returns |
|---|---|
| `alertsPage(id: String)` / `alert(alertId: String!)` | top-bell alerts |
| `subscriptionAlertsPage(id: String, pageSize: Int)` / `subscriptionAlert(alertId: String!)` | subscription-based alerts |
| `updateAlertsPage(id: String, pageSize: Int)` / `updateAlert(alertId: String!)` | update alerts |

`AlertsPage { id, alerts: [Alert!]!, next: String }`: pass `next` back as
the `id` of the following call to paginate.

`Alert` is an interface with a large set of implementers. Fetch the
common fields (`id`, `title`, `message`, `viewUrl`, `iconUrl`, `date`,
`organization`) and use inline fragments for specifics:

* `AnnouncementAvailableAlert` / `AnnouncementUpdatedAlert` add `announcementId`.
* `ContentCreatedAlert` / `ContentUpdatedAlert` add `contentId`.
* `GradeReleasedAlert` / `GradeUpdatedAlert` add `gradeId`.
* `DropboxDueDateApproachingAlert`, `DropboxEndDateApproachingAlert`,
  `QuizDueDateApproachingAlert`, `QuizEndDateApproachingAlert`,
  `DiscussionTopicDueDateApproachingAlert`,
  `DiscussionTopicEndDateApproachingAlert` add `activityId`.
* `ClassStreamCommentAlert` / `ClassStreamMessageAlert` add `activityFeedItemId`.
* `*ForChildAlert` variants: parent-portal alerts aggregated across children.
* `GenericAlert`: fallback.

### Class-stream activity feed

| Query | Returns | Notes |
|---|---|---|
| `activityFeedArticlePage(orgUnitId: String!, id: String)` | `ActivityFeedArticlePage` | Per-course feed of posts. |
| `activityFeedArticle(id: String!)` | `ActivityFeedPost` | Single post (union of `ActivityFeedArticle` / `ActivityFeedAssignment`). |
| `activityFeedCommentPage(id: String!)` | `ActivityFeedCommentPage` | Paginated comments on a post. |

Posts expose `attachmentLinks: [ActivityFeedLink!]`, `isPinned`,
`webLink`, `commentsLink`, `firstComment`, `commentsCount`, plus the
post `message` (article) or `name`/`instructions`/`dueDate`/`submissionLink`
(assignment).

### Push notifications

| Query | Returns |
|---|---|
| `pushNotificationConfig` | `PushNotificationConfigOptions!` |

Returns categories of togglable notifications (`settingKey`, `name`,
`enabled`). Modified via the mutations below.

---

## Mutations

| Mutation | Returns | Notes |
|---|---|---|
| `pinEnrollment(id: String!)` | `UserEnrollment` | Pin a course to the home screen. |
| `unpinEnrollment(id: String!)` | `UserEnrollment` | Unpin. |
| `viewContentTopic(topicId: String!)` | `ContentTopic!` | Mark a topic as viewed (completion tracking). |
| `resetNotificationCount()` | `Boolean!` | Clear the bell counter. |
| `registerDevice(platform: DevicePlatform!, deviceToken: String!)` | `Boolean!` | `platform` is `android` or `ios`. Needed only for receiving pushes. |
| `deregisterDevice(platform: DevicePlatform!, deviceToken: String!)` | `Boolean!` | |
| `updatePushNotificationSetting(settingKey: String!, enabled: Boolean!)` | `PushNotificationSetting` | |

---

## Key types: field reference

Condensed. Any field not listed here is included in the introspection
dump; `jq` the `IntrospectionQuery` response if you need to be exhaustive.

### Organization
`id`, `name`, `code`, `startDate`, `endDate`, `isActive`, `color`,
`homeUrl`, `imageUrl`, `sequenceUrl`, `gradesViewUrl`, `semester: Semester`,
`theme: Theme`, `hasGradesEnabled`, `hasActivityFeed`, `requiresDevicePin`.

### UserEnrollment
`id`, `pinned`, `state: EnrollmentState`, `organization: Organization`,
`notifications: Notifications` (per-course unread counts for assignments,
discussions, quizzes), `startDate`, `endDate`, `dueDate`, `completionDate`.

`EnrollmentState`: `CURRENT`, `FUTURE`, `PAST`, `OVERDUE`, `DUESOON`,
`ENDSSOON`, `COMPLETE`, `INACTIVE`.

### UserEnrollmentPage
`id`, `enrollments: [UserEnrollment!]!`, `next: String`, `organization`.

### ContentRoot
`id`, `modules: [ContentModule!]!`.

### ContentModule (folder; implements `ContentItem`)
`id`, `parentId`, `title`, `isComplete`, `description`, `descriptionHtml`,
`descriptionHtmlRichContent`, `dueDate`, `startDate`, `endDate`,
`children: [ContentItem!]!`.

### ContentTopic (leaf; implements `ContentItem`)
`id`, `parentId`, `title`, `isComplete`, `description`, `descriptionHtml`,
`descriptionHtmlRichContent`, `dueDate`, `startDate`, `endDate`,
`viewHref: String!`, `viewUrl`, `downloadHref`, `pdfHref`,
`iconHref: String!`, `type`, `fileName`, `modifiedDate`.

### UserGrade
`id`, `name`, `value` (string), `userActivityUsageLink`,
`activity: Activity`, `feedback: Feedback`.

### Activity
`id`, `startDate`, `endDate`, `dueDate`, `completed`, `completionDate`,
`organization: Organization`, `source: ActivitySource`,
`gradeInfo: GradeInfo { id, type, value }`, `feedback: Feedback`.

### User
`id`, `displayName`, `firstName`, `lastName`, `imageUrl`, `isLearner`,
`isParent`, `parentPortalPath`.

---

## Example queries

### List all courses with pagination

```graphql
query EnrollmentPage($id: String) {
  enrollmentPage(id: $id) {
    next
    enrollments {
      id
      pinned
      state
      startDate
      endDate
      organization {
        id
        name
        code
        homeUrl
        isActive
        hasGradesEnabled
        hasActivityFeed
        semester { name }
      }
    }
  }
}
```

Paginate by passing the returned `next` back as `$id`; stop when `next` is
null.

### Walk the content tree for a course

Step 1: fetch the roots.

```graphql
query Root($id: String!) {
  contentRoot(organizationId: $id) {
    modules {
      id
      title
      children {
        __typename
        ... on ContentModule { id title }
        ... on ContentTopic {
          id title viewHref downloadHref pdfHref type fileName modifiedDate
        }
      }
    }
  }
}
```

Step 2: for each child that is a `ContentModule`, recurse.

```graphql
query Module($id: String!) {
  contentModule(moduleId: $id) {
    id
    title
    children {
      __typename
      ... on ContentModule { id title }
      ... on ContentTopic {
        id title viewHref downloadHref pdfHref type fileName modifiedDate
      }
    }
  }
}
```

The API does **not** return a deeply-nested tree in one call; you walk it
yourself. `contentRoot` already includes one level of children, so for a
fully flat course you often only need the first query.

### Grades for a course

```graphql
query Grades($org: String!) {
  userGrades(organization: $org) {
    id
    name
    value
    activity {
      id
      source {
        __typename
        ... on Assignment { name outOf }
        ... on Quiz { name }
      }
    }
    feedback { text textHtml }
  }
}
```

### Upcoming activities across all courses

```graphql
query Upcoming($from: String!, $to: String!) {
  activities(start: $from, end: $to, strict: false) {
    id
    dueDate
    completed
    organization { id name }
    source {
      __typename
      id
      name
      url
      ... on Assignment { outOf }
    }
    gradeInfo { value type }
  }
}
```

---

## Notes / tips

* **IDs are opaque URLs.** Don't parse or mint them. The trailing numeric
  segment of the URL matches Valence REST org/course/content ids, but the
  query string segments (e.g. `?deepEmbedEntities=0&embedDepth=1`) are
  load-bearing; the server rejects stripped versions.
* **`downloadHref` is often null.** For attachments always fall back to
  `viewHref` and follow the browser flow, or hit the Valence REST endpoint
  `/d2l/api/le/unstable/<ouId>/content/topics/<topicId>/file` using the
  same session.
* **Content-tree queries return one level only.** Recurse client-side;
  there is no depth parameter.
* **Cross-tenant access is not possible with one token.** Call
  `consortiumOrganizations` to discover sibling tenants, but each tenant
  requires its own OAuth flow with its own `tenant_id`.
* **Null-bubbling looks like an empty response.** When a non-null field
  (e.g. `userGrades: [UserGrade!]!`) resolves to null server-side, the
  server returns `{"data": null}` with 200 OK and no `errors` key. Easy
  to mistake for a transport glitch. This tool's `gql()` wrapper
  normalises it to `{}` so calling code can do `data.get("userGrades")`
  safely, but when debugging against the raw endpoint remember the
  server response is literally `{"data": null}`.
* **`userGrades` is unreliable.** At least on TU Delft, every course
  with `hasGradesEnabled=true` returns `{"data": null}` over GraphQL
  regardless of whether grades exist and regardless of user role.
  Could be tenant-specific or a wider Pulse backend issue, verify on
  your own tenant. Pulse itself fetches grades via the Valence REST
  endpoint `/d2l/api/le/<v>/<ouId>/grades/values/myGradeValues/`, see
  [VALENCE.md](VALENCE.md).
* **Introspection is enabled.** If the schema changes, re-run
  `IntrospectionQuery`; that is the source of truth, not this document.
