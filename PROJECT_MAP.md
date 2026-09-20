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
| `config/urls.py` | Root URL routing — yahan se har app ke `urls.py` ko include kiya gaya hai. `/healthz/` (DB-only check, Docker/CI ke liye — hamesha fast rehna chahiye) aur **`/healthz/deep/`** (naya, 2026-09-19 — Redis bhi check karta hai, alag endpoint isliye taake hot Docker-healthcheck path slow na ho) |
| `config/wsgi.py` / `config/asgi.py` | Server entrypoints (production/deployment ke liye, aksar chhedne ki zaroorat nahi) |
| `.env` | Real secrets (API keys, SECRET_KEY, backup credentials) — **kabhi commit nahi hoti**, sirf is machine par hai |
| `.env.example` | `.env` ka template, bina real values ke — naya setup karte waqt copy karke `.env` banayein. **Hamesha placeholder blank rakhein, kabhi real value yahan paste na karein** (ek dafa isi file mein real demo-account passwords accidentally aa gaye thay) |
| `requirements.txt` | Python packages ki list. **`defusedxml` yahan pin hona zaroori hai** (`chat/live_intelligence.py` import karta hai) — pehle sirf pip-audit ke zariye local mein aa raha tha, CI ne pakda (commit 8f57030 fail).  `django-axes` (login lockout), `boto3` (S3 backups), `celery`+`django-celery-beat` (background jobs/scheduler), `geoip2fast` (IP→country language guess), `openpyxl` (Excel export), `xhtml2pdf`/`reportlab` (PDF export), `arabic-reshaper`+`python-bidi` (Arabic/Urdu text shaping in PDFs), `sentry-sdk` — sab is project mein add hui hain |
| `AI_Client_Portal_Spec.md` | Original spec document jis par poora project based hai |
| `PROJECT_MAP.md` | Yehi file jo aap abhi padh rahe hain |
| `docs/BACKUP_RESTORE.md` | Database backup/restore ka poora procedure — exact commands, env vars, Railway cron setup. **Emergency mein sabse pehle yahan jayein.** Postgres/pg_dump/pg_restore path ab real production VPS par actually test ho chuka hai (2026-09-19) — real dump, ek alag throwaway database mein restore, row counts + real user rows byte-for-byte match confirm kiye. `BACKUP_S3_BUCKET` etc. abhi bhi VPS par set nahi — mechanism kaam karta hai, off-server backup abhi actually nahi ho raha jab tak wo set na ho |
| `docs/SECRETS.md` | Secrets audit (2026-09-19) — poori git history + current tree scan ki, koi real hardcoded secret nahi mila. `FIELD_ENCRYPTION_KEY` ka off-server backup abhi confirm nahi — is file mein action item hai |
| `logs/app.log` | Runtime error log (gitignored) — kisi bhi AI provider call ya server error ka **real** traceback yahan milta hai, generic user-facing message ke peeche |
| `media/` | User-uploaded chat attachments (gitignored — accidentally ek test file commit ho gayi thi purane commit mein, ab aage se aisa nahi hoga) |
| `locale/ur/`, `locale/ar/` | Urdu aur Arabic UI translations (`.po` source + compiled `.mo`) — 394 strings har language mein. Naya translatable string add karna ho to yahan dono files mein entry chahiye (extraction script is session mein banaya gaya tha, standard `makemessages`/`compilemessages` is machine par nahi chal saka kyunke `xgettext`/`msgfmt` install nahi thay) |
| `railway.json`, `mise.toml` | Railway deployment configs |
| `deployment/` | VPS (non-Railway) deployment ke configs: Gunicorn, Nginx, systemd |
| `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` | Docker setup — `web`/`worker`/`beat` teeno isi image se banate hain, entrypoint migrate+collectstatic khud chalata hai. Is machine (local dev) par Docker kabhi nahi tha, lekin `docker compose build` real production VPS par actually run karke confirm kiya (2026-09-19) — 3 images clean build hue, running containers untouched rahe |
| `.github/workflows/ci.yml` | GitHub Actions — har push par lint (flake8, black, **pip-audit** dependency scan) + tests chalte hain, phir SSH se VPS par deploy, phir `/healthz/` poll karke confirm. Deploy `needs:` se lint+test par depend karta hai — fail hote hi deploy nahi chalta. Real pushed commit par end-to-end pass + VPS par deploy hote confirm kiya (2026-09-19) |
| `.github/dependabot.yml` | Weekly pip + github-actions dependency update checks (2026-09-19 mein add hua) |

---

## 2. `accounts` app — Login, Signup, Users, Departments, Profile, Login Security

**Responsibility:** Authentication, User model, Department model, RBAC, profile settings, brute-force login protection.

| File | Kaam | Kis se link hai |
|---|---|---|
| `accounts/models.py` | **`User`** (email, role, department) aur **`Department`** (name, budget cap) models yahan define hain | Almost har app isko import karta hai (`chat`, `governance`) |
| `accounts/signals.py` | `post_save` signal — **naya user ban'ne par automatically Demo plan assign** karta hai (`governance/plans.py::assign_default_plan_if_missing`). Login-lockout hone par audit log entry likhne wala signal, **aur har successful login par bhi `auth.login` AuditLog entry** (Django ke `user_logged_in` signal se — password/post-MFA/Google sign-in teeno isi ek signal se guzarte hain) | `accounts/apps.py::ready()` se connect hota hai |
| `accounts/axes_hooks.py` | Login lockout hone par custom (on-brand) error page dikhane wala callable | `config/settings.py::AXES_LOCKOUT_CALLABLE` isko point karta hai |
| `accounts/middleware.py` | **`GeoLanguageMiddleware`** — naye/anonymous visitors ke liye IP se country guess karke starting language set karta hai (sirf pehli dafa, cookie set hone ke baad kabhi override nahi karta). **`UserLanguagePreferenceMiddleware`** — logged-in user ke DB mein stored `preferred_language` ko har request par activate karta hai (session/cookie se independent, isi liye "per-user not per-session" persist hota hai). **`RequestIDMiddleware`** (naya, 2026-09-19, remaining-audit pass) — har request ko ek short id deta hai (`request.id`, `X-Request-ID` response header, Sentry tag, aur logging formatter mein `req=...` — `RequestIDLogFilter` ke zariye, koi naya logging framework nahi, sirf existing formatter mein field). Reset `response.close()` (`_resource_closers`) par hota hai, `get_response()` ke baad `finally` mein nahi — kyunke `stream_message`'s `StreamingHttpResponse` ka generator body baad mein WSGI server iterate karta hai, middleware ke `__call__` khatam hone ke baad. Same file mein **`get_request_cache()`** — ek plain dict jo sirf ek request tak zinda rehta hai (`ContextVar`-based), `governance/features.py::role_has_feature`'s N+1 fix ke liye (see section 5) — Django ka cross-request cache (pehli koshish) TestCase transaction rollback ke sath stale ho jata tha, isliye request-scoped design chuna. | `config/settings.py::MIDDLEWARE` mein sab wired hain — RequestID sabse pehle, Geo `LocaleMiddleware` se pehle, User-preference Authentication ke baad |
| `config/health.py` (naya, 2026-09-19) | **Ek hi** DB/Redis health probe — `/healthz/`, `/healthz/deep/` aur governance ka System status teeno isi ko use karte hain. Fixed vocabulary lautata hai (`healthy` / `unavailable` / `not_configured`), kabhi exception text nahi (dono endpoints anonymous hain — pehle `str(exc)` DB/Redis ka error, host ya Redis URL (password ke sath) leak kar sakta tha). Redis probe real `PING` hai, 2s timeout ke sath (redis-py ka default timeout nahi hota) | `config/urls.py`, `governance/system_status.py` |
| `accounts/geo.py` | IP address → country → language (en/ur/ar) mapping. `geoip2fast` library use karta hai (offline database, koi API key/account nahi chahiye) | `accounts/middleware.py::GeoLanguageMiddleware` isko call karta hai |
| `accounts/forms.py` | Login form, Signup form, Profile edit form | `accounts/views.py` use karta hai |
| `accounts/views.py` | Login, Logout, Signup, Dashboard, Profile (naam edit + password change — **ab `user.password_change` AuditLog entry bhi likhta hai**, password khud kabhi log nahi hota), **`set_language_preference`** (Settings ka language toggle) views | `accounts/urls.py` se wire hain, templates render karte hain |
| `accounts/urls.py` | `/accounts/login/`, `/accounts/signup/`, `/accounts/profile/`, `/accounts/set-language/` waghera | `config/urls.py` mein include hai |
| `accounts/permissions.py` | RBAC helpers: `role_required` decorator, `AdminRequiredMixin` — **ye poore project mein har jagah use hota hai** admin-only pages protect karne ke liye | `chat/views.py`, `governance/views.py` sab isko import karte hain |
| `accounts/admin.py` | Django admin panel mein User/Department dikhane ka config | Sirf `/admin/` (raw Django admin) ke liye |
| `accounts/management/commands/create_demo_users.py` | `python manage.py create_demo_users` — `.env` ke `DEMO_*` vars se ek admin/manager/user demo account bana/update karta hai | `.env` ke `DEMO_ADMIN_EMAIL` waghera read karta hai |
| `accounts/management/commands/backup_database.py` | `python manage.py backup_database` — production Postgres ka backup lekar S3-compatible storage par upload karta hai, purane backups delete karta hai | `docs/BACKUP_RESTORE.md` mein poora procedure hai |
| `accounts/management/commands/verify_sentry.py` | `python manage.py verify_sentry` (ya `--raise` ek real exception ke sath) — Sentry ko ek deliberate test event bhejta hai, taake sirf config dekh kar assume na karein ke Sentry kaam kar raha hai. 2026-09-19 ko real Sentry project ke against actually chalaya — reachable confirm hua (ek dafa rate-limit response bhi mila, jo shayad rapid back-to-back testing ki wajah se tha, plan/quota check karna worth hai) | `config/settings.py`'s `SENTRY_DSN` na ho to CommandError deta hai |
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
| `chat/models.py` | **`ModelConfig`** (AI models list + pricing + `display_name`), **`UserModelPermission`** (per-user explicit allow/deny override), **`Conversation`** (pin/soft-delete + `last_provider_model` + `project` fields), **`Message`** (attachment fields + `served_from_cache`), **`MessageFeedback`** (thumbs up/down + comment + denormalized `model_used`), **`PromptTemplate`** (personal ya department-wide "Team" template), `Project` (personal conversation-grouping). Attachment `upload_to` ab **per-user subfolder** hai (`chat_attachments/user_<id>/...`) — pehle sab users ka data ek hi flat folder mein tha, filename ke siwa kuch alag nahi karta tha (2026-09-19 fix) | `accounts.User`/`Department` ko reference karta hai; `Conversation.objects` sirf non-deleted dikhata hai (`Conversation.all_objects` sab kuch) |
| `chat/utils.py` | `group_conversations()` — sidebar list ko "Today / Yesterday / Previous 7 Days / ..." mein group karta hai | `chat/views.py::chat_home` use karta hai |
| `chat/markdown_utils.py` | AI reply ka Markdown → safe HTML render karta hai (bleach se sanitize, taake koi prompt-injected reply raw HTML/script na chala sake) | `chat/templatetags/chat_extras.py` ka filter isko call karta hai |
| `chat/templatetags/chat_extras.py` | Template filters: `render_markdown`, `to_offset` (usage-ring animation ke liye) | `_message_bubble.html`, `_usage_ring.html` use karte hain |
| `chat/document_extraction.py` | Uploaded file (PDF/Word/Excel/text) se text nikalta hai aur `[BEGIN/END ATTACHED DOCUMENT]` delimiters mein wrap karta hai — **prompt-injection defense**: model ko instruction di jati hai ke ye sirf reference data hai, commands nahi. Real payload se tested (`chat/tests.py::AttachmentContextInPromptTests`) | `chat/views.py::post_message` call karta hai |
| `chat/export.py` | Conversation ko PDF/Markdown/plain-text mein export karta hai — Markdown formatting PDF mein properly render hoti hai, raw syntax nahi | `chat/views.py` ke export views use karte hain |
| ~~`chat/model_sync.py`~~ | **Ye file ab exist nahi karti** — model-sync poora `providers` app (section 4 dekhein) mein migrate ho gaya hai (`providers/services.py::sync_provider` + per-provider adapters). Real-time verification: Anthropic (chat streaming AND model-listing dono), OpenAI, Gemini — sab 2026-09-19 ko real API calls se confirmed | — |
| `chat/response_cache.py` | Exact-match response caching — Redis (ya `LocMemCache` agar Redis nahi hai) mein user+model+poori history ka hash key bana kar 1-hour TTL ke sath store karta hai. Alag users kabhi cache share nahi karte | `chat/views.py::stream_message` isko check/store dono ke liye call karta hai |
| `chat/models.py::Message.is_generating` + `generation_started_at` | **Naya (2026-09-19, remaining-audit pass)** — `stream_message` isse atomically "claim" karta hai us pending reply par kaam shuru karne se pehle. Real gap tha: do concurrent GET (duplicate tab, ya htmx ka `sse-connect` reconnect ek stale connection ke sath race) dono purane `content=""` guard se pass ho jate the aur dono provider ko independently call karte the — ek hi reply ka cost double ho jata tha. `STALE_GENERATION_TIMEOUT` (10 min) se stuck claim reclaim ho sakta hai (process crash case). `finally` block claim release karta hai AUR client disconnect (GeneratorExit) par jo partial text ban chuka tha wo save karta hai, taake row hamesha `content=""` par wedged na rahe | `chat/views.py::stream_message`'s `event_stream()`/`_generate_reply()` |
| `chat/live_intelligence.py` + `templates/chat/_live_intelligence_*.html` (naya, 2026-09-19) | **Live Intelligence** — `/chat/` par secondary section: real headlines (public RSS/Atom feeds: Ars Technica, TechCrunch, The Verge AI, Hacker News, GitHub Blog, BleepingComputer, Krebs, The Hacker News + GitHub search API). Feed URLs code mein constants hain, koi user input URL nahi banta, article pages kabhi fetch nahi hote (SSRF surface nahi), `defusedxml`, 1 MB / 3s+5s cap. Cache: 15 min fresh + 24h "last good"; labels **Just retrieved / Cached / Older copy** alag hain (cached ko live nahi kehte); cache error fail-open. `/chat/` khud kabhi network nahi chhoota — headlines htmx se baad mein `chat:live_intelligence` se aate hain (htmx out-of-band block, taake order cards → commands → headlines rahe). Quick commands (`COMMANDS`) existing `create_conversation` + `?starter=` flow use karte hain aur auto-send hote hain; retrieved stories server-side system prompt mein **delimited reference data** ke taur par jaati hain (rules: sirf retrieved items, koi invented story/URL/date nahi). **Agar kuch retrieve na ho to model call hi nahi hota** — fixed reply save hoti hai (warna model headlines invent kar deta). `Message.live_intel` (migration 0024) taake Regenerate/Edit dobara grounded rahe. Refresh: per-user 60s cooldown, jhoota "updated" nahi. Role toggle `live_intelligence` (USER_CHAT_FEATURES) + `LIVE_INTELLIGENCE_ENABLED` setting. Daily scheduled brief **nahi** banaya (on-demand). Real model summarization abhi tak verify nahi hui (local provider keys kaam nahi kar rahi thi) | `chat/views.py` (`live_intelligence_feed/refresh`, `stream_message`), `chat/urls.py`, `static/css/main.css` |
| `chat/tasks.py` | Daily Celery task — naye providers models discover hone par admins ko notify karta hai (`notify()` call), khud kuch enable nahi karta | `notifications/notify.py` use karta hai; Celery Beat se schedule hota hai |
| `chat/providers.py` | **4 providers ka common interface**: OpenAI (+ Grok/DeepSeek, same `OpenAICompatibleProvider` class kyunke wire format identical hai), Anthropic, Gemini — sab `ProviderError` mein wrap hoti hain, retry/timeout sab jagah configured (OpenAI/Grok/Anthropic ke SDK ka apna `max_retries=5`; Gemini raw `requests` se call hota hai isliye `_RETRYING_SESSION` — shared `requests.Session` + `Retry` adapter — 2026-09-19 mein add hui, pehle Gemini ka koi retry nahi tha). Koi bhi provider fail ho to user ko friendly fallback message milta hai, raw error kabhi nahi | `chat/views.py` aur `chat/router.py` use karte hain |
| `chat/router.py` | Smart routing (kaunsa model use hoga) — **ab user ke Plan ke allowed_models se restrict hota hai** (`governance/plans.py::effective_allowed_provider_model_ids` — ProviderModel-based, legacy `effective_allowed_model_ids` nahi), phir UserModelPermission overrides | `chat/views.py` use karta hai |
| `chat/prompts.py` | System prompt banane ka logic (base prompt + department-specific instructions + attached-document delimiter instruction) | `governance.models.SystemPromptVersion` import karta hai |
| `chat/views.py` | **Sabse bari file.** Chat home, message send/receive, streaming (SSE), file upload/download, pin/unpin, soft-delete, sidebar search, `request_upgrade`, message edit (regenerates forward) / regenerate (replaces in place), feedback, export, prompt templates, response caching, usage-warning notification trigger | `governance/limits.py`, `governance/plans.py`, `chat/router.py`, `chat/providers.py`, `chat/response_cache.py`, `chat/export.py`, `notifications/notify.py` — sab yahan milte hain |
| `chat/urls.py` | `/chat/`, `/chat/conversations/...` (edit/regenerate/feedback/export sab isi ke andar), `/chat/templates/`, `/chat/request-upgrade/` | `config/urls.py` mein include hai |
| `chat/admin.py` | Django admin mein ModelConfig/Conversation/Message dikhane ka config | Sirf `/admin/` ke liye |
| `chat/management/commands/seed_models.py` | Purana command jo shuru mein kuch legacy `ModelConfig` rows seed karta tha — ab naya real provider connect karna ho to `providers:list` page se (real API key paste karo), legacy `ModelConfig` list `governance:models` par hai (plain CRUD, koi sync nahi) | `chat/models.py` ka `ModelConfig` use karta hai |
| `templates/chat/chat_home.html` | **Poora chat interface** — sidebar (search box + pinned/grouped conversations + usage widget + request-upgrade button) + panel (messages + composer + model dropdown). Mobile par sidebar ek slide-in drawer ban jata hai (hamburger icon) | `chat:chat_home` URL |
| `templates/chat/_conversation_list.html` | Sidebar ki conversation list ka fragment (Pinned section + date-grouped sections) — pin/delete ke baad isi ko htmx se refresh kiya jata hai | `chat_home.html` include karta hai, `toggle_pin`/`delete_conversation` views isko re-render karte hain |
| `chat/views.py` — sidebar pagination + search (naya, 2026-09-19) | `_conversation_list_context` ab sirf pehle `CONVERSATIONS_PAGE_SIZE` (100) unpinned conversations render karta hai (pinned unbounded rehte hain — hamesha kam hote hain). **`load_more_conversations`** (`chat:load_more_conversations`) cursor-based "Load more" hai: cursor = `updated_at|id`, `-updated_at, -id` ordering (id tiebreaker — `updated_at` har interaction par badalta hai isliye offset pagination unstable hoti). **`search_conversations`** (`chat:search_conversations`) text search ab SERVER-side hai (pehle pure client-side DOM filter tha jo sirf already-rendered items dekh sakta tha, isliye cap ke baad search toot jati). Provider/project filter ab bhi client-side (`chat_home.html::applyFilter`) — jo bhi rendered ho us par lagta hai. Templates: `_conversation_list_more.html` (append fragment, button `hx-on::after-swap` se filter dobara lagata hai), `_conversation_search_results.html`. Sentry: provider failure ab `ai.provider`/`ai.model` tags ke sath capture hoti hai (`new_scope()`, sirf slugs — prompt/reply kabhi nahi) | `chat/urls.py`, `static/css/main.css` (`.sidebar-load-more-btn`) |
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

## 4. `providers` app — AI Provider Connections (org-wide credentials + model registry)

**Responsibility:** `chat.models.ModelConfig` + env-var API keys ki jagah ye app hai — real, admin-connected API keys (encrypted DB mein) + per-provider live model-list sync. **Note:** `chat/model_sync.py` naam ki file ab exist nahi karti, aur `governance:sync_models_preview`/`sync_models_import` URLs bhi nahi hain — purana architecture tha, poora is app mein migrate ho gaya (`providers/management/commands/migrate_models_to_provider_model.py`). 4 providers connected/available: **Anthropic (Claude)**, **OpenAI-compatible** (OpenAI khud + Grok + DeepSeek — same adapter class, kyunke wire format identical hai), **Gemini** — teenon 2026-09-19 ko real API calls se verified (Anthropic ka chat streaming AND model-listing dono, production ke real connected key se).

| File | Kaam | Kis se link hai |
|---|---|---|
| `providers/models.py` | **`Provider`** (name/slug/`adapter_type`/`region`/`base_url`/encrypted `api_key_encrypted`+`api_key_last4`/`sync_status`), **`ProviderModel`** (model_id/pricing/`is_enabled`/`is_manager_assignable`/`supports_vision`) | `governance.Plan.allowed_provider_models` M2M isko reference karta hai |
| `providers/adapters/base.py` | `BaseProviderAdapter` — har adapter ka common interface (`test_connection`, `fetch_models`), `ProviderAPIError` | Har adapter subclass isko extend karta hai |
| `providers/adapters/anthropic.py`, `openai_compatible.py`, `gemini.py` | Per-provider model-LISTING adapters (connect-time key test + "Sync Models" ke liye) — **ye `chat/providers.py` ke chat-STREAMING adapters se ALAG file hain**, dono ka kaam alag hai (listing vs actual chat call) | `providers/services.py::sync_provider` isko call karta hai |
| `providers/adapters/sanitize.py` | Error messages se accidentally leak hui API key ko hata deta hai (agar koi exception message mein raw key aa jaye) | `providers/services.py`, adapters isko call karte hain |
| `providers/services.py` | `sync_provider()` — adapter ka `fetch_models()` result ko `ProviderModel` rows mein reconcile karta hai. Naya model kabhi auto-enable nahi hota | `providers/views.py::connect_provider`/`resync_provider`, `providers/tasks.py` dono isko call karte hain |
| `providers/errors.py` (naya) | Provider ke raw error text ko **kabhi** UI par nahi dikhana — 7 categories (Authentication / Rate limited / Timeout / Provider unavailable / Invalid response / Configuration / Unknown) + fixed safe message. Sync ab sirf safe label store karta hai (`last_sync_error`), raw text sirf server log mein. Purani rows jin mein raw text pada hai unhein `describe()` display par classify karta hai — data migration nahi. Providers card, connect/resync flash message, Django admin, System status, **aur user-facing Document generation + Domain Generator** (ye do raw text seedha end-users ko dikhate the) sab isi se guzarte hain | `providers/services.py`, `providers/views.py`, `providers/admin.py`, `chat/views.py`, `domaingen/views.py` |
| `providers/tasks.py` | `sync_all_connected_providers` — daily Celery Beat task, har connected provider ko resync karta hai | Beat schedule (data migration se seed) |
| `providers/views.py` | `ProviderListView`, `connect_provider` (key paste + `test_connection` + `sync_provider`), `approve_provider`/`reject_provider` (SuperAdmin approval step), `resync_provider`, `disconnect_provider`, `update_provider_region`, `toggle_provider_model`/`_manager_assignable`/`_vision` | `providers/services.py`, `governance.audit.log_action` |
| `providers/urls.py` | `/providers/`, `/providers/<id>/connect\|approve\|reject\|resync\|disconnect\|region/`, `/providers/models/<id>/toggle...` | `config/urls.py` mein include hai |
| `providers/admin.py` | Django admin config | Sirf `/admin/` ke liye |
| `templates/providers/list.html` + `_provider_card.html` | Har provider ka card — connect form (key paste), connected-status, resync/disconnect buttons, models list toggle | `providers:list` URL |

---

## 5. `governance` app — Admin Dashboard + Plan/Tier System (poora control yahan hai)

**Responsibility:** Admin ke liye sab kuch — users manage karna, **Plans (tier-based access control)**, models/pricing, per-user permissions, usage/upload limits, upgrade requests, audit logs, department + system prompts, charts, search/filter.

| File | Kaam | Kis se link hai |
|---|---|---|
| `governance/models.py` | **`Plan`** (tier: Demo/Standard/Premium — models/limits/feature-flags bundle, **`max_messages_per_minute`** naya burst-rate-limit field, 2026-09-19), **`UserPlanAssignment`** (kaun kis plan par hai + expiry + **`cancelled_at`** naya field — billing app ke cancel/resume ke liye), **`UpgradeRequest`** (self-service upgrade requests), `SystemPromptVersion`, **`UsageLimit`** (per-user/department override — Plan se upar priority), **`AuditLog`** (**ab immutable hai** — `save()`/`delete()` override karke ek dafa likhne ke baad kabhi edit/delete nahi ho sakta, sirf `SET_NULL` FK cascade jab actor user delete ho, 2026-09-19) | `accounts.Department`, `accounts.User`, `chat.ModelConfig` (Plan ka `allowed_models` M2M) |
| `governance/plans.py` | **Plan resolution ka poora dimagh.** `get_plan_status()` (active/grace/expired), `assign_plan()`, `effective_allowed_model_ids()`, `plan_limit_fallback()`, `has_feature()`, `engagement_score()`, `check_session_creation_limit()`, `get_user_overrides()`/`count_user_overrides()`/`clear_user_overrides()`, **`check_message_burst_limit()`** (naya, 2026-09-19 — per-minute rate limit, cache-backed via `accounts/rate_limit.py`, `max_requests_per_period` se alag kyunke uska shortest window ek poora din hai). Precedence: personal `UsageLimit` > department `UsageLimit` > user ka Plan > kuch nahi | `chat/router.py`, `governance/limits.py`, `chat/views.py` sab isko call karte hain |
| `governance/limits.py` | Usage limit check (`check_usage_limits` — Plan expiry/grace + **burst rate limit** ab yahan check hota hai) aur file-upload validation (`validate_upload` — size/extension + **`governance/uploads.py::verify_file_content` magic-byte check**, 2026-09-19) | `chat/views.py` isko directly call karta hai har message/upload par |
| `governance/uploads.py` | **Naya file (2026-09-19).** Magic-byte content verification (`filetype` library, pure Python) — executable/script signatures hard-block karta hai (admin ka `allowed_file_extensions` override bhi bypass nahi kar sakta), aur pdf/png/jpg/docx/xlsx ke liye real content claimed-extension se match karta hai. txt/csv/md/json ke liye kuch signature nahi hota (genuinely plain text) | `governance/limits.py::validate_upload` isko call karta hai |
| `governance/error_alerts.py` | **Naya file (2026-09-19).** `AsyncAdminEmailHandler` — koi bhi unhandled 500 error hone par `settings.ADMINS` ko Celery task ke zariye email jata hai (Django ka apna `mail_admins` synchronous hota, request thread block kar deta) `HealthProbeDowngradeFilter` (Phase 4C) — `/healthz/` aur `/healthz/deep/` ka **jaan-boojh kar diya gaya 503** (dependency down) ERROR ki jagah WARNING log hota hai, warna Docker/CI/monitor ke har poll par admins ko email jata (test mein 8 polls = 16 emails). Sirf wahi case: 503 + probe path + exception nahi. Probe ke andar asli crash (exc_info) ya kisi aur URL ka 503 ERROR hi rehta hai. **Known, alag masla:** Django ka apna sync `AdminEmailHandler` (`django` logger par) bhi chalta hai, isliye har asli 500 par admin ko 2 email jate hain — abhi badla nahi gaya | `config/settings.py::LOGGING`'s `django.request` logger (filter `health_probe`) isko use karta hai |
| `governance/audit.py` | `log_action()` helper — har admin action (role change, plan change, model enable, lockout, login, password change, refund, cancellation, waghera) yahan se AuditLog mein likha jata hai | `governance/views.py`, `accounts/signals.py`, `billing/views.py` isko call karte hain |
| `governance/templatetags/governance_extras.py` | `dict_get` filter — templates mein ek dict ko variable-key se lookup karne ke liye (e.g. Users list mein har row ka plan-status) | `_users_table.html` use karta hai |
| `governance/views.py` | **Sabse bari file is app ki.** Dashboard (charts + org usage ring), Users list (search/filter + Plan column + bulk plan-assign + **overrides badge/view/clear**), **Plans CRUD**, **Upgrade Requests**, legacy Models (`ModelConfig` add/pricing/enable + search/filter — **naye AI provider connect karna ho to `providers:list` par jao, ye sirf legacy list hai**), Model Permissions, Limits (CRUD + search), Departments (CRUD + search + **department templates**), Audit Logs (search/filter/pagination), System Prompt, **Feedback review**, **Usage export (CSV/Excel/monthly summary)** | `chat.models`, `accounts.models`, `governance.plans` — sab import karta hai |
| `governance/urls.py` | `/governance/...` sab routes — including `/governance/plans/`, `/governance/upgrade-requests/`, `/governance/users/<id>/change-plan/`, `/governance/users/bulk-change-plan/`, `/governance/users/<id>/overrides/` (view/clear), `/governance/models/sync/`, `/governance/usage/export.csv\|.xlsx`, `/governance/feedback/` | `config/urls.py` mein include hai |
| `governance/admin.py` | Django admin mein ye models dikhane ka config | Sirf `/admin/` ke liye (fallback/advanced use) |
| `templates/governance/dashboard.html` | Overview + Charts (Chart.js, 14-day zero-filled data, empty-states) + **org-wide usage ring** | `governance:dashboard` URL |
| `governance/system_status.py` + `templates/governance/_system_status.html` (naya, 2026-09-19) | **SuperAdmin-only** "System status" section existing dashboard mein (`dashboard.html` isko `{% include %}` karta hai). `build_system_status()` structured dict deta hai: 4 summary cards (Application / Database / Redis / Background jobs), providers, jobs. **Redis ke teen alag states** — `not_configured` (REDIS_URL blank: local dev ka intentional setup, LocMemCache + Celery eager), `unavailable` (URL set hai lekin real PING fail — 2s timeout, warna blackholed host dashboard hang kar deta), `healthy` (real PING kaamyab). URL/password/exception text kabhi page par nahi aata. **Providers:** stored `last_sync_*` fields se, "last known status" label ke sath; raw `last_sync_error` kabhi render nahi hota — `classify_provider_error()` usay fixed categories (Authentication / Rate limited / Network / Provider service error) mein badalta hai. **Jobs:** `PeriodicTask.last_run_at` sirf "scheduler ne dispatch kiya" batata hai, kamyabi nahi — isliye states sirf Disabled / Not run yet / Active hain, "Healthy"/"Failed" jaan-boojh kar nahi (data support nahi karta). Celery retry/duration/last-error aur per-request provider latency/failure counters abhi record hi nahi hote (alag project). CSS `sys-*` classes hain, `.chart-row`/`.chart-card` nahi (wo 3 charts ke liye sized hain, aur global `main .card + .card` margin grid mein panel ko neeche dhakel deta tha). Phone par jobs table stacked rows ban jati hai | `governance/views.py::DashboardView`, `static/css/main.css` |
| `templates/governance/users.html` + `_users_table.html` | Users list — search/role/status/plan filters, role/department dropdown, **Plan column (days-left/grace/expired badge + engagement 🔥 flag)**, inline Change-Plan, bulk checkbox "assign plan to selected" | `governance:users` URL |
| `templates/governance/plans.html` + `plan_form.html` | **Plan management** — list + create/edit form (limits, allowed-models checkboxes, feature-flag checkboxes, default/visibility toggles) | `governance:plans`, `plan_new`, `plan_edit` URLs |
| `templates/governance/_plan_downgrade_confirm.html` | Jab admin kisi user ko aisay plan par downgrade kare jiski limit already cross ho chuki ho, ye confirmation step dikhata hai | `governance:change_user_plan` view isko render karta hai |
| `templates/governance/upgrade_requests.html` | Pending self-service upgrade requests — Approve (Users page pe le jata hai, pre-filtered) / Dismiss | `governance:upgrade_requests` URL |
| `templates/governance/models.html` + `_models_table.html` | Legacy `ModelConfig` list — add/pricing/enable/disable, search/status filter. **Koi sync button nahi hai — real provider connect/sync `providers:list` page par hai** (section 4) | `governance:models` URL |
| `templates/governance/model_permissions.html` | Ek specific model ke liye "kaun use kar sakta hai" (per-user override, Plan ke upar) | `governance:model_permissions` URL |
| `templates/governance/limits.html` + `_limits_table.html` + `limit_form.html` | Usage/Upload limits (per-user/department override) ki list (search) + add/edit form | `governance:limits`, `limit_new`, `limit_edit` URLs |
| `templates/governance/user_overrides.html` | Ek user ke personal `UsageLimit`/`UserModelPermission` overrides dikhata hai + "Clear all overrides" button (Plan defaults par wapas) | `governance:user_overrides` URL — Users list ke "N custom overrides — view/clear" link se |
| `templates/governance/departments.html` + `_departments_table.html` + `department_templates.html` | Departments CRUD + search, plus **department-wide "Team" prompt templates** management | `governance:departments`, `department_templates` URLs |
| `templates/governance/system_prompt.html` | Ek department ka system prompt edit karna | `governance:system_prompt` URL |
| `templates/governance/usage.html` + `_usage_table.html` | Per-user usage/cost table — search/model/date-range filter, **Export CSV/Excel + Monthly summary export** buttons, cache-hit-rate/estimated-cost-saved metric | `governance:usage`, `export_usage_csv/xlsx` URLs |
| `templates/governance/audit_logs.html` + `_audit_logs_table.html` | Audit log history — search/action-type/date-range filter, pagination (filters preserve karta hai) | `governance:audit_logs` URL |
| `templates/governance/feedback.html` + `_feedback_table.html` | Response feedback review — recent thumbs-down + context, model se filter | `governance:feedback` URL |

---

## 6. `billing` app — Invoices, Regional Pricing, Refund & Cancellation

**Responsibility:** Invoice generation (manual + recurring monthly sweep), payment verification (manual proof-of-payment — koi real payment gateway nahi hai), regional pricing, self-service plan checkout, **refund requests** aur **plan cancellation** (2026-09-19 mein add hua).

| File | Kaam | Kis se link hai |
|---|---|---|
| `billing/models.py` | **`Invoice`** (status: unpaid/pending_verification/paid/**refunded** — amounts creation-time par snapshot hote hain, baad mein Plan/RegionalPrice badalne se purane invoice ka number nahi badalta), **`RefundRequest`** (7-day window ke andar auto-approved, bahar Admin ki pending review — `governance.UpgradeRequest` jaisa pending/approved/rejected shape), `DepartmentBillingProfile`, `OrganizationBillingProfile`, `UserBillingProfile`, `RegionalPrice` | `governance.Plan`, `accounts.Department`/`User` reference karta hai |
| `billing/invoicing.py` | `generate_invoice_for_department()` / `generate_invoice_for_user()` — poora money-math (seats, tax, line items) **ek hi jagah** hai, taake manual "Generate invoice" button aur recurring sweep dono same logic use karein | `billing/views.py`, `billing/tasks.py` dono isko call karte hain |
| `billing/tasks.py` | **`sweep_due_invoices`** — daily Celery Beat task, har recipient ke last invoice se 30-din rolling cycle par next invoice banata hai (**cancelled plan (`UserPlanAssignment.cancelled_at` set) ko skip kar deta hai** — cancel karne ka yahi asal mechanism hai, plan khud nahi badalta, sirf future invoice generate hona band ho jata hai). `send_overdue_reminders` bhi yahan hai | `django_celery_beat` se schedule (data migration se seed hua) |
| `billing/access.py` | `has_overdue_unpaid_invoice()` — koi bhi overdue unpaid invoice ho to chat access block karta hai | `governance/limits.py::check_usage_limits` isko call karta hai |
| `billing/emails.py` | Invoice email, overdue reminder email bhejne ka code (`send_tracked_email` reuse karta hai, alag se koi email-sending code nahi) | `billing/views.py::checkout_plan` waghera call karte hain |
| `billing/pdf.py` | Invoice ka PDF render (`xhtml2pdf`/`reportlab`) | `billing/views.py::download_invoice_pdf` use karta hai |
| `billing/regions.py`, `billing/tax_rules.py` | Region list (currency/flag) aur country → tax-rate mapping | `billing/views.py` regional pricing/checkout mein use karta hai |
| `billing/views.py` | Invoice CRUD (generate/verify/reject/toggle-status/delete/email/**paginated list — 50/page**), regional pricing, self-service checkout (`checkout_plan` — sirf unpaid Invoice banata hai, plan tabhi badalta hai jab invoice PAID mark ho), My Invoices, My Plans, **`request_refund`** (7-day window check → auto-refund ya pending RefundRequest), **`resolve_refund_request`** (Admin approve/reject), **`cancel_plan`/`resume_plan`** (self-service, `next=dashboard` param se wapas dashboard par ya my-plans par redirect) | `governance.plans`, `governance.audit.log_action`, `notifications.notify` sab yahan milte hain |
| `billing/views.py` — idempotency/locking (naya, 2026-09-19) | `checkout_plan` ab same (user, plan) ke liye open (unpaid/pending_verification) invoice ho to naya nahi banata, existing par redirect karta hai — paid/refunded invoice naye checkout ko block nahi karte. `submit_payment_proof`, `request_refund`, `resolve_refund_request`, `cancel_plan`, `resume_plan` ka check+mutate+notify ek `transaction.atomic()` + `select_for_update()` ke andar hai (double-click par dobara notification/refund nahi). **Note:** `select_for_update` SQLite par no-op hai, sirf Postgres (production) par real lock — isliye tests sequential retry prove karte hain, true concurrency nahi. Naya audit action: `billing.invoice_checkout`. Deliberately DB constraint nahi lagaya: admin ka manual "Generate invoice" aur recurring sweep ko legitimately extra invoice banana pad sakta hai | `governance/views.py::change_user_plan/bulk_change_plan` mein same-plan resubmit ab no-op hai (`previous_plan` corrupt nahi hota) |
| `billing/urls.py` | `/billing/invoices/...`, `/billing/my-invoices/...`, `/billing/my-plans/cancel/`, `/billing/my-plans/resume/`, `/billing/refund-requests/`, `/billing/refund-requests/<id>/resolve/` | `config/urls.py` mein include hai |
| `billing/admin.py` | Django admin config (Invoice/Plan/RegionalPrice) | Sirf `/admin/` ke liye |
| `templates/billing/invoices.html` + `_invoices_table.html` | Admin invoice list — department filter, verify/reject/toggle actions (htmx), **pagination bar (50/page)** | `billing:invoices` URL |
| `templates/billing/invoice_detail.html` | Ek invoice ka poora detail — proof-of-payment, **refund request button (paid invoice par)**, refunded-status message | `billing:invoice_detail` URL |
| `templates/billing/my_invoices.html` | User ki apni invoices — payment submit karna, **"Request a refund" (7-day-window text ke sath)** | `billing:my_invoices` URL |
| `templates/billing/my_plans.html` | Plan-cards grid + **current plan status card (Cancel plan / Resume plan button)** | `billing:my_plans` URL |
| `templates/billing/refund_requests.html` + `_refund_requests_table.html` | Admin-facing pending refund requests — Approve/Reject | `billing:refund_requests` URL — sidebar mein "Billing" group ke andar, "Invoices" ke sath |
| `templates/accounts/dashboard.html` | (accounts app mein hai, lekin yahan note karna zaroori hai) Post-login "Your plan" card — **Cancel plan/Resume plan button yahan bhi hai**, `next=dashboard` se wapas isi page par aata hai | `accounts:dashboard` URL |

**Refund/cancellation ka poora flow:** koi payment gateway nahi hai is app mein — refund matlab hamesha "Admin (ya 7-day auto-approval) invoice ko REFUNDED mark karta hai app ke andar, paisa wapas bhejna admin ka apna kaam hai (bank transfer waghera) app ke bahar." `RefundRequest.auto_approved=True` hota hai jab 7-day window ke andar khud approve ho jaye — Admin ko sirf window ke BAHAR wale requests dikhte hain review ke liye.

---

## 7. `notifications` app — In-app Bell + Email Notifications

**Responsibility:** In-app notification bell, email sending (via Celery), per-user per-type email opt-out. Zero tests before Phase 6 — now has 62 (refund/cancellation triggers aur baaki hardening passes ne badhaya, 2026-09-19 tak).

| File | Kaam | Kis se link hai |
|---|---|---|
| `notifications/models.py` | **`Notification`** (title/body/is_read/email_sent), **`NotificationType`** choices (usage_warning/plan_change/trial_expiring/trial_expired/admin_change/model_sync_available/invoice_payment_submitted/**refund_requested**/**refund_decision**/**plan_cancellation** — teen naye 2026-09-19 refund/cancellation feature ke liye), **`NotificationPreference`** (per-type email on/off — missing row = "email everything", safe default) | `accounts.User` ko reference karta hai |
| `notifications/notify.py` | `notify()` — **har trigger isi se guzarta hai.** In-app row hamesha banata hai; email sirf preference allow kare to Celery task queue karta hai. `recently_notified()` dedup helper (same type ko 24h mein dobara na bheje) | `chat/views.py`, `governance/views.py`, `notifications/tasks.py` sab isko call karte hain |
| `notifications/tasks.py` | `send_notification_email` (Celery task — branded HTML email render+send), `sweep_expiring_demo_plans` (daily Beat task — trial-expiring/trial-expired notify) | `notify()` `.delay()` karta hai; Beat schedule `django-celery-beat` se DB mein hai |
| `notifications/views.py` | Bell dropdown fragment, mark-read/mark-all-read, Settings ka preferences form | `notifications/urls.py` se wire hain |
| `notifications/urls.py` | `/notifications/bell/`, `/mark-all-read/`, `/preferences/` | `config/urls.py` mein include hai |
| `notifications/tests.py` | **62 tests** (12 test classes) — `notify()` khud, usage-warning trigger, admin-change/plan-change triggers (real views ke zariye), refund/cancellation triggers, trial-expiring/expired sweep (dedup-on-rerun sameet), bell dropdown, preference opt-out. Ye file Phase 6 se pehle exist hi nahi karti thi | `python manage.py test notifications` |
| `templates/notifications/_bell_dropdown.html` | Bell icon + unread badge + dropdown list | `templates/base.html` include karta hai (har page par visible) |
| `templates/notifications/email_generic.html` | Har notification email ka branded HTML template (portal ke colors/logo consistent) | `notifications/tasks.py::send_notification_email` render karta hai |

---

## 8. Shared/Layout files

| File | Kaam |
|---|---|
| `templates/base.html` | **Sab pages ka parent template.** Sidebar navigation (Admin sub-nav mein Plans/Upgrade Requests bhi hain), mobile hamburger topbar + slide-in drawer overlay, notification bell, `{% get_current_language %}`/`{% get_current_language_bidi %}` se `lang`/`dir` attributes (Urdu/Arabic RTL), `{% block content %}` jahan har page apna content dalta hai |
| `static/css/main.css` | **Poori app ki styling ek hi file mein.** Design tokens (colors/spacing/type-scale/mono-font) `:root` mein top par hain. Filter-toolbar, usage-ring, sidebar redesign, mobile `@media (max-width: 720px)` overlay pattern, aur `[lang="ur"]`/`[lang="ar"]` font-family rules (Noto Nastaliq Urdu / Noto Naskh Arabic) isi file mein hain |

---

## 8b. Phase 5 — Hardening, authorization aur operations (2026-09-20)

| File | Kaam | Kahan se use hota hai |
|---|---|---|
| `config/redaction.py` | **Credentials ko logs/exceptions/Sentry se bahar rakhta hai.** `redact_secrets()` (`?key=`, Bearer, `sk-…` jaisi cheezein mask), `SecretRedactionFilter` (har console/file log record + poori exception chain), `scrub_event()` (Sentry). Wajah: Gemini key URL mein jati thi aur `requests` ke exception text se log/Sentry mein likhi gayi (app.log mein poori key thi). Ab key `x-goog-api-key` **header** mein jati hai | `chat/providers.py::ProviderError`, `config/settings.py::LOGGING` + Sentry `before_send` |
| `config/files.py` | `delete_file_after_commit()` — row delete hone par uski file disk se hatata hai (commit ke **baad**, rollback par nahi). Pehle retention sweep rows hata deta tha magar attachments hamesha disk par rehte the | `chat/models.py` (Message.attachment), `billing/models.py` (Invoice proof) `post_delete` signals |
| `accounts/redirects.py` | `safe_next_url()` — POSTed `next` sirf same-host par follow hota hai (pehle 8 views open redirect the) | notifications, governance, billing views |
| `governance/management/commands/ops_verify.py` | **Read-only production self-check** (migrations, schema, Redis PING + cache round trip, Celery ping, beat, feeds (har source par 1 GET), disk, DB connections, retention counts, backup config, release SHA). Kuch likhta/delete nahi karta, credentials print nahi karta. CI deploy ke baad chalata hai (`--annotate`), nateeja run ke checks par annotations ki shakal mein dikhta hai | `.github/workflows/ci.yml` "Post-deploy verification" |
| `accounts/authz_matrix.py` + `governance/test_authorization_matrix.py` + `governance/authz_expected.json` | **Authorization matrix**: har route (346) ko 7 roles (anonymous/user/member/manager/dept-admin/admin/superadmin) se asli backend par hit karta hai. Invariants + golden file: kisi route ka minimum role badle to test fail (jaan-boojh kar badla ho to `AUTHZ_UPDATE=1` se dobara likho aur diff review karo) | CI test job |
| `billing/views.py::invoice_proof` | Payment screenshot sirf usi ko jo invoice dekh sakta hai (recipient / usi department ka Admin / SuperAdmin). Pehle `/media/invoice_proofs/…` seedha public tha | `billing/templates/billing/invoice_detail.html`, `_invoices_table.html` |
| `config/urls.py::serve_media` / `serve_docs` | `/media/` ab **sirf `branding/`** (login page ka logo/favicon) public hai, path normalise karke check hota hai (`branding/../chat_attachments/x` pehle guzar jata tha). `/docs/` sirf `guides/*.html` + `FEATURE_GUIDE.html`; `SECRETS.md`/`PRODUCTION_ACCESS.md` etc. kabhi nahi | `billing/test_media_privacy.py` |
| `chat/markdown_utils.py::normalize_headings` | AI reply ke headings page ke h1 ke neeche h2 se shuru (levels rank ho kar, gap nahi; `md-hN` class se dikhawat wahi). Sirf on-page display; documents/exports author ke levels rakhte hain | `chat/templatetags/chat_extras.py` filter |
| `chat/providers.py::StreamChunk.truncated` + `chat/views.py::TRUNCATED_REPLY_NOTICE` | Provider ne jawab beech mein kaat diya (OpenAI `length`, Anthropic `max_tokens`, Gemini finishReason ke bagair stream khatam) to user ko note dikhta hai, aur woh reply cache nahi hoti | `chat/views.py::stream_message` |
| `billing/views.py::verify/reject/toggle_invoice` | Status ab **maujooda state** dekh kar, row lock ke andar badalta hai: sirf `pending_verification` verify/reject ho sakti hai; refunded invoice wapas paid nahi hoti | `billing/test_invoice_state_guards.py` |
| `governance/error_alerts.py` | Ab **ek hi** admin-alert path (Django ka apna sync `AdminEmailHandler` `django` logger se hata diya); broker down ho to direct send fallback; subject par `[Django]` prefix. Health-probe 503 downgrade waisa hi | `config/settings.py::LOGGING` |
| `deployment/ci_annotate_failures.py` | Test job fail ho to sirf tab chalta hai: fail hone wale tests ko annotations banata hai (raw logs public nahi). Kabhi pass/fail decide nahi karta | `.github/workflows/ci.yml` |
| `config/test_requirements_complete.py`, `config/test_ci_workflow.py` | Har import kiya hua third-party module `requirements.txt` mein pin ho (defusedxml incident); CI gates sirf exit code par (koi `grep FAILED`), deploy `needs: lint+test`, verification steps rollback trigger nahi kar sakte | CI |
| `docker-compose.yml` | `RELEASE_SHA` web/worker/beat mein; container logs ka size limit (3 × 10 MB) | deploy step |
| `chat/test_browser_sse.py` | Chromium (Playwright) se SSE regression: normal reply par console `Event` error nahi, toota hua stream phir bhi report hota hai. Browser na ho to skip (browser **pehle** check hota hai, live server baad mein — warna Postgres par `DROP DATABASE` fail hota tha) | local |

---

## 9. Naya kaam karte waqt kahan jayein (cheat-sheet)

| Karna kya hai | Kis file mein jayein |
|---|---|
| Naya field User/Department mein add karna | `accounts/models.py` → phir `makemigrations` |
| Naya AI provider add karna (Claude/GPT/Gemini/Grok already hain) | `chat/providers.py` mein naya `AIProvider` subclass, `providers/adapters/` mein model-listing adapter |
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
| Rate limit (login/signup/OTP/chat messages) badalna | `accounts/rate_limit.py::is_rate_limited` (login/signup/OTP, module-level constants `accounts/views.py` mein), `governance/plans.py::check_message_burst_limit` (chat messages, `Plan.max_messages_per_minute` se) |
| File upload validation (naya file-type allow karna) | `governance/uploads.py::_EXPECTED_KINDS` (magic-byte signature) + `settings.DEFAULT_ALLOWED_FILE_EXTENSIONS`/`UsageLimit.allowed_file_extensions` (extension allowlist — dono check hote hain) |
| Refund/cancellation ka rule badalna | `billing/models.py::REFUND_WINDOW_DAYS` (7-day window), `billing/views.py::request_refund`/`resolve_refund_request`/`cancel_plan`/`resume_plan` |
| Naya AuditLog action type add karna | Bas jahan action ho wahan `governance/audit.py::log_action(actor, "app.action_name", target, ...)` call karo — AuditLog immutable hai, koi migration nahi chahiye naye action_type ke liye (plain string hai) |

---

## 10. Data flow example — "User message bhejta hai"

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
        │       └─→ governance/plans.py::effective_allowed_provider_model_ids()   (Plan ke allowed_models se restrict)
        ├─→ chat/prompts.py::build_system_prompt()  (governance.SystemPromptVersion se department prompt)
        ├─→ chat/providers.py::get_provider()        (OpenAI/Anthropic ko actual call, retry-safe)
        └─→ Message.save()  (final reply + tokens + cost DB mein)
```

## 11. Data flow example — "Admin kisi user ka Plan change karta hai"

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

## 12. Data flow example — "5 galat password attempts"

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

## 13. Data flow example — "User apni usage limit ke 85% par pohonch jata hai"

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

*Last updated: 2026-09-20 (Phase 5 hardening - section 8b), previously 2026-08-30 (Section B feature pack — export/templates/shortcuts/file-upload/multi-language/usage-export/notifications/mobile-review/onboarding, Section C reliability — feedback/brute-force/backup/caching, IP-based language detection, aur per-user Plan-override view/clear UI ke baad; sab kuch real live-tested evidence ke saath, ab CI par bhi pass ho raha hai). Jab bhi naye app/model/major feature add ho, is document ko bhi update kar dena.*
