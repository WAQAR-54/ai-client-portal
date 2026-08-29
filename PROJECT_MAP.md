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
| `requirements.txt` | Python packages ki list. `django-axes` (login lockout), `boto3` (S3 backups) is session mein add hui hain |
| `AI_Client_Portal_Spec.md` | Original spec document jis par poora project based hai |
| `PROJECT_MAP.md` | Yehi file jo aap abhi padh rahe hain |
| `docs/BACKUP_RESTORE.md` | Database backup/restore ka poora procedure — exact commands, env vars, Railway cron setup. **Emergency mein sabse pehle yahan jayein** |
| `logs/app.log` | Runtime error log (gitignored) — kisi bhi AI provider call ya server error ka **real** traceback yahan milta hai, generic user-facing message ke peeche |
| `media/` | User-uploaded chat attachments (gitignored — accidentally ek test file commit ho gayi thi purane commit mein, ab aage se aisa nahi hoga) |
| `railway.json`, `mise.toml` | Railway deployment configs |
| `deployment/` | VPS (non-Railway) deployment ke configs: Gunicorn, Nginx, systemd |
| `.github/workflows/ci.yml` | GitHub Actions — har push par tests automatically chalte hain |

---

## 2. `accounts` app — Login, Signup, Users, Departments, Profile, Login Security

**Responsibility:** Authentication, User model, Department model, RBAC, profile settings, brute-force login protection.

| File | Kaam | Kis se link hai |
|---|---|---|
| `accounts/models.py` | **`User`** (email, role, department) aur **`Department`** (name, budget cap) models yahan define hain | Almost har app isko import karta hai (`chat`, `governance`) |
| `accounts/signals.py` | `post_save` signal — **naya user ban'ne par automatically Demo plan assign** karta hai (`governance/plans.py::assign_default_plan_if_missing`). Login-lockout hone par audit log entry likhne wala signal bhi yahan hai | `accounts/apps.py::ready()` se connect hota hai |
| `accounts/axes_hooks.py` | Login lockout hone par custom (on-brand) error page dikhane wala callable | `config/settings.py::AXES_LOCKOUT_CALLABLE` isko point karta hai |
| `accounts/forms.py` | Login form, Signup form, Profile edit form | `accounts/views.py` use karta hai |
| `accounts/views.py` | Login, Logout, Signup, Dashboard, Profile (naam edit + password change) views | `accounts/urls.py` se wire hain, templates render karte hain |
| `accounts/urls.py` | `/accounts/login/`, `/accounts/signup/`, `/accounts/profile/` waghera | `config/urls.py` mein include hai |
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
| `chat/models.py` | **`ModelConfig`** (AI models list + pricing), **`UserModelPermission`** (per-user explicit allow/deny override), **`Conversation`** (pin/soft-delete fields bhi hain), **`Message`** (attachment fields) | `accounts.User`/`Department` ko reference karta hai; `Conversation.objects` sirf non-deleted dikhata hai (`Conversation.all_objects` sab kuch) |
| `chat/utils.py` | `group_conversations()` — sidebar list ko "Today / Yesterday / Previous 7 Days / ..." mein group karta hai | `chat/views.py::chat_home` use karta hai |
| `chat/markdown_utils.py` | AI reply ka Markdown → safe HTML render karta hai (bleach se sanitize, taake koi prompt-injected reply raw HTML/script na chala sake) | `chat/templatetags/chat_extras.py` ka filter isko call karta hai |
| `chat/templatetags/chat_extras.py` | Template filters: `render_markdown`, `to_offset` (usage-ring animation ke liye) | `_message_bubble.html`, `_usage_ring.html` use karte hain |
| `chat/providers.py` | OpenAI aur Anthropic API calls ka common interface (retries/timeout bhi yahan configured hain — network blips ke against resilience) — **naya AI provider add karna ho to yahan** | `chat/views.py` aur `chat/router.py` use karte hain |
| `chat/router.py` | Smart routing (kaunsa model use hoga) — **ab user ke Plan ke allowed_models se restrict hota hai** (`governance/plans.py::effective_allowed_model_ids`), phir UserModelPermission overrides | `chat/views.py` use karta hai |
| `chat/prompts.py` | System prompt banane ka logic (base prompt + department-specific instructions) | `governance.models.SystemPromptVersion` import karta hai |
| `chat/views.py` | **Sabse bari file.** Chat home, message send/receive, streaming (SSE), file upload/download, pin/unpin, soft-delete, sidebar search, `request_upgrade` | `governance/limits.py`, `governance/plans.py`, `chat/router.py`, `chat/providers.py` — sab yahan milte hain |
| `chat/urls.py` | `/chat/`, `/chat/conversations/...`, `/chat/request-upgrade/` | `config/urls.py` mein include hai |
| `chat/admin.py` | Django admin mein ModelConfig/Conversation/Message dikhane ka config | Sirf `/admin/` ke liye |
| `chat/management/commands/seed_models.py` | Purana command jo shuru mein kuch AI models seed karta tha — ab admin khud "Add model" form se model add kar sakta hai (`governance:models` page) | `chat/models.py` ka `ModelConfig` use karta hai |
| `templates/chat/chat_home.html` | **Poora chat interface** — sidebar (search box + pinned/grouped conversations + usage widget + request-upgrade button) + panel (messages + composer + model dropdown). Mobile par sidebar ek slide-in drawer ban jata hai (hamburger icon) | `chat:chat_home` URL |
| `templates/chat/_conversation_list.html` | Sidebar ki conversation list ka fragment (Pinned section + date-grouped sections) — pin/delete ke baad isi ko htmx se refresh kiya jata hai | `chat_home.html` include karta hai, `toggle_pin`/`delete_conversation` views isko re-render karte hain |
| `templates/chat/_conversation_item.html` | Ek conversation ki row (pin icon + delete icon) | `_conversation_list.html` include karta hai |
| `templates/chat/_message_bubble.html` | Ek message ka bubble (user ya assistant) — assistant wala Markdown render karta hai | `chat_home.html` aur `_message_pending.html` dono include karte hain |
| `templates/chat/_message_pending.html` | Naya message bhejne ke baad ka fragment (user bubble + streaming assistant bubble via SSE) | `chat/views.py::post_message` return karta hai |
| `templates/chat/_usage_widget.html` | Sidebar ka "Your usage" widget — usage ring + progress bars, Plan ke limits ke against | `chat_home.html` mein har 20 second refresh hota hai (htmx) |
| `templates/chat/_usage_ring.html` | **Signature circular progress ring** (SVG) — sidebar (chhota) aur admin Overview (bara) dono jagah reuse hota hai | `_usage_widget.html` aur `governance/dashboard.html` dono include karte hain |
| `templates/chat/_limit_exceeded.html` | Error message jab usage limit, Plan expiry, ya file-upload limit cross ho jaye | `chat/views.py` use karta hai |

---

## 4. `governance` app — Admin Dashboard + Plan/Tier System (poora control yahan hai)

**Responsibility:** Admin ke liye sab kuch — users manage karna, **Plans (tier-based access control)**, models/pricing, per-user permissions, usage/upload limits, upgrade requests, audit logs, department + system prompts, charts, search/filter.

| File | Kaam | Kis se link hai |
|---|---|---|
| `governance/models.py` | **`Plan`** (tier: Demo/Standard/Premium — models/limits/feature-flags bundle), **`UserPlanAssignment`** (kaun kis plan par hai + expiry), **`UpgradeRequest`** (self-service upgrade requests), `SystemPromptVersion`, **`UsageLimit`** (per-user/department override — Plan se upar priority), `AuditLog` | `accounts.Department`, `accounts.User`, `chat.ModelConfig` (Plan ka `allowed_models` M2M) |
| `governance/plans.py` | **Plan resolution ka poora dimagh.** `get_plan_status()` (active/grace/expired), `assign_plan()`, `effective_allowed_model_ids()`, `plan_limit_fallback()`, `has_feature()`, `engagement_score()`, `check_session_creation_limit()`. Precedence: personal `UsageLimit` > department `UsageLimit` > user ka Plan > kuch nahi | `chat/router.py`, `governance/limits.py`, `chat/views.py` sab isko call karte hain |
| `governance/limits.py` | Usage limit check (`check_usage_limits` — ab Plan expiry/grace bhi yahan check hota hai) aur file-upload validation (`validate_upload`) | `chat/views.py` isko directly call karta hai har message/upload par |
| `governance/audit.py` | `log_action()` helper — har admin action (role change, plan change, model enable, lockout, waghera) yahan se AuditLog mein likha jata hai | `governance/views.py`, `accounts/signals.py` isko call karte hain |
| `governance/templatetags/governance_extras.py` | `dict_get` filter — templates mein ek dict ko variable-key se lookup karne ke liye (e.g. Users list mein har row ka plan-status) | `_users_table.html` use karta hai |
| `governance/views.py` | **Sabse bari file is app ki.** Dashboard (charts + org usage ring), Users list (search/filter + Plan column + bulk plan-assign), **Plans CRUD**, **Upgrade Requests**, Models (add/pricing/enable + search/filter), Model Permissions, Limits (CRUD + search), Departments (CRUD + search), Audit Logs (search/filter/pagination), System Prompt | `chat.models`, `accounts.models`, `governance.plans` — sab import karta hai |
| `governance/urls.py` | `/governance/...` sab routes — including `/governance/plans/`, `/governance/upgrade-requests/`, `/governance/users/<id>/change-plan/`, `/governance/users/bulk-change-plan/` | `config/urls.py` mein include hai |
| `governance/admin.py` | Django admin mein ye models dikhane ka config | Sirf `/admin/` ke liye (fallback/advanced use) |
| `templates/governance/dashboard.html` | Overview + Charts (Chart.js, 14-day zero-filled data, empty-states) + **org-wide usage ring** | `governance:dashboard` URL |
| `templates/governance/users.html` + `_users_table.html` | Users list — search/role/status/plan filters, role/department dropdown, **Plan column (days-left/grace/expired badge + engagement 🔥 flag)**, inline Change-Plan, bulk checkbox "assign plan to selected" | `governance:users` URL |
| `templates/governance/plans.html` + `plan_form.html` | **Plan management** — list + create/edit form (limits, allowed-models checkboxes, feature-flag checkboxes, default/visibility toggles) | `governance:plans`, `plan_new`, `plan_edit` URLs |
| `templates/governance/_plan_downgrade_confirm.html` | Jab admin kisi user ko aisay plan par downgrade kare jiski limit already cross ho chuki ho, ye confirmation step dikhata hai | `governance:change_user_plan` view isko render karta hai |
| `templates/governance/upgrade_requests.html` | Pending self-service upgrade requests — Approve (Users page pe le jata hai, pre-filtered) / Dismiss | `governance:upgrade_requests` URL |
| `templates/governance/models.html` + `_models_table.html` | AI Models list — add/pricing/enable/disable, search/status filter | `governance:models` URL |
| `templates/governance/model_permissions.html` | Ek specific model ke liye "kaun use kar sakta hai" (per-user override, Plan ke upar) | `governance:model_permissions` URL |
| `templates/governance/limits.html` + `_limits_table.html` + `limit_form.html` | Usage/Upload limits (per-user/department override) ki list (search) + add/edit form | `governance:limits`, `limit_new`, `limit_edit` URLs |
| `templates/governance/departments.html` + `_departments_table.html` | Departments CRUD + search | `governance:departments` URL |
| `templates/governance/system_prompt.html` | Ek department ka system prompt edit karna | `governance:system_prompt` URL |
| `templates/governance/usage.html` + `_usage_table.html` | Per-user usage/cost table — search/model/date-range filter | `governance:usage` URL |
| `templates/governance/audit_logs.html` + `_audit_logs_table.html` | Audit log history — search/action-type/date-range filter, pagination (filters preserve karta hai) | `governance:audit_logs` URL |

---

## 5. Shared/Layout files

| File | Kaam |
|---|---|
| `templates/base.html` | **Sab pages ka parent template.** Sidebar navigation (Admin sub-nav mein Plans/Upgrade Requests bhi hain), mobile hamburger topbar + slide-in drawer overlay, `{% block content %}` jahan har page apna content dalta hai |
| `static/css/main.css` | **Poori app ki styling ek hi file mein.** Design tokens (colors/spacing/type-scale/mono-font) `:root` mein top par hain. Filter-toolbar, usage-ring, sidebar redesign, aur mobile `@media (max-width: 720px)` overlay pattern isi file mein hain |

---

## 6. Naya kaam karte waqt kahan jayein (cheat-sheet)

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

---

## 7. Data flow example — "User message bhejta hai"

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

## 8. Data flow example — "Admin kisi user ka Plan change karta hai"

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

## 9. Data flow example — "5 galat password attempts"

```
User login form 5 baar galat password se submit karta hai
        │
        ▼
django-axes (AxesStandaloneBackend, AUTHENTICATION_BACKENDS mein sabse pehle)
        │
        ├─→ 5th attempt par: account lock (AXES_FAILURE_LIMIT=5, AXES_COOLOFF_TIME=20 min)
        ├─→ accounts/axes_hooks.py::axes_lockout_response()   (custom locked-out page)
        └─→ accounts/signals.py::_log_axes_lockout()          (audit log: "auth.lockout", IP included)
```

---

*Last updated: 2026-08-29 (Plan/Tier system, search/filter, security audit fixes, mobile-responsive redesign, login brute-force protection, aur database backup strategy ke baad). Jab bhi naye app/model/major feature add ho, is document ko bhi update kar dena.*
