# Project Map — AI Client Portal

Ye document batata hai ke har file kis kaam ke liye hai aur kaunsi file kis se link/connected hai. Jab bhi koi edit karni ho, pehle yahan dekh lein ke sahi file kaunsi hai aur usay chhedne se kya aur kya affect hoga.

**General rule of thumb (Django pattern, har app mein yehi chain hai):**

```
models.py  →  views.py  →  urls.py  →  templates/<app>/*.html
   ↑              ↑
   |              └── forms.py (agar form hai), admin.py (Django admin)
   └── migrations/ (models.py badalne ke baad hamesha `makemigrations` chalayein)
```

Static CSS **ek hi file** hai sab pages ke liye: `static/css/main.css`. Har page ka look isi file se aata hai — naya class chahiye ho to yahin add hoga.

---

## 1. Project-level files (root)

| File | Kaam |
|---|---|
| `manage.py` | Django ka entry point — sab commands isi se chalte hain (`runserver`, `migrate`, `test`, waghera) |
| `config/settings.py` | **Sabse important file.** Database, installed apps, API keys, sessions, logging, login brute-force protection (axes), backup config — sab yahan |
| `config/urls.py` | Root URL routing — yahan se har app ke `urls.py` ko include kiya gaya hai |
| `config/wsgi.py` / `config/asgi.py` | Server entrypoints (production/deployment ke liye, aksar chhedne ki zaroorat nahi) |
| `.env` | Real secrets (API keys, SECRET_KEY, backup credentials) — **kabhi commit nahi hoti**, sirf is machine par hai |
| `.env.example` | `.env` ka template, bina real values ke — naya setup karte waqt copy karke `.env` banayein. **Hamesha placeholder blank rakhein, kabhi real value yahan paste na karein** (ek dafa isi file mein real demo-account passwords accidentally aa gaye thay) |
| `requirements.txt` | Python packages ki list. `django-axes` (login lockout), `boto3` (S3 backups), `celery`+`django-celery-beat` (background jobs/scheduler), `geoip2fast` (IP→country language guess), `openpyxl` (Excel export), `xhtml2pdf`/`reportlab` (PDF export), `arabic-reshaper`+`python-bidi` (Arabic/Urdu text shaping in PDFs), `sentry-sdk` — sab is project mein add hui hain |
| `AI_Client_Portal_Spec.md` | Original spec document jis par poora project based hai |
| `PROJECT_MAP.md` | Yehi file jo aap abhi padh rahe hain |
| `docs/BACKUP_RESTORE.md` | Database backup/restore ka poora procedure — exact commands, env vars, Railway cron setup. **Emergency mein sabse pehle yahan jayein.** Postgres/S3 path abhi tak live-tested nahi (koi Postgres/pg_dump is machine par available nahi) — SQLite-based logical restore test ka evidence bhi isi file mein hai |
| `logs/app.log` | Runtime error log (gitignored) — kisi bhi AI provider call ya server error ka **real** traceback yahan milta hai, generic user-facing message ke peeche |
| `media/` | User-uploaded chat attachments (gitignored — accidentally ek test file commit ho gayi thi purane commit mein, ab aage se aisa nahi hoga) |
| `locale/ur/`, `locale/ar/` | Urdu aur Arabic UI translations (`.po` source + compiled `.mo`) — 394 strings har language mein. Naya translatable string add karna ho to yahan dono files mein entry chahiye (extraction script is session mein banaya gaya tha, standard `makemessages`/`compilemessages` is machine par nahi chal saka kyunke `xgettext`/`msgfmt` install nahi thay) |
| `railway.json`, `mise.toml` | Railway deployment configs |
| `deployment/` | VPS (non-Railway) deployment ke configs: Gunicorn, Nginx, systemd |
| `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` | Docker setup — `web`/`worker`/`beat` teeno isi image se banate hain, entrypoint migrate+collectstatic khud chalata hai. **Note:** files review ho chuki hain lekin is machine par Docker kabhi install nahi tha, isliye `docker-compose up` khud kabhi actually run nahi hua — deployment target par test karna baaki hai |
| `.github/workflows/ci.yml` | GitHub Actions — har push par tests + lint automatically chalte hain. Real pushed commit par pass hote confirm kiya gaya hai (sirf file existence nahi) |

---

## 2. `accounts` app — Login, Signup, Users, Departments, Profile, Login Security

**Responsibility:** Authentication, User model, Department model, RBAC, profile settings, brute-force login protection.

| File | Kaam | Kis se link hai |
|---|---|---|
| `accounts/models.py` | **`User`** (email, role, department) aur **`Department`** (name, budget cap) models yahan define hain | Almost har app isko import karta hai (`chat`, `governance`) |
| `accounts/signals.py` | `post_save` signal — **naya user ban'ne par automatically Demo plan assign** karta hai (`governance/plans.py::assign_default_plan_if_missing`). Login-lockout hone par audit log entry likhne wala signal bhi yahan hai | `accounts/apps.py::ready()` se connect hota hai |
| `accounts/axes_hooks.py` | Login lockout hone par custom (on-brand) error page dikhane wala callable | `config/settings.py::AXES_LOCKOUT_CALLABLE` isko point karta hai |
| `accounts/middleware.py` | **`GeoLanguageMiddleware`** — naye/anonymous visitors ke liye IP se country guess karke starting language set karta hai (sirf pehli dafa, cookie set hone ke baad kabhi override nahi karta). **`UserLanguagePreferenceMiddleware`** — logged-in user ke DB mein stored `preferred_language` ko har request par activate karta hai (session/cookie se independent, isi liye "per-user not per-session" persist hota hai) | `config/settings.py::MIDDLEWARE` mein dono wired hain — Geo wala `LocaleMiddleware` se pehle, User-preference wala Authentication ke baad |
| `accounts/geo.py` | IP address → country → language (en/ur/ar) mapping. `geoip2fast` library use karta hai (offline database, koi API key/account nahi chahiye) | `accounts/middleware.py::GeoLanguageMiddleware` isko call karta hai |
| `accounts/forms.py` | Login form, Signup form, Profile edit form | `accounts/views.py` use karta hai |
| `accounts/views.py` | Login, Logout, Signup, Dashboard, Profile (naam edit + password change), **`set_language_preference`** (Settings ka language toggle) views | `accounts/urls.py` se wire hain, templates render karte hain |
| `accounts/urls.py` | `/accounts/login/`, `/accounts/signup/`, `/accounts/profile/`, `/accounts/set-language/` waghera | `config/urls.py` mein include hai |
| `accounts/permissions.py` | RBAC helpers: `role_required` decorator, `AdminRequiredMixin` — **ye poore project mein har jagah use hota hai** admin-only pages protect karne ke liye | `chat/views.py`, `governance/views.py` sab isko import karte hain |
| `accounts/admin.py` | Django admin panel mein User/Department dikhane ka config | Sirf `/admin/` (raw Django admin) ke liye |
| `accounts/management/commands/create_demo_users.py` | `python manage.py create_demo_users` — `.env` ke `DEMO_*` vars se ek admin/manager/user demo account bana/update karta hai | `.env` ke `DEMO_ADMIN_EMAIL` waghera read karta hai |
| `accounts/management/commands/backup_database.py` | `python manage.py backup_database` — production Postgres ka backup lekar S3-compatible storage par upload karta hai, purane backups delete karta hai | `docs/BACKUP_RESTORE.md` mein poora procedure hai |
| `templates/accounts/login.html` | Login page (split-screen design) | `accounts:login` URL |
| `templates/accounts/locked_out.html` | "Too many failed attempts" page — 5 galat password attempts ke baad dikhta hai | `accounts/axes_hooks.py` render karta hai |
| `templates/accounts/signup.html` | Signup page | `accounts:signup` URL |
| `templates/accounts/dashboard.html` | Login ke baad ka landing page | `accounts:dashboard` URL |
| `templates/accounts/profile.html` | Profile settings (naam + password change) | `accounts:profile` URL |

---

## 3. `chat` app — AI Chat Interface

**Responsibility:** Conversations, Messages, AI provider calls (OpenAI/Anthropic), streaming replies, file uploads, pin/search/delete, Plan-based access.

| File | Kaam | Kis se link hai |
|---|---|---|
| `chat/models.py` | **`ModelConfig`** (AI models list + pricing + `display_name`), **`UserModelPermission`** (per-user explicit allow/deny override), **`Conversation`** (pin/soft-delete fields bhi hain), **`Message`** (attachment fields + `served_from_cache`), **`MessageFeedback`** (thumbs up/down + comment + denormalized `model_used`), **`PromptTemplate`** (personal ya department-wide "Team" template) | `accounts.User`/`Department` ko reference karta hai; `Conversation.objects` sirf non-deleted dikhata hai (`Conversation.all_objects` sab kuch) |
| `chat/utils.py` | `group_conversations()` — sidebar list ko "Today / Yesterday / Previous 7 Days / ..." mein group karta hai | `chat/views.py::chat_home` use karta hai |
| `chat/markdown_utils.py` | AI reply ka Markdown → safe HTML render karta hai (bleach se sanitize, taake koi prompt-injected reply raw HTML/script na chala sake) | `chat/templatetags/chat_extras.py` ka filter isko call karta hai |
| `chat/templatetags/chat_extras.py` | Template filters: `render_markdown`, `to_offset` (usage-ring animation ke liye) | `_message_bubble.html`, `_usage_ring.html` use karte hain |
| `chat/document_extraction.py` | Uploaded file (PDF/Word/Excel/text) se text nikalta hai aur `[BEGIN/END ATTACHED DOCUMENT]` delimiters mein wrap karta hai — **prompt-injection defense**: model ko instruction di jati hai ke ye sirf reference data hai, commands nahi. Real payload se tested (`chat/tests.py::AttachmentContextInPromptTests`) | `chat/views.py::post_message` call karta hai |
| `chat/export.py` | Conversation ko PDF/Markdown/plain-text mein export karta hai — Markdown formatting PDF mein properly render hoti hai, raw syntax nahi | `chat/views.py` ke export views use karte hain |
| `chat/model_sync.py` | Har provider ke live models-list API (`GET /v1/models` waghera) se real model IDs fetch karta hai — chat-capable models filter karta hai (embeddings/whisper/dall-e waghera hata deta hai). OpenAI ke against **real API se verified**; Anthropic key abhi tak available nahi hui isliye wo path untested hai | `governance/views.py::sync_models_preview`/`sync_models_import` isko call karte hain |
| `chat/response_cache.py` | Exact-match response caching — Redis (ya `LocMemCache` agar Redis nahi hai) mein user+model+poori history ka hash key bana kar 1-hour TTL ke sath store karta hai. Alag users kabhi cache share nahi karte | `chat/views.py::stream_message` isko check/store dono ke liye call karta hai |
| `chat/tasks.py` | Daily Celery task — naye providers models discover hone par admins ko notify karta hai (`notify()` call), khud kuch enable nahi karta | `notifications/notify.py` use karta hai; Celery Beat se schedule hota hai |
| `chat/providers.py` | OpenAI aur Anthropic API calls ka common interface (retries/timeout bhi yahan configured hain — network blips ke against resilience) — **naya AI provider add karna ho to yahan** | `chat/views.py` aur `chat/router.py` use karte hain |
| `chat/router.py` | Smart routing (kaunsa model use hoga) — **ab user ke Plan ke allowed_models se restrict hota hai** (`governance/plans.py::effective_allowed_model_ids`), phir UserModelPermission overrides | `chat/views.py` use karta hai |
| `chat/prompts.py` | System prompt banane ka logic (base prompt + department-specific instructions + attached-document delimiter instruction) | `governance.models.SystemPromptVersion` import karta hai |
| `chat/views.py` | **Sabse bari file.** Chat home, message send/receive, streaming (SSE), file upload/download, pin/unpin, soft-delete, sidebar search, `request_upgrade`, message edit (regenerates forward) / regenerate (replaces in place), feedback, export, prompt templates, response caching, usage-warning notification trigger | `governance/limits.py`, `governance/plans.py`, `chat/router.py`, `chat/providers.py`, `chat/response_cache.py`, `chat/export.py`, `notifications/notify.py` — sab yahan milte hain |
| `chat/urls.py` | `/chat/`, `/chat/conversations/...` (edit/regenerate/feedback/export sab isi ke andar), `/chat/templates/`, `/chat/request-upgrade/` | `config/urls.py` mein include hai |
| `chat/admin.py` | Django admin mein ModelConfig/Conversation/Message dikhane ka config | Sirf `/admin/` ke liye |
| `chat/management/commands/seed_models.py` | Purana command jo shuru mein kuch AI models seed karta tha — ab admin "Sync Models" button (real API se) ya "Add model" form se model add kar sakta hai (`governance:models` page) | `chat/models.py` ka `ModelConfig` use karta hai |
| `templates/chat/chat_home.html` | **Poora chat interface** — sidebar (search box + pinned/grouped conversations + usage widget + request-upgrade button) + panel (messages + composer + model dropdown). Mobile par sidebar ek slide-in drawer ban jata hai (hamburger icon) | `chat:chat_home` URL |
| `templates/chat/_conversation_list.html` | Sidebar ki conversation list ka fragment (Pinned section + date-grouped sections) — pin/delete ke baad isi ko htmx se refresh kiya jata hai | `chat_home.html` include karta hai, `toggle_pin`/`delete_conversation` views isko re-render karte hain |
| `templates/chat/_conversation_item.html` | Ek conversation ki row (pin icon + delete icon) | `_conversation_list.html` include karta hai |
| `templates/chat/_message_bubble.html` | Ek message ka bubble (user ya assistant) — assistant wala Markdown render karta hai | `chat_home.html` aur `_message_pending.html` dono include karte hain |
| `templates/chat/_message_pending.html` | Naya message bhejne ke baad ka fragment (user bubble + streaming assistant bubble via SSE) | `chat/views.py::post_message` return karta hai |
| `templates/chat/_usage_widget.html` | Sidebar ka "Your usage" widget — usage ring + progress bars, Plan ke limits ke against | `chat_home.html` mein har 20 second refresh hota hai (htmx) |
| `templates/chat/_usage_ring.html` | **Signature circular progress ring** (SVG) — sidebar (chhota) aur admin Overview (bara) dono jagah reuse hota hai | `_usage_widget.html` aur `governance/dashboard.html` dono include karte hain |
| `templates/chat/_limit_exceeded.html` | Error message jab usage limit, Plan expiry, ya file-upload limit cross ho jaye | `chat/views.py` use karta hai |
| `templates/chat/_message_feedback.html` | Thumbs up/down control (assistant message ke neeche) — down par optional comment box | `_message_bubble.html` include karta hai |
| `templates/chat/_prompt_template_picker.html` | Personal + department "Team"-badged templates ki list, composer se `/` ya icon se khulta hai | `chat_home.html` include karta hai |
| `templates/chat/_conversation_messages.html`, `_pending_assistant_row.html` | Poori conversation ki message list, aur streaming ke dauran ek pending assistant row (typing-dots indicator, phir stream se replace hoti hai) | `chat_home.html` aur `stream_message` view use karte hain |
| Quick-switcher, keyboard shortcuts, onboarding tour | `chat_home.html` ke andar hi JS/markup hai (Ctrl/Cmd+K, Enter/Shift+Enter/Esc, 3-4 step guided tour naye users ke liye) — koi alag file nahi, `static/css/main.css` mein styling | `accounts/models.py::User.has_seen_onboarding` se track hota hai |

---

## 4. `governance` app — Admin Dashboard + Plan/Tier System (poora control yahan hai)

**Responsibility:** Admin ke liye sab kuch — users manage karna, **Plans (tier-based access control)**, models/pricing, per-user permissions, usage/upload limits, upgrade requests, audit logs, department + system prompts, charts, search/filter.

| File | Kaam | Kis se link hai |
|---|---|---|
| `governance/models.py` | **`Plan`** (tier: Demo/Standard/Premium — models/limits/feature-flags bundle), **`UserPlanAssignment`** (kaun kis plan par hai + expiry), **`UpgradeRequest`** (self-service upgrade requests), `SystemPromptVersion`, **`UsageLimit`** (per-user/department override — Plan se upar priority), `AuditLog` | `accounts.Department`, `accounts.User`, `chat.ModelConfig` (Plan ka `allowed_models` M2M) |
| `governance/plans.py` | **Plan resolution ka poora dimagh.** `get_plan_status()` (active/grace/expired), `assign_plan()`, `effective_allowed_model_ids()`, `plan_limit_fallback()`, `has_feature()`, `engagement_score()`, `check_session_creation_limit()`, **`get_user_overrides()`/`count_user_overrides()`/`clear_user_overrides()`** (per-user Plan-override visibility, added). Precedence: personal `UsageLimit` > department `UsageLimit` > user ka Plan > kuch nahi | `chat/router.py`, `governance/limits.py`, `chat/views.py` sab isko call karte hain |
| `governance/limits.py` | Usage limit check (`check_usage_limits` — ab Plan expiry/grace bhi yahan check hota hai) aur file-upload validation (`validate_upload`) | `chat/views.py` isko directly call karta hai har message/upload par |
| `governance/audit.py` | `log_action()` helper — har admin action (role change, plan change, model enable, lockout, waghera) yahan se AuditLog mein likha jata hai | `governance/views.py`, `accounts/signals.py` isko call karte hain |
| `governance/templatetags/governance_extras.py` | `dict_get` filter — templates mein ek dict ko variable-key se lookup karne ke liye (e.g. Users list mein har row ka plan-status) | `_users_table.html` use karta hai |
| `governance/views.py` | **Sabse bari file is app ki.** Dashboard (charts + org usage ring), Users list (search/filter + Plan column + bulk plan-assign + **overrides badge/view/clear**), **Plans CRUD**, **Upgrade Requests**, Models (add/pricing/enable + search/filter + **Sync Models**), Model Permissions, Limits (CRUD + search), Departments (CRUD + search + **department templates**), Audit Logs (search/filter/pagination), System Prompt, **Feedback review**, **Usage export (CSV/Excel/monthly summary)** | `chat.models`, `accounts.models`, `governance.plans` — sab import karta hai |
| `governance/urls.py` | `/governance/...` sab routes — including `/governance/plans/`, `/governance/upgrade-requests/`, `/governance/users/<id>/change-plan/`, `/governance/users/bulk-change-plan/`, `/governance/users/<id>/overrides/` (view/clear), `/governance/models/sync/`, `/governance/usage/export.csv\|.xlsx`, `/governance/feedback/` | `config/urls.py` mein include hai |
| `governance/admin.py` | Django admin mein ye models dikhane ka config | Sirf `/admin/` ke liye (fallback/advanced use) |
| `templates/governance/dashboard.html` | Overview + Charts (Chart.js, 14-day zero-filled data, empty-states) + **org-wide usage ring** | `governance:dashboard` URL |
| `templates/governance/users.html` + `_users_table.html` | Users list — search/role/status/plan filters, role/department dropdown, **Plan column (days-left/grace/expired badge + engagement 🔥 flag)**, inline Change-Plan, bulk checkbox "assign plan to selected" | `governance:users` URL |
| `templates/governance/plans.html` + `plan_form.html` | **Plan management** — list + create/edit form (limits, allowed-models checkboxes, feature-flag checkboxes, default/visibility toggles) | `governance:plans`, `plan_new`, `plan_edit` URLs |
| `templates/governance/_plan_downgrade_confirm.html` | Jab admin kisi user ko aisay plan par downgrade kare jiski limit already cross ho chuki ho, ye confirmation step dikhata hai | `governance:change_user_plan` view isko render karta hai |
| `templates/governance/upgrade_requests.html` | Pending self-service upgrade requests — Approve (Users page pe le jata hai, pre-filtered) / Dismiss | `governance:upgrade_requests` URL |
| `templates/governance/models.html` + `_models_table.html` + `model_sync.html` | AI Models list — add/pricing/enable/disable, search/status filter, **Sync Models** (real provider API se checklist, admin select karta hai kaunse enable karne hain) | `governance:models`, `sync_models_preview` URLs |
| `templates/governance/model_permissions.html` | Ek specific model ke liye "kaun use kar sakta hai" (per-user override, Plan ke upar) | `governance:model_permissions` URL |
| `templates/governance/limits.html` + `_limits_table.html` + `limit_form.html` | Usage/Upload limits (per-user/department override) ki list (search) + add/edit form | `governance:limits`, `limit_new`, `limit_edit` URLs |
| `templates/governance/user_overrides.html` | Ek user ke personal `UsageLimit`/`UserModelPermission` overrides dikhata hai + "Clear all overrides" button (Plan defaults par wapas) | `governance:user_overrides` URL — Users list ke "N custom overrides — view/clear" link se |
| `templates/governance/departments.html` + `_departments_table.html` + `department_templates.html` | Departments CRUD + search, plus **department-wide "Team" prompt templates** management | `governance:departments`, `department_templates` URLs |
| `templates/governance/system_prompt.html` | Ek department ka system prompt edit karna | `governance:system_prompt` URL |
| `templates/governance/usage.html` + `_usage_table.html` | Per-user usage/cost table — search/model/date-range filter, **Export CSV/Excel + Monthly summary export** buttons, cache-hit-rate/estimated-cost-saved metric | `governance:usage`, `export_usage_csv/xlsx` URLs |
| `templates/governance/audit_logs.html` + `_audit_logs_table.html` | Audit log history — search/action-type/date-range filter, pagination (filters preserve karta hai) | `governance:audit_logs` URL |
| `templates/governance/feedback.html` + `_feedback_table.html` | Response feedback review — recent thumbs-down + context, model se filter | `governance:feedback` URL |

---

## 5. `notifications` app — In-app Bell + Email Notifications

**Responsibility:** In-app notification bell, email sending (via Celery), per-user per-type email opt-out. Zero tests before Phase 6 — now has 22.

| File | Kaam | Kis se link hai |
|---|---|---|
| `notifications/models.py` | **`Notification`** (title/body/is_read/email_sent), **`NotificationType`** choices (usage_warning/plan_change/trial_expiring/trial_expired/admin_change/model_sync_available), **`NotificationPreference`** (per-type email on/off — missing row = "email everything", safe default) | `accounts.User` ko reference karta hai |
| `notifications/notify.py` | `notify()` — **har trigger isi se guzarta hai.** In-app row hamesha banata hai; email sirf preference allow kare to Celery task queue karta hai. `recently_notified()` dedup helper (same type ko 24h mein dobara na bheje) | `chat/views.py`, `governance/views.py`, `notifications/tasks.py` sab isko call karte hain |
| `notifications/tasks.py` | `send_notification_email` (Celery task — branded HTML email render+send), `sweep_expiring_demo_plans` (daily Beat task — trial-expiring/trial-expired notify) | `notify()` `.delay()` karta hai; Beat schedule `django-celery-beat` se DB mein hai |
| `notifications/views.py` | Bell dropdown fragment, mark-read/mark-all-read, Settings ka preferences form | `notifications/urls.py` se wire hain |
| `notifications/urls.py` | `/notifications/bell/`, `/mark-all-read/`, `/preferences/` | `config/urls.py` mein include hai |
| `notifications/tests.py` | **22 tests** — `notify()` khud, usage-warning trigger, admin-change/plan-change triggers (real views ke zariye), trial-expiring/expired sweep (dedup-on-rerun sameet), bell dropdown, preference opt-out. Ye file Phase 6 se pehle exist hi nahi karti thi | `python manage.py test notifications` |
| `templates/notifications/_bell_dropdown.html` | Bell icon + unread badge + dropdown list | `templates/base.html` include karta hai (har page par visible) |
| `templates/notifications/email_generic.html` | Har notification email ka branded HTML template (portal ke colors/logo consistent) | `notifications/tasks.py::send_notification_email` render karta hai |

---

## 6. Shared/Layout files

| File | Kaam |
|---|---|
| `templates/base.html` | **Sab pages ka parent template.** Sidebar navigation (Admin sub-nav mein Plans/Upgrade Requests bhi hain), mobile hamburger topbar + slide-in drawer overlay, notification bell, `{% get_current_language %}`/`{% get_current_language_bidi %}` se `lang`/`dir` attributes (Urdu/Arabic RTL), `{% block content %}` jahan har page apna content dalta hai |
| `static/css/main.css` | **Poori app ki styling ek hi file mein.** Design tokens (colors/spacing/type-scale/mono-font) `:root` mein top par hain. Filter-toolbar, usage-ring, sidebar redesign, mobile `@media (max-width: 720px)` overlay pattern, aur `[lang="ur"]`/`[lang="ar"]` font-family rules (Noto Nastaliq Urdu / Noto Naskh Arabic) isi file mein hain |

---

## 7. Naya kaam karte waqt kahan jayein (cheat-sheet)

| Karna kya hai | Kis file mein jayein |
|---|---|
| Naya field User/Department mein add karna | `accounts/models.py` → phir `makemigrations` |
| Naya AI provider (e.g. Google Gemini) add karna | `chat/providers.py` mein naya class, `chat/models.py` mein `ModelConfig.Provider` choice add karo |
| Chat ka UI/design badalna | `templates/chat/chat_home.html`, `_message_bubble.html`, aur `static/css/main.css` ka "Chat" section |
| Admin dashboard mein naya page add karna | `governance/views.py` (naya view) + `governance/urls.py` (naya route) + `templates/governance/` mein naya template + `templates/base.html` ke sidebar mein link |
| Admin list page mein search/filter add karna | View mein `FilterableListMixin` use karo (`governance/views.py` mein already kai jagah hai), `_xxx_table.html` partial banao, toolbar form mein `hx-get`/`hx-trigger` **har individual input/select par** lagao (wrapping `<form>` par nahi — ye kaam nahi karta) |
| Plan ke limits/models/features badalna | Admin UI se: `/governance/plans/` → koi bhi plan edit karo. Code mein hardcoded nahi hai |
| Naya feature flag add karna (jaisay `file_upload`) | `governance/models.py::KNOWN_FEATURE_FLAGS` list mein add karo, phir `governance/plans.py::has_feature()` se check karo |
| Koi limit/restriction ka rule badalna | `governance/limits.py` (per-user override) ya `governance/plans.py` (Plan-level default) |
| Naya permission/role rule | `accounts/permissions.py` |
| Login lockout ki settings (attempts/cooldown) badalna | `config/settings.py` ke `AXES_*` settings |
| Poori app ka color/font badalna | `static/css/main.css` ke `:root` wale design tokens |
| Mobile responsive kuch tootay to | `static/css/main.css` ka `@media (max-width: 720px)` block, `templates/base.html` ka hamburger/drawer markup |
| Database backup/restore | `docs/BACKUP_RESTORE.md` — exact commands wahan hain |
| Email templates, naye URLs | respective app ka `urls.py` + `views.py` |
| Naya translatable string add karna (UI text) | Template mein `{% trans "..." %}`/`{% blocktrans %}` ya Python mein `gettext`/`ngettext` use karo, phir `locale/ur/LC_MESSAGES/django.po` aur `locale/ar/LC_MESSAGES/django.po` dono mein wahi msgid ka translation add karo, phir `.mo` compile karo (is machine par `xgettext`/`msgfmt` nahi hai — `polib` + Django ke `templatize()` se manual extraction ka tareeqa upar `locale/` row mein likha hai) |
| IP se language guess ki country-list badalna (kaunse countries Urdu/Arabic) | `accounts/geo.py` ke `_ARABIC_COUNTRIES`/`_URDU_COUNTRIES` sets |
| Naya notification type add karna | `notifications/models.py::NotificationType` + `EMAIL_TOGGLE_LABELS` mein add karo, phir jahan trigger hona hai wahan `notify()` call karo |
| Response caching ka TTL ya scope badalna | `chat/response_cache.py` |
| Per-user Plan override dikhana/clear karna | `governance/plans.py::get_user_overrides`/`clear_user_overrides`, UI `templates/governance/user_overrides.html` |

---

## 8. Data flow example — "User message bhejta hai"

```
User composer form submit karta hai (chat_home.html)
        │  (hx-post, model_id dropdown se hx-include)
        ▼
chat/urls.py → chat/views.py::post_message
        │
        ├─→ governance/limits.py::check_usage_limits()
        │       └─→ governance/plans.py::get_plan_status()   (expired/grace hai to yahin block ho jata hai)
        ├─→ governance/limits.py::validate_upload()          (agar file hai — plan ka file_upload feature flag bhi check)
        ├─→ chat/models.py::Message.objects.create()          (DB mein save)
        └─→ templates/chat/_message_pending.html return       (HTML fragment)
                │  (SSE connect shuru)
                ▼
chat/urls.py → chat/views.py::stream_message
        │
        ├─→ chat/router.py::classify_complexity() + select_model_candidates()
        │       └─→ governance/plans.py::effective_allowed_model_ids()   (Plan ke allowed_models se restrict)
        ├─→ chat/prompts.py::build_system_prompt()  (governance.SystemPromptVersion se department prompt)
        ├─→ chat/providers.py::get_provider()        (OpenAI/Anthropic ko actual call, retry-safe)
        └─→ Message.save()  (final reply + tokens + cost DB mein)
```

## 9. Data flow example — "Admin kisi user ka Plan change karta hai"

```
Admin Users page par ek row ka "Change" button click karta hai
        │  (POST, plan_id)
        ▼
governance/urls.py → governance/views.py::change_user_plan
        │
        ├─→ _usage_exceeds_plan()   (agar current usage target-plan ki limit se zyada hai)
        │       └─→ True: templates/governance/_plan_downgrade_confirm.html dikhao, ruk jao
        │       └─→ False (ya already confirmed): aage badho
        ├─→ governance/plans.py::assign_plan()   (UserPlanAssignment update, expiry compute agar demo plan hai)
        └─→ governance/audit.py::log_action()    ("user.plan_change" audit trail mein)
```

## 10. Data flow example — "5 galat password attempts"

```
User login form 5 baar galat password se submit karta hai
        │
        ▼
django-axes (AxesStandaloneBackend, AUTHENTICATION_BACKENDS mein sabse pehle)
        │
        ├─→ 5th attempt par: account lock (AXES_FAILURE_LIMIT=5, AXES_COOLOFF_TIME=20 min) — live-tested: 429 + locked-out page
        │       ├─→ accounts/axes_hooks.py::axes_lockout_response()   (custom locked-out page)
        │       └─→ accounts/signals.py::_log_axes_lockout()          (audit log: "auth.lockout", IP included)
```

## 11. Data flow example — "User apni usage limit ke 85% par pohonch jata hai"

```
chat/views.py::post_message ek message save karne ke baad
        │
        ▼
chat/views.py::_notify_if_usage_warning(user)
        │
        ├─→ governance/limits.py::get_usage_status(user)     (warn=True agar koi metric 80%+ hai)
        ├─→ notifications/notify.py::recently_notified()      (pichle 24h mein already bheja to skip)
        └─→ notifications/notify.py::notify()
                │
                ├─→ notifications/models.py::Notification.objects.create()   (in-app row — bell badge turant badh jata hai)
                └─→ agar NotificationPreference allow kare (default: haan):
                        └─→ notifications/tasks.py::send_notification_email.delay()
                                └─→ templates/notifications/email_generic.html   (recipient ki preferred_language mein render)
```

---

*Last updated: 2026-08-30 (Section B feature pack — export/templates/shortcuts/file-upload/multi-language/usage-export/notifications/mobile-review/onboarding, Section C reliability — feedback/brute-force/backup/caching, IP-based language detection, aur per-user Plan-override view/clear UI ke baad; sab kuch real live-tested evidence ke saath, ab CI par bhi pass ho raha hai). Jab bhi naye app/model/major feature add ho, is document ko bhi update kar dena.*
