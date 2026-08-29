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
| `config/settings.py` | **Sabse important file.** Sari app-wide settings: database, installed apps, API keys, security, email/media/static paths |
| `config/urls.py` | Root URL routing — yahan se har app ke `urls.py` ko include kiya gaya hai |
| `config/wsgi.py` / `config/asgi.py` | Server entrypoints (production/deployment ke liye, aksar chhedne ki zaroorat nahi) |
| `.env` | Real secrets (API keys, SECRET_KEY) — **kabhi commit nahi hoti**, sirf is machine par hai |
| `.env.example` | `.env` ka template, bina real values ke — naya setup karte waqt copy karke `.env` banayein |
| `requirements.txt` | Python packages ki list — naya package install karne ke baad `pip freeze > requirements.txt` chalayein |
| `AI_Client_Portal_Spec.md` | Original spec document jis par poora project based hai |
| `PROJECT_MAP.md` | Yehi file jo aap abhi padh rahe hain |
| `railway.json`, `mise.toml`, `Procfile`* | Railway deployment configs |
| `deployment/` | VPS (non-Railway) deployment ke configs: Gunicorn, Nginx, systemd |
| `.github/workflows/ci.yml` | GitHub Actions — har push par tests automatically chalte hain |

---

## 2. `accounts` app — Login, Signup, Users, Departments, Profile

**Responsibility:** Authentication, User model, Department model, RBAC (role-based permissions), profile settings.

| File | Kaam | Kis se link hai |
|---|---|---|
| `accounts/models.py` | **`User`** (email, role, department) aur **`Department`** (name, budget cap) models yahan define hain | Almost har app isko import karta hai (`chat`, `governance`) |
| `accounts/forms.py` | Login form, Signup form, Profile edit form | `accounts/views.py` use karta hai |
| `accounts/views.py` | Login, Logout, Signup, Dashboard, Profile (naam edit + password change) views | `accounts/urls.py` se wire hain, templates render karte hain |
| `accounts/urls.py` | `/accounts/login/`, `/accounts/signup/`, `/accounts/profile/` waghera | `config/urls.py` mein include hai |
| `accounts/permissions.py` | RBAC helpers: `role_required` decorator, `AdminRequiredMixin` — **ye poore project mein har jagah use hota hai** admin-only pages protect karne ke liye | `chat/views.py`, `governance/views.py` sab isko import karte hain |
| `accounts/admin.py` | Django admin panel mein User/Department dikhane ka config | Sirf `/admin/` (raw Django admin) ke liye |
| `templates/accounts/login.html` | Login page (split-screen design) | `accounts:login` URL |
| `templates/accounts/signup.html` | Signup page | `accounts:signup` URL |
| `templates/accounts/dashboard.html` | Login ke baad ka landing page | `accounts:dashboard` URL |
| `templates/accounts/profile.html` | Profile settings (naam + password change) | `accounts:profile` URL |

---

## 3. `chat` app — AI Chat Interface

**Responsibility:** Conversations, Messages, AI provider calls (OpenAI/Anthropic), streaming replies, file uploads.

| File | Kaam | Kis se link hai |
|---|---|---|
| `chat/models.py` | **`ModelConfig`** (AI models list + pricing), **`UserModelPermission`** (kaun kaunsa model use kar sakta hai), **`Conversation`**, **`Message`** (attachment fields bhi yahan hain) | `accounts.User` aur `accounts.Department` ko reference karta hai |
| `chat/providers.py` | OpenAI aur Anthropic API calls ka common interface — **agar naya AI provider add karna ho to yahan** | `chat/views.py` aur `chat/router.py` use karte hain |
| `chat/router.py` | Smart routing (kaunsa model use hoga), model permissions check, dropdown ke liye model list | `chat/views.py` use karta hai, `governance/limits.py` se conceptually related |
| `chat/prompts.py` | System prompt banane ka logic (base prompt + department-specific instructions) | `governance.models.SystemPromptVersion` ko import karta hai (department ka active prompt uthane ke liye) |
| `chat/views.py` | **Sabse bara file.** Chat home page, message send/receive, streaming (SSE), file upload/download | `governance/limits.py` (usage limits check), `chat/router.py`, `chat/providers.py` — sab yahan milte hain |
| `chat/urls.py` | `/chat/`, `/chat/conversations/...` | `config/urls.py` mein include hai |
| `chat/admin.py` | Django admin mein ModelConfig/Conversation/Message dikhane ka config | Sirf `/admin/` ke liye |
| `chat/management/commands/seed_models.py` | Command jo shuru mein kuch AI models seed karta hai (`python manage.py seed_models`) | `chat/models.py` ka `ModelConfig` use karta hai |
| `templates/chat/chat_home.html` | **Poora chat interface** — sidebar (conversations list) + panel (messages + composer) | `chat:chat_home` URL, andar `_message_bubble.html` include karta hai |
| `templates/chat/_message_bubble.html` | Ek message ka bubble (user ya assistant, avatar ke sath) — history render karne ke liye | `chat_home.html` aur `_message_pending.html` dono include karte hain |
| `templates/chat/_message_pending.html` | Jab naya message bheja jata hai, ye fragment return hota hai (user bubble + streaming assistant bubble) | `chat/views.py::post_message` return karta hai |
| `templates/chat/_limit_exceeded.html` | Error message jab usage limit ya file-upload limit cross ho jaye | `chat/views.py` use karta hai |

---

## 4. `governance` app — Admin Dashboard (poora control yahan hai)

**Responsibility:** Admin ke liye sab kuch — users manage karna, models/pricing, per-user permissions, usage/upload limits, audit logs, department + system prompts, charts.

| File | Kaam | Kis se link hai |
|---|---|---|
| `governance/models.py` | **`SystemPromptVersion`** (department ke prompts), **`UsageLimit`** (token/budget/upload limits, per-user ya per-department), **`AuditLog`** (kisne kya change kiya) | `accounts.Department`, `accounts.User` ko reference karta hai |
| `governance/limits.py` | Usage limit check karne ka logic (`check_usage_limits`) aur file-upload validation (`validate_upload`) | `chat/views.py` isko directly call karta hai har message/upload par |
| `governance/audit.py` | `log_action()` helper — har admin action yahan se AuditLog mein likha jata hai | `governance/views.py` ki almost har function isko call karti hai |
| `governance/views.py` | **Sabse bara file is app ki.** Dashboard (charts), Users list, Models (add/pricing/enable), Model Permissions, Limits (CRUD), Departments (CRUD), Audit Logs, System Prompt | `chat.models` (ModelConfig, UserModelPermission), `accounts.models` (User, Department) — dono import karta hai |
| `governance/urls.py` | `/governance/...` sab routes | `config/urls.py` mein include hai |
| `governance/admin.py` | Django admin mein ye models dikhane ka config | Sirf `/admin/` ke liye (fallback/advanced use) |
| `templates/governance/dashboard.html` | Overview + Charts (Chart.js) | `governance:dashboard` URL |
| `templates/governance/users.html` | Users list — role + department dropdown se change | `governance:users` URL |
| `templates/governance/models.html` | AI Models list — add/pricing/enable/disable | `governance:models` URL |
| `templates/governance/model_permissions.html` | Ek specific model ke liye "kaun use kar sakta hai" | `governance:model_permissions` URL |
| `templates/governance/limits.html` + `limit_form.html` | Usage/Upload limits ki list + add/edit form | `governance:limits`, `limit_new`, `limit_edit` URLs |
| `templates/governance/departments.html` | Departments CRUD | `governance:departments` URL |
| `templates/governance/system_prompt.html` | Ek department ka system prompt edit karna | `governance:system_prompt` URL |
| `templates/governance/usage.html` | Per-user usage/cost table | `governance:usage` URL |
| `templates/governance/audit_logs.html` | Audit log history | `governance:audit_logs` URL |

---

## 5. Shared/Layout files

| File | Kaam |
|---|---|
| `templates/base.html` | **Sab pages ka parent template.** Sidebar navigation, top-right profile dropdown, `{% block content %}` jahan har page apna content dalta hai |
| `static/css/main.css` | **Poori app ki styling ek hi file mein.** Design tokens (colors/spacing) `:root` mein top par hain |

---

## 6. Naya kaam karte waqt kahan jayein (cheat-sheet)

| Karna kya hai | Kis file mein jayein |
|---|---|
| Naya field User/Department mein add karna | `accounts/models.py` → phir `makemigrations` |
| Naya AI provider (e.g. Google Gemini) add karna | `chat/providers.py` mein naya class, `chat/models.py` mein `ModelConfig.Provider` choice add karo |
| Chat ka UI/design badalna | `templates/chat/chat_home.html`, `_message_bubble.html`, aur `static/css/main.css` ka "Chat" section |
| Admin dashboard mein naya page add karna | `governance/views.py` (naya view) + `governance/urls.py` (naya route) + `templates/governance/` mein naya template + `templates/base.html` ke sidebar mein link add karo |
| Koi limit/restriction ka rule badalna | `governance/limits.py` |
| Naya permission/role rule | `accounts/permissions.py` |
| Poori app ka color/font badalna | `static/css/main.css` ke `:root` wale design tokens |
| Email templates, naye URLs | respective app ka `urls.py` + `views.py` |

---

## 7. Data flow example — "User message bhejta hai"

```
User composer form submit karta hai (chat_home.html)
        │  (hx-post, model_id dropdown se hx-include)
        ▼
chat/urls.py → chat/views.py::post_message
        │
        ├─→ governance/limits.py::check_usage_limits()   (limit check)
        ├─→ governance/limits.py::validate_upload()       (agar file hai)
        ├─→ chat/models.py::Message.objects.create()      (DB mein save)
        └─→ templates/chat/_message_pending.html return   (HTML fragment)
                │  (SSE connect shuru)
                ▼
chat/urls.py → chat/views.py::stream_message
        │
        ├─→ chat/router.py::classify_complexity() + select_model_candidates()
        ├─→ chat/prompts.py::build_system_prompt()  (governance.SystemPromptVersion se department prompt)
        ├─→ chat/providers.py::get_provider()        (OpenAI/Anthropic ko actual call)
        └─→ Message.save()  (final reply + tokens + cost DB mein)
```

---

*Last updated: 2026-08-29. Jab bhi naye app/model/major feature add ho, is document ko bhi update kar dena.*
