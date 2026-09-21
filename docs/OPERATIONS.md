# Operations guide

What is actually in place, how to check it, and what is *not* covered. Everything below was
observed on the running system unless it says otherwise.

## Deploying

`git push` to `main` runs `.github/workflows/ci.yml`:

1. **lint** - flake8, `black --check`, `pip-audit -r requirements.txt`.
2. **test** - fresh `pip install -r requirements.txt`, `manage.py check`, `makemigrations --check`,
   `manage.py test` (Postgres 16 service). Every gate is a plain command: the exit code decides.
   If the test step fails, a diagnostics step re-runs the suite and publishes the failing tests as
   annotations (readable without logging in to GitHub); it cannot change the outcome.
3. **deploy** (only if 1 and 2 passed, only on `main`) - SSH to the VPS: pre-deploy backup
   (best effort), `git pull`, `RELEASE_SHA=<commit> docker compose up -d --build`. The `web`
   container runs `migrate` and `collectstatic` before Gunicorn starts.
4. **health check** - `/healthz/` polled for ~50 s. Failure rolls the **code** back.
5. **informational, never a gate** (`continue-on-error`): smoke requests, then
   `manage.py ops_verify --annotate` inside the `web` container.

### Rollback limits
The rollback step resets Git to the previous commit and rebuilds. It does **not** undo a database
migration that already ran, so a migration the old code cannot read is not recoverable by it.
Migrations must stay additive (see `chat` 0023 and 0024) until a real down-migration plan exists.

## Checking production (read-only)

`docker compose exec -T web python manage.py ops_verify` reports migrations and schema drift,
Redis (PING + a 15-second throw-away key) and Celery (control ping), the beat schedule and how
long ago it last dispatched, provider connection state, one GET per news source, disk usage,
database connections, retention row counts, whether a backup target is configured, and the
`RELEASE_SHA` of the process. It writes nothing, deletes nothing and prints no credential or
user data. After every deploy the same output appears as annotations on the workflow run.

`docker compose exec -T worker printenv RELEASE_SHA` (and `beat`) confirms every container runs
the commit that was deployed; "git is current" says nothing about the image that is running.

## Health endpoints

| URL | Meaning |
|---|---|
| `/healthz/` | Database `SELECT 1`. 200 or 503. Polled by Docker and CI. |
| `/healthz/deep/` | Database + Redis (a real PING, 2 s bound). Anonymous, so it only returns fixed words. |

A 503 from either is logged as a WARNING, not an ERROR (`HealthProbeDowngradeFilter`), so a dependency
outage does not produce one alert per poll. A crash *inside* a probe is still an ERROR.

## Branding (SuperAdmin)

One setting controls the look of the whole application: **Branding 1** (the current appearance, the default),
**Branding 2** (Web Host Era Brand Kit), **Branding 3** (a separate "modern technology" identity) or **Custom**.
It applies to every user at once: the web UI (chat, projects, billing, admin, system status, media, login and
password-reset pages), the fonts, the logo, the browser-tab title, every email, the invoice page/PDF/share link and the
maintenance page. It changes presentation only: users, plans, invoices, permissions, AI settings, providers,
conversations, projects and files are not touched (the only row written is `SiteBranding`).

**Where:** sidebar Admin > **Branding** > "Brand themes" (`/governance/branding/theme/`), SuperAdmin only (every other
role gets 403, anonymous visitors are sent to login). The older Branding page (site name, tagline, logo, favicon)
stays: those are the identity of **Branding 1**.

1. **View the current branding.** The header of the page says "Current: ● Branding N"; the card of the applied
   branding carries an "Active" badge.
2. **Preview a branding.** Click a card, then **Preview**. Nothing is saved and nobody else sees a change. The preview
   is a compact sample (sidebar, buttons, badges, card, input, table, notifications) in the previewed colors and fonts,
   with a Light / Dark switch and an accessibility report. Clicking a card alone never applies anything.
3. **Apply Branding 1 / 2 / 3.** Select the card, press **Apply branding**, confirm the dialog. The change is
   immediate for everyone (no restart; the generated CSS is cached under a key that includes the row's version, so the
   next request already builds and caches the new one).
   * **Branding 1** uses the site name, logo and favicon saved on the old Branding page, and emits no extra CSS: the
     stylesheet alone renders it (`governance/test_branding.py` checks its preset data against `static/css/main.css`).
   * **Branding 2** takes its values from *Brand Kit.pdf* (pages 7-9): Too Blue to be True `#2C6EF8` (see the decision
     below), Matt Black `#151515`, White, Void `#00172A`, Soulstone Blue `#0055A5`, High Seas `#7DB5DC`, Halloween
     `#F96939`, Fennel Fiesta `#01C970`, Lime Fizz `#C7FC35`, Pearl Powder `#F9FFEB`, Dark Charcoal `#333333`, Nickel
     `#737373`, Philippine Silver `#B3B3B3`, Light Silver `#D9D9D9`, Plaster `#E9EAE9`. Two values are derived, not from
     the kit: muted text `#4D4D4D` and muted surface `#F3F4F3` (so muted text passes AA on Plaster).
   * **Branding 2 primary blue - decision (2026-09-21): `#2C6EF8`.** The kit's page-7 *label* says `#008CFF`, but the
     artwork disagrees with it: sampled from the rendered pages, `#2C6EF8` is the colour of the "Too Blue to be True"
     swatch itself, of the logo on every page (the kit's own "never change to colors other than specified" rule protects
     exactly this blue), of the cover and of the Color-section divider. `#008CFF` appears only in the printed label and in
     one of the nine logo-background tiles on page 4. The colour the brand actually wears is `#2C6EF8`, so it is the
     primary and the label is treated as a typo. White on `#2C6EF8` is 4.47:1 - 0.03 under WCAG AA for small text (it passes
     for large text); this is the kit's own pairing (cover, logo tiles), so it is kept and shown in the accessibility report
     as an *accepted* pair rather than hidden or "fixed" by changing the brand colour. To switch to `#008CFF` instead,
     change `colors.primary` (and the three literals marked `# PRIMARY`) in `BRANDING_2` (`governance/branding.py`).
     Branding 1 and Branding 3 are untouched by this.
   * **Fonts - requested vs fallback.** *Requested* (the Brand Kit): **Gilroy** (primary) and **Mont-Trial** (secondary).
     They are commercial fonts and **no licensed file exists in this project**, so they are **not bundled, downloaded,
     scraped or made a runtime dependency** (no font is fetched from any foundry). *Fallback in use:* the stacks declare
     `"Gilroy"` / `"Mont-Trial"` first (a visitor who has them installed sees them), then the closest open Google Fonts -
     **Urbanist** for Gilroy, **Montserrat** for Mont-Trial - then Inter and the system font. The Google Fonts stylesheet
     is the same optional CSS link every branding uses; if it is blocked the system font is shown. To use the real fonts
     later: obtain a licence, add the licensed `@font-face` files under `static/fonts/`, and keep the same family names.
   * **Branding 3:** deep indigo `#4F3FE0` (primary), deep slate-indigo `#1F2A5C` (structure), electric cyan `#22D3EE`
     (accent) on cool slate neutrals, crisp 4/6/10 px radii, a light sidebar with an indigo pill, Sora + Inter, its own
     mark (`static/branding/modern-mark-*.svg`).
4. **Create a Custom branding.** Select **Custom**; the form opens. Fill it, **Preview**, then **Apply**.
5. **Change the Custom brand name.** "Brand name" (1-60 characters, no `< > { } \`). It replaces the product name in the
   sidebar, login page, browser tab, email header and subjects, invoices and the maintenance page. It does not touch
   legal/company names (billing profile, "Bill to" data).
6. **Change Custom colors.** Three brand colors (primary = buttons/active/focus, secondary = headings/links/nav pill,
   accent = highlights) and, for Light and Dark separately, background, surface, text, muted text and border. Each is a
   6-digit hex (`#abc` is accepted and expanded). Hover, tinted and label colors are derived; **your colors are never
   changed silently**.
7. **Change Custom typography.** Primary font (body) and secondary font (headings): letters, numbers, spaces and hyphens
   only. Tick "Load these fonts from Google Fonts" for a Google font; otherwise a font must already be on the visitor's
   device (else the system font is used).
8. **Upload logos.** Custom has a light-mode and a dark-mode logo (PNG, JPG, GIF or WebP, under 2 MB, checked as a
   real image). They are stored with the existing branding uploads (`media/branding/`, the one public media folder).
   The favicon is the one saved on the old Branding page.
9. **Poor contrast.** The preview lists every pair that misses WCAG AA (4.5:1). A Custom branding with warnings cannot be
   applied until the acknowledgement box is ticked (the server checks this itself); the colors are applied exactly as chosen.
10. **Reset Custom branding.** "Reset Custom branding" (bottom of the page) clears the Custom settings and both logos;
    if Custom was applied the app returns to Branding 1.
11. **Recover from a bad branding.** Apply Branding 1 (no override at all). A saved Custom configuration that no longer
    validates falls back to Branding 1 by itself, so a bad value can never take the site down.

**Design tokens.** The app already consumed CSS custom properties, so a branding is a set of validated inputs from which
`governance/branding.py` derives those same properties. The brand tokens map to the existing ones: `--brand-primary` =
`--accent`, `--brand-secondary` = `--secondary`, `--brand-accent` (new), `--brand-background/-surface/-text/-muted/-border`
= `--color-bg/-surface/-text/-text-muted/-border`, `--brand-success/-warning/-danger` = `--color-success/--warn/--color-danger`,
`--font-primary/--font-secondary` = `--font-sans/--font-display`. Presets live in one place (`BRANDING_1/2/3`,
`custom_brand()`); templates contain no branding values (emails, invoices and PDFs read concrete colors through
`{% brand_email as brand %}`).

**Dark / light.** Branding and theme are independent: each branding defines a light and a dark surface set, and the
user's own Light / Dark / System choice still wins.

**Limits (by design or not built).**
* The Domain Generator and Code Playground pages are **client-facing** (role-gated tools their users open like any other
  page), so they follow the branding fully: surfaces, text, borders, accent, status colours, code syntax colours, focus
  and the fonts come from the same tokens as the rest of the application, in light and dark. (They load no `main.css`, so
  they take the complete token set from `{{ site_branding.full_css }}`; on Branding 1 they now use the standard palette
  instead of their old fixed dark console.) Deliberately fixed: the white label on the provider-icon chips and black
  drop-shadows.
* Two sentences that embed the product name inside translated text ("Welcome to AI Client Portal" and the
  admin-created-account email) are rewritten at send time by replacing the default name; a translation that does not
  contain the default name would keep its own wording.
* Email clients cannot render SVG, so Branding 3's mark is not shown in emails (its name is).

## Maintenance mode (SuperAdmin)

A real switch that puts the application behind the branded maintenance page. **Where:** sidebar Admin > **Maintenance**
(`/governance/maintenance/`), SuperAdmin only (every other role gets 403, anonymous visitors are sent to login).
States: **OFF** (no open window), **SCHEDULED** (waiting for its start), **ACTIVE**, and, as history, **COMPLETED** and
**CANCELLED**. There is at most one open (scheduled or active) window at a time - the database itself refuses a second.
All times are in the configured time zone (`TIME_ZONE`, currently UTC); the page, the maintenance page, the emails and the
history all show its name.

**What happens while it is ACTIVE.** `governance.middleware.MaintenanceMiddleware` (after authentication) answers every
request from a normal user, Manager, Admin, department Admin **or anonymous visitor** with the branded maintenance page,
HTTP **503**, `Retry-After`, `Cache-Control: no-store` - for any URL (typed and bookmarked links are blocked exactly like
menu items; htmx requests are sent to the page). **SuperAdmins carry on as normal.** Never blocked: `/healthz/` and
`/healthz/deep/` (Docker, the deploy check and monitors keep seeing the truth), static files and `media/branding/` (the
page is branded), and the sign-in flow (login, MFA step, Google sign-in, logout), so a SuperAdmin can always get in; the
sign-in pages behave exactly as they do outside maintenance and reveal nothing about accounts. If the maintenance check
itself ever fails, the site stays **open** (it never locks everyone out by mistake).

**How the clock works.** The site does not depend on a background job: every request applies a window's due start or end
by the clock. The beat task "Maintenance windows: apply due start/end" (every minute, seeded by a migration, editable in
the beat schedule) only makes the audit rows and the emails appear on time when nobody is visiting.

1. **Enable maintenance immediately.** Maintenance > "Enable now": Reason (required, 200 characters), Message for visitors
   (optional, 1000 characters, plain text), End time (optional - when set, the window ends by itself then), "Email users"
   tick. Press **Enable Now**, confirm the dialog. Everyone except SuperAdmins is locked out at once.
2. **Schedule maintenance.** "Schedule": Reason, Message, **Start** and **End** (both required; the start must be in the
   future; the end after the start; at most 7 days). Press **Schedule**, confirm. Nothing changes for visitors until the
   start: then it becomes ACTIVE by itself, and COMPLETED by itself at the end.
3. **Cancel a scheduled window.** While SCHEDULED, press **Cancel Scheduled**, confirm. Nothing was switched on; users who
   were told about it get a "cancelled" email.
4. **End maintenance.** While ACTIVE, press **End Maintenance**, confirm. Everyone can use the site again at once (a
   window with an end time also ends by itself).
5. **Check maintenance history.** The "History" table on the same page: type (Immediate / Scheduled), status, scheduled
   and actual start/end, reason, who initiated it (a name, never an email address) and who ended or cancelled it (or
   "automatically"). The full trail is in the audit log: `maintenance.scheduled`, `maintenance.enabled` (also for an
   automatic start), `maintenance.ended` (by hand), `maintenance.completed` (automatic), `maintenance.cancelled`.
6. **Verify the status.** The badge at the top of the Maintenance page (OFF / SCHEDULED / ACTIVE). From outside, as any
   non-SuperAdmin: `curl -sI https://<domain>/` returns `503` while ACTIVE and the normal answer otherwise.
7. **Verify the health endpoints during maintenance.** `curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/healthz/`
   and `.../healthz/deep/` keep returning `200` (or their honest `503` if a dependency is really down) - maintenance never
   changes them.
8. **Change the active branding.** Admin > Branding > "Brand themes" (see Branding above). The maintenance page and the
   maintenance emails follow the active branding like every other page; there is no second design.
9. **Branding 2 font fallback.** See "Fonts - requested vs fallback" under Branding: Gilroy / Mont-Trial are requested,
   Urbanist / Montserrat are what visitors get until licensed font files are added.

**Emails.** "Email users" sends four notices - scheduled, started, completed, cancelled (the last only if the scheduled
notice was sent) - to every active user through the normal notification path, so they use the global email shell and the
active branding. Each notice is claimed with one database update, so it is sent **exactly once** however many workers or
the beat task notice the transition. Untick "Email users" to switch all of them off for a window. Testing must not use
this on production: preview the page from Maintenance > "Preview the page visitors see" (nothing is activated).

**Support line.** Set `SUPPORT_EMAIL` (an address) to show "Need help? ..." on the maintenance page; empty = no line.

**Recovery.** If you are locked out of the UI, the site is still open to SuperAdmins: sign in and press **End
Maintenance**. If no SuperAdmin can sign in (they cannot reach the server either), on the host:
`docker compose exec web python manage.py shell -c "from governance.models import MaintenanceWindow as M; M.objects.filter(open_slot=True).update(status='completed', open_slot=None)"`
then clear the cache key (`docker compose exec web python manage.py shell -c "from django.core.cache import cache; cache.delete('maintenance:state')"`)
or wait 30 seconds.

**Emails in general.** Every email the server sends is rendered from the global email shell
(`templates/notifications/_email_shell.html`): notifications, MFA and password-reset emails, invoices and reminders, the
deploy email, crash alerts, the SMTP test email and maintenance notices. Callers that pass no HTML (deploy, alerts, the SMTP
test) get the shell wrapped around their text automatically in `send_via_connection`, so a future sender cannot leak a
bare-text email (`notifications/test_email_shell.py` exercises every sender).

## Dashboards (what each role sees)

The home page after sign-in (`/accounts/dashboard/`) is role-aware. **SuperAdmin:** system, providers, users, AI requests
today, storage, invoices, maintenance and alerts at a glance, then **Needs attention** (each item links to the page that
fixes it), business and AI usage, recent activity and quick actions; the detailed charts and System status stay below.
**Admin:** the same idea for their own department (team, projects, approvals, billing) - no infrastructure. **Manager:**
their team. **User:** their own workspace (continue where they left off, projects, usage, notifications).

* **Needs attention** lists an underlying problem once. For a SuperAdmin it includes "Database backup is not configured"
  until `BACKUP_S3_BUCKET` is set on the server (see docs/BACKUP_RESTORE.md), failing scheduled jobs, a provider whose sync
  failed, disk above the Server Media thresholds, payment proofs to verify, overdue invoices, refund and plan requests,
  users above 80 % of their limit, and maintenance that is scheduled or active.
* A figure the application cannot measure is shown as "Unavailable", never as zero. A trend appears only when yesterday
  gives a real baseline (10 or more requests in the same hours).
* Admins and Managers see counts for their own scope only; other people's conversation titles and project names are never
  shown. A dashboard link never grants access - the page behind it still checks the role.
* Nothing to configure: no new settings, no migration, no cache to clear. The page reads what the application already
  stores; System status (SuperAdmin) is the same single probe it always ran.

## Deploy emails

After every deploy the workflow runs `manage.py send_deploy_notification` inside the `web` container
(`notifications/management/commands/send_deploy_notification.py`): one email per **active SuperAdmin**, sent through the
same path as every other email (the SMTP settings saved under Email Logs, else the `EMAIL_*` environment). Subject
`[AI Client Portal] Deploy succeeded (<commit>)`, or `... Deploy FAILED - rolled back` after a rollback.

* **Placeholder addresses are skipped** (`example.com/.org/.net`, `.test`, `.invalid`, `.localhost`): they can never
  receive mail, so the demo `admin@example.com` account would otherwise bounce (550) on every deploy. Deactivate or
  re-address such a SuperAdmin to stop it also receiving crash alerts (`alert_recipients`).
* **The run page tells you the outcome**: a `Deploy email` annotation says how many SuperAdmins the mail server
  accepted the message for, or a warning if some were refused. The step never fails a deploy.
* **"Accepted" is not "delivered".** The app can only see what the mail server answered. If the annotation says
  accepted but nothing arrives, look in Spam/Promotions (search `Deploy succeeded`) and see Email Logs (status, and the
  reason for a refusal). The sending domain's SPF record covers the mail server; DKIM and DMARC records for it are not
  published (checked in public DNS), which makes Gmail more likely to file automated plain-text mail as spam.
  Publishing DKIM (the mail host's "Email Deliverability" page) and a DMARC record are DNS changes, not code.
* `manage.py ops_verify` (section `settings`, line `email:`) shows which mail path is live and the last 24 hours as
  counts (accepted / refused), never an address, subject or error text.

## Alerts

* Unhandled server errors go to **Sentry** (`SENTRY_DSN`) and to the admins in `ADMINS` by email,
  exactly once per error (one handler; falls back to a direct send if the broker is down).
  **If `ADMINS` is empty (it is, in production today) no email is sent and Sentry is the only alert.**
* Credentials are masked before they reach a log, an exception message or Sentry
  (`config/redaction.py`).
* There is no alert for: Celery worker stopped, beat stopped or stale, disk pressure, repeated
  provider failures. Nothing pages anyone. What exists is visibility: the SuperAdmin dashboard's
  System status shows each background task's recorded outcome (last success or failure with the
  exception class only, duration, retries, running, overdue) and flags a stale scheduler;
  `ops_verify` prints the same plus today's AI call counters, browser-side errors, streaming
  capacity and media orphans. Outcomes and counters are kept in the shared cache (Redis), so a
  Redis flush resets them and a task with no record shows "No outcome recorded", never "OK".

## Authorization

Roles are hierarchical - `user < manager < admin < superadmin` - and checked at the view
(`accounts/permissions.py`). Department scoping (an Admin sees their own department) and team
scoping (a Manager sees their own team) are enforced inside the views, per object. The full
route x role table is a test (`governance/test_authorization_matrix.py`) against a reviewed golden
file (`governance/authz_expected.json`): if a route's minimum role changes the test fails, and
after a deliberate change you rerun it with `AUTHZ_UPDATE=1` and review the diff.

Private files are never served from `/media/`; only `branding/` is public. Chat attachments are
served by `chat:download_attachment` (owner only), payment proofs by `billing:invoice_proof`.
The SuperAdmin "Server Media" page (`governance/media_views.py`) is not a second door: it is
SuperAdmin-only, addresses a file by (source, database id) and never by path, sends every download
as an opaque attachment, previews only images/PDF/plain text whose first bytes match their
extension (never SVG or HTML), audits each download and preview of a private file, and can delete
only a file that no record refers to. Its "View" page shows a file and its facts without ever making
it public; anything it cannot show safely says "Preview not available" and offers a download.
Bulk delete (`governance:media_bulk_delete`) trusts nothing the browser selected: it takes opaque ids (never a
path), refuses more than 50 per request, refuses to run the same step twice, and for every file re-checks - now -
that it still exists and that no record refers to it; a referenced file is skipped, never deleted. The page sends
chunks of 10 (at most 100 per run) so it can show real progress, and each request writes one audit row of counts
(selected / deleted / skipped / failed / bytes freed), never names.

Object-level rules (`governance/test_object_authorization.py`): a department Admin manages the Users
and Managers of their own department only - not another department, a peer Admin or a SuperAdmin -
and an Admin with no department has no scope at all. Chat conversations are owner-only for every
role, SuperAdmin included.

## One signed-in browser per account

Signing in on a second browser or device signs the first one out (`accounts/single_session.py`). Every login
(password, after MFA, Google, signup) writes a fresh random token to `User.active_session_token` and to that
browser's session; `SingleSessionMiddleware` compares them on every authenticated request. A browser whose token
no longer matches is logged out, sent to the login page with "You were signed out because your account was signed
in on another browser or device.", and an `auth.session_superseded` row is written to the audit log (browser
requests made by htmx get an `HX-Redirect` instead). The newest login always wins; the old browser is signed out
on its **next request**, not instantly (a reply already streaming finishes).

* Applies to every role. Password change in your own browser does not sign you out (the token lives in the
  session data, not the session key).
* Accounts already signed in when this shipped hold no token: the first request from any of their sessions adopts
  one, so an existing user is not logged out by the deploy itself; two such sessions at once leave exactly one.
* Kill switch: `SINGLE_SESSION_PER_USER=False` in the server's `.env` (then `docker compose up -d`). Nothing else
  needs undoing; the token column is harmless when unused.
* Limits: this is "latest login wins", not a device manager. There is no list of active sessions, no "sign out
  everywhere else" button, and a shared account will keep signing its own users out of each other.

## Rate limits and Redis

Limits are counted in Redis. What happens when Redis is unreachable depends on the kind of limit
(`accounts/rate_limit.py`):

| Kind | Used for | During a Redis outage |
|---|---|---|
| SECURITY_CRITICAL | login, signup, password reset, Google sign-in | per-process in-memory counter with the same limit (3 Gunicorn workers => at most ~3x the limit, bounded) |
| EXPENSIVE | per-minute AI message limit | same local fallback, so paid provider calls stay restricted |
| NORMAL | everything else (the browser-error beacon) | fails open |

Nothing fails closed: a limiter that cannot count never blocks someone it has not counted itself.
Daily/monthly plan quotas are database-backed and unaffected. Login is throttled three ways:
one username (django-axes locks the account after 5 failures, from any IP), many usernames from
one IP (failed logins are counted per IP: `LOGIN_IP_FAILURE_LIMIT`, default 30 per hour; successes
never count, so an office signing in each morning is unaffected) and one username from many IPs
(the same axes lock). The refusal text does not depend on whether the account exists.
**Client IP (what the per-IP limits key on).** `accounts/rate_limit.py::client_ip` decides from who actually
connected, not from headers alone: a peer inside Cloudflare's published ranges (`CLOUDFLARE_IP_RANGES`) is
believed when it sends `CF-Connecting-IP`; behind this host's Nginx (`X-Real-IP`, set by Nginx to the true TCP
peer) the header is believed only if that peer is a Cloudflare address, otherwise `X-Real-IP` is the visitor and
every forwarded header is ignored; a direct public connection to Gunicorn ignores all forwarded headers. So
reaching the origin directly can no longer be used to pick your own rate-limit identity. **Remaining limit:** if
the live Nginx does not send `X-Real-IP` (its config is not in this repository), the old behaviour applies
(`CF-Connecting-IP` is believed) rather than making every visitor look like the proxy. Confirm the live Nginx
matches `deployment/nginx.conf.example`, and close the direct door (see "Transport security").

## Server health panel (SuperAdmin, System status)

`/governance/` -> System status -> **Server health**: CPU, memory, disk, uptime, load and the five services. Real
readings from `/proc` (a container reads its host's, so these are the server's numbers) and statvfs; a reading that
cannot be taken says **Unavailable**, never 0 %. One snapshot is cached for `SERVER_HEALTH_CACHE_SECONDS` (45 s) and
the panel refreshes itself every 60 s while the tab is visible. Thresholds: CPU 70/85 %, memory 75/90 %
(`SERVER_HEALTH_*_PCT`), disk 80/90 % (`MEDIA_DISK_WARN_PCT`/`MEDIA_DISK_CRITICAL_PCT`, shared with Server Media).
The service rows are inferred from the checks System status already runs; **container (Docker) state is not
readable from the app** (no Docker socket is mounted, on purpose), so use `docker compose ps` on the server for that.
The same numbers are in `ops_verify` (`capacity` memory, `disk`), which remains the deploy-time check.

## Transport security (Cloudflare in front, plain HTTP to the origin)

What production showed before this was configured (anonymous requests, before the deploy that added it): `http://`
answered 200 with no redirect, the CSRF cookie had no `Secure` flag, and no HSTS header was sent. After that deploy
(f38d6e6, 2026-09-20): `http://` GET -> 301 and POST -> 308 to https, https 200 with `Strict-Transport-Security:
max-age=86400`, CSRF cookie `Secure`, `/healthz/` and `/healthz/deep/` 200 on both schemes. Cloudflare terminates TLS and speaks
plain HTTP to the origin, so Django never sees HTTPS (`request.is_secure()` is False): Django's own
`SECURE_SSL_REDIRECT`/HSTS cannot be used. `accounts/middleware.py::CloudflareHttpsMiddleware` uses the
`CF-Visitor` header Cloudflare adds instead (the scheme the **visitor** used):

| Setting (default in `docker-compose.yml`) | Effect |
|---|---|
| `ENFORCE_HTTPS_VIA_CLOUDFLARE=True` | a visitor who used `http://` gets a 301 (GET/HEAD) or 308 (other methods) to the same URL on https. `/healthz/` and `/healthz/deep/` and requests without `CF-Visitor` (the deploy's own health check) are never redirected. No loop: `CF-Visitor` reflects the visitor's scheme. |
| `CLOUDFLARE_HSTS_SECONDS=86400` | `Strict-Transport-Security: max-age=86400` on https visitors' responses. One day on purpose (a browser remembers it that long); no `includeSubDomains`, no `preload`. Raise to `31536000` in the server's `.env` after it has run cleanly for a while. |
| `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True` | the browser never sends the session or CSRF cookie over `http://`. |

**Kill switch:** set `ENFORCE_HTTPS_VIA_CLOUDFLARE=False` (and/or the cookie flags to `False`) in the server's `.env`
and redeploy; nothing is stored in the database. Everything is per-process configuration.
Cloudflare's own "Always Use HTTPS" setting (dashboard) would make the redirect redundant; it is not required.
Security headers already present: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`. **No `Content-Security-Policy` is sent**
(known limitation: the pages use inline scripts and handlers).

**Origin exposure.** `docker-compose.yml` publishes Gunicorn on `0.0.0.0:8000` (the default is unchanged: the live
Nginx and firewall are not in this repository), but checked from the internet on 2026-09-21 the origin answered only
on **port 80** (Nginx): `:8000`, `:443`, PostgreSQL `:5432` and Redis `:6379` did not answer, so the cloud
firewall/security list already blocks them (one vantage point; the rules themselves were not read). Consequences:
port 80 can still be reached without Cloudflare (`http://<origin>/` serves the app), and because 443 is closed
Cloudflare must be talking plain HTTP to the origin (Cloudflare SSL mode "Flexible", inferred, not read from the
dashboard), so that leg is unencrypted. Closing both is an infrastructure change: a certificate on the origin
(e.g. a Cloudflare Origin CA cert) with Cloudflare set to "Full (strict)", and port 80/443 restricted to Cloudflare's
ranges (`https://www.cloudflare.com/ips/`) in the cloud firewall. Once `deployment/nginx.conf.example`'s
`proxy_pass http://127.0.0.1:8000` is confirmed on the server, `WEB_BIND_ADDRESS=127.0.0.1` in the server's `.env`
also removes the 0.0.0.0 binding (belt and braces). None of this has been changed or verified from here.

**Redis memory.** Redis (`redis:7-alpine`, no `--maxmemory`, no container limit) holds the Celery queue, the cache
and the rate-limit counters, so an eviction policy would be a correctness decision, not a tuning one. Measured by
`ops_verify` on production (2026-09-21, deploy 838fb1f): **used 1.85 MB, peak 2.10 MB, `maxmemory` not set, policy
`noeviction` (Redis's default, so it never silently drops queued work)**, on a host with 5.8 GB total / 3.2 GB
available. At that size the missing bound is a theoretical risk, not a current one, and setting one needs a Redis
restart, so nothing was changed. If usage ever grows into the hundreds of MB (`ops_verify` section `redis` shows it
after every deploy; it reports WARN while unbounded), pick a limit from the numbers then and add
`--maxmemory <n> --maxmemory-policy noeviction` to the redis service's command.

**Crash alerts.** `ADMINS` (`Name:email` pairs) is unset in production. `governance/error_alerts.py::alert_recipients`
now falls back to every **active SuperAdmin** (the same people the deploy notification already emails), so an unset
`ADMINS` no longer means nobody is told. `ops_verify` reports how many recipients there are and which source. The
health probes' expected 503 still sends nothing (`HealthProbeDowngradeFilter`).

## Database backup

* **Command:** `manage.py backup_database` (`pg_dump --format custom` -> S3-compatible bucket, prefix `db-backups/`,
  names `backup-YYYYMMDD-HHMMSS.dump`). After uploading it asks the destination for the object's size and fails if it
  differs from the dump. Not client-side encrypted (rely on bucket-level encryption).
* **Exit status:** `0` done, `3` no bucket configured, `4` a configured backup failed (dump, upload or size check).
* **Schedule:** the daily Celery Beat task `accounts.tasks.run_scheduled_database_backup` (03:00 UTC). Not configured
  -> a warning; a real failure -> an ERROR log, a retry (3x with backoff) and a "Failed" row in System status.
* **Every deploy:** the pre-deploy step runs the command before `git pull`. `0` -> notice annotation; `3` -> a visible
  **warning annotation** and the deploy continues; `4` -> **error annotation and the deploy stops before anything
  changes** (production keeps the old code and schema); set the repository variable
  `ALLOW_DEPLOY_WITHOUT_BACKUP=true` to ship anyway on purpose; any other status (the web container is down) ->
  warning, deploy continues so a down stack can still be repaired. Nothing is only a line in a server file.
* **Retention:** `BACKUP_RETENTION_DAYS` (default 30). Only `backup-*.dump` objects are ever removed, and the newest
  3 are never removed whatever their age, so a stopped schedule cannot delete the last backups.
* **Verify:** `manage.py ops_verify` (section `backups`) lists the destination read-only and reports the newest
  backup's age and size (WARN after 26 h, FAIL if it is empty or unreadable). `manage.py verify_backup` downloads the
  newest backup to a temp directory and has `pg_restore --list` read it (restores nothing; exit 0 = readable).
* **Restore:** `docs/BACKUP_RESTORE.md` (throwaway database first). A real dump/restore of the production database
  was rehearsed on 2026-09-19 (row counts and sampled rows identical).
* **CURRENT STATE: no destination is configured in production** (`ops_verify`: "no S3 backup target configured"), so
  no off-server backup exists yet. Setting `BACKUP_S3_BUCKET`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY`
  (and `BACKUP_S3_ENDPOINT_URL`/`BACKUP_S3_REGION` for non-AWS storage) in the server's `.env` needs a bucket and
  credentials that only the owner can create; the next deploy's annotation and `ops_verify` then show the first real
  backup. The S3 upload/verify path is proven with a doubled destination in tests, never against a real bucket.

## Capacity (Gunicorn, streaming)

**CURRENT CAPACITY ASSUMPTION (not a measurement).** Production reports **1 CPU core** (`ops_verify`, section
`capacity`, which also prints the container's memory). `deployment/gunicorn.conf.py`: `gthread` workers,
3 workers x 4 threads = 12 requests at once (`GUNICORN_WORKERS`/`GUNICORN_THREADS`; the default formula
`min(2*cores+1, 3)` gives 3 on one core), `timeout=60`, `graceful_timeout=30`, workers recycled every ~1000 requests.
Chat replies are SSE streams (`StreamingHttpResponse`), and **each open reply holds one thread for its whole
duration**, so at most 12 replies can stream at once and every other request (page loads, health checks) shares
those threads. Streaming is I/O-bound (waiting on the provider), which is why threads rather than sync workers are
used and why a single core is workable at small scale; CPU-heavy work (PDF/DOCX/XLSX parsing, Markdown rendering)
runs on that same core. `ops_verify` reports how many replies are streaming right now against the 12 threads.

What was checked, and what it means:

* `timeout=60` does not cut a long reply: for `gthread` workers it is a heartbeat, not a per-request limit.
* The example Nginx uses `proxy_buffering off` and `proxy_read_timeout 120s`: a reply that sends nothing for
  2 minutes (a provider retrying) is cut by Nginx, not by Gunicorn. The live Nginx is not in this repository.
* A deploy restarts the web container; Docker's default 10 s stop timeout is shorter than `graceful_timeout` (30 s),
  so a reply still streaming at that moment is cut (its partial text is saved). Accepted.
* Every request opens its own database connection (`CONN_MAX_AGE=0`); the database allows 100, so 12 threads are
  comfortable on that axis.
* Attachment parsing is bounded (PDF: first 60 pages; sheets: 5,000 rows; every extractor stops at 8,000 characters)
  and cached, so one large upload no longer costs seconds of the single core on every turn.

**No load test has been run, against production or anywhere else, so no maximum number of users is claimed.** Raise
`GUNICORN_THREADS` only if `ops_verify` shows streaming at or above ~75% of the threads at peak; adding workers on
one core adds memory, not CPU.

## Context, attachments and quotas

A chat request sends a bounded slice of the conversation (`chat/context_window.py`), not all of it:
the current message, the newest messages that fit, the opening message, and short notes on older
questions - inside min(plan `max_context_tokens`, model window minus an 8192-token reply reserve).
Model windows are configured assumptions (`MODEL_CONTEXT_TOKENS*`), not read from the providers, so
they are unverified against the real limits. Extracted attachment text is cached (Redis, 6 h, keyed
by message and file state); a replaced file is read again. The daily new-conversation limit is
enforced atomically under a row lock on the user, and a deleted conversation still counts.

## Database timeouts

Production reports `statement_timeout=0` and `idle_in_transaction_session_timeout=0`. Both are
opt-in via `DB_STATEMENT_TIMEOUT_MS` and `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` (default 0 = unchanged;
rationale, risk and rollback are in `config/db_options.py`). Start with idle-in-transaction; a
statement timeout also applies to migrations, the retention sweep and exports.

## Cache review

| Cache | Lifetime | Invalidation | Note |
|---|---|---|---|
| AI response cache (`chat/response_cache.py`) | 1 h | key = user + model + system prompt + full history, so any change to the prompt, history or an attachment is a new key; nothing purges it | identical prompts can return an up-to-1-hour-old answer; never holds the "older messages condensed" note; truncated replies are not cached; cache hits are not counted as provider calls |
| Live Intelligence feeds | 15 min | manual refresh, 1 per user per minute | grounding text is part of the prompt, so a refreshed feed changes the response-cache key |
| Attachment text | 6 h (failure 10 min) | key includes file size and mtime | |
| Media scan (Server Media) | 10 min | rescan button (30 s cooldown) and every delete | the file LIST is database-driven and always current; only the totals/orphans are cached |
| Pricing, provider/model visibility, usage, System status | not cached | - | recomputed per request |
| Celery results | 1 day (Celery default `result_expires`) | expire by themselves | |
| Task outcomes, AI counters | 30 d / 8 d | expire by themselves | diagnostics, not accounting |

Redis in `docker-compose.yml` has no `maxmemory` or eviction policy: every key above expires, but a
key written without a TTL would never leave. None of the new keys is written without one.

## Files and retention

* Uploaded files are removed when the row that owns them is deleted (after the transaction commits).
  Deleting a conversation in the UI is a *soft* delete; the rows and files go when a department's
  retention period sweeps them, if one is set. The default is "forever".
* Audit log, email log and notifications have no automatic cleanup. `Delete email logs` is a manual
  SuperAdmin action.
* Current retention, per data set (nothing else is deleted automatically, and **no cleanup job was
  added: no explicit retention policy exists to base one on**):

  | Data | Kept | Removed by |
  |---|---|---|
  | Audit log | forever | nothing; the model refuses updates and deletes |
  | Email log | forever | SuperAdmin "Delete email logs" (manual) |
  | Notifications | forever | nothing |
  | Conversations, messages, attachments | forever unless the department sets a retention period | daily sweep for those departments; files go with their rows |
  | Temporary files | only `backup_database`'s temp directory | removed when the command ends |
  | Celery results | 1 day | expire by themselves |
  | Cache keys | see the cache review | expire by themselves |
  | Database backups | `BACKUP_RETENTION_DAYS` (30) in the S3 bucket | the backup command - **but no bucket is configured in production, so no backup exists** |

  `ops_verify` prints row counts and the oldest row of each so growth is visible.
* Storage visibility: `ops_verify` (`disk`) reports disk usage, the media directory's size, and the
  count of orphan candidates; the SuperAdmin "Server Media" page shows the media disk's
  NORMAL/WARNING/CRITICAL state (`MEDIA_DISK_WARN_PCT`/`MEDIA_DISK_CRITICAL_PCT`) and lists files no
  record refers to. Database size and largest tables are in `ops_verify` (`database`); Docker's own
  disk use is not visible from inside a container - run `docker system df` on the host. Nothing is
  ever deleted automatically; an orphan is deleted one at a time, by a SuperAdmin, after a "Delete
  file?" confirmation and - the part that matters - a fresh server-side check that nothing refers to
  it (a file that became referenced after the page loaded is refused).
* Docker container logs are capped at 3 x 10 MB per container. The app writes `logs/app.log`
  (5 MB x 3 rotated) **inside the container**, so it is replaced on every deploy.
* Docker build cache is not pruned automatically; run `docker builder prune` occasionally.

## Configuration worth knowing

| Variable | Effect |
|---|---|
| `RELEASE_SHA` | Set by the deploy step; shown by `ops_verify`. |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | Default to `FORCE_HTTPS`. Can be set alone: behind Cloudflare with an HTTP-only origin they can still be `True`. |
| `CELERY_TASK_TIME_LIMIT` / `CELERY_TASK_SOFT_TIME_LIMIT` | 1800 / 1500 seconds. A task that exceeds them is stopped. |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS` | Default 3 x 4 = 12 concurrent requests. Every open chat reply holds one thread until it finishes. |
| `ADMINS` | `Name:email` pairs that receive crash emails. |
| `ENFORCE_HTTPS_VIA_CLOUDFLARE`, `CLOUDFLARE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | Transport security (see above); `docker-compose.yml` defaults them to True / 86400 / True / True. |
| `CLOUDFLARE_IP_RANGES` | Comma-separated Cloudflare proxy ranges used by `client_ip` (defaults to the published list). |
| `SINGLE_SESSION_PER_USER` | Default True: a new login signs the account's other browser out (see "One signed-in browser per account"). |
| `WEB_BIND_ADDRESS` | Compose only: interface Gunicorn's port 8000 is published on (default `0.0.0.0`; `127.0.0.1` closes direct access once Nginx is confirmed to proxy to it). |
| `LOGIN_IP_FAILURE_LIMIT` | Failed logins per IP per hour across all usernames (default 30). |
| `MODEL_CONTEXT_TOKENS_DEFAULT`, `MODEL_CONTEXT_TOKENS`, `MODEL_CONTEXT_TOKENS_BY_MODEL` | Input budget per model (default 32000; adapter defaults in `config/settings.py`). |
| `MEDIA_DISK_WARN_PCT`, `MEDIA_DISK_CRITICAL_PCT` | Server Media disk thresholds (80 / 90). |
| `MEDIA_MEDIUM_MIN_BYTES`, `MEDIA_LARGE_MIN_BYTES`, `MEDIA_LARGE_THRESHOLDS_MB` | Server Media size filter: Small/Medium/Large limits (1 MB / 10 MB) and the extra large-file thresholds (50, 100, 500 MB). Visibility only - nothing is deleted by size. |
| `DB_STATEMENT_TIMEOUT_MS`, `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | Opt-in PostgreSQL timeouts (default off). |

Every variable the code reads is listed in `.env.example` (a test fails otherwise); names only,
never values, are checked.

## Not covered (do not assume otherwise)

* No screen-reader testing has been done; only automated (axe) and keyboard checks.
* No staging environment; no load or concurrency test against production.
* No automatic fallback if a provider is down beyond trying the next candidate model.
* No down-migrations on rollback (see above).
