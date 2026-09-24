# Production server access — Oracle Cloud VM

Server: `141.148.220.88` (Oracle Cloud, Ubuntu 24.04, ARM64), user `ubuntu`,
key `C:\Users\PC\Downloads\ssh-key-2026-08-30.key`. App runs via Docker
Compose (`~/ai-client-portal` on the server) behind host Nginx on port 80.

## Connect

```bash
ssh -i "C:\Users\PC\Downloads\ssh-key-2026-08-30.key" ubuntu@141.148.220.88
```

Everything below assumes you're SSH'd in and have run:
```bash
cd ~/ai-client-portal
```

## If the admin login doesn't work

**Change the admin password yourself (recommended — interactive, safest):**
```bash
docker compose exec web python manage.py changepassword admin@example.com
```
It'll prompt you to type the new password twice (hidden input, never shown
on screen or saved to shell history).

**Create a brand new admin account instead** (if you'd rather not reuse the
existing one):
```bash
docker compose exec web python manage.py createsuperuser
```
Prompts for email and password interactively.

**Check whether the account got locked out** (5 wrong attempts locks it for
20 minutes — this is `django-axes`, working as designed, not a bug):
```bash
docker compose exec web python manage.py shell -c "
from axes.models import AccessAttempt
print(AccessAttempt.objects.filter(username='admin@example.com').count())
"
```
A non-zero count means recent failures are on record. A *successful* login
automatically clears them — no manual reset needed. To force-clear anyway:
```bash
docker compose exec web python manage.py axes_reset_username admin@example.com
```

**Double check the account itself is actually active/admin:**
```bash
docker compose exec web python manage.py shell -c "
from accounts.models import User
u = User.objects.get(email='admin@example.com')
print(u.is_active, u.role, u.is_staff, u.is_superuser)
"
```

## Common gotcha: HTTP vs HTTPS

The site currently only serves plain `http://141.148.220.88/` (see
`FORCE_HTTPS=False` in the server's `.env` — no domain/SSL cert exists yet).
If a browser auto-upgrades the address to `https://`, it will fail to
connect (nothing listens on port 443 yet) and can look exactly like "login
doesn't work" without ever reaching the login page at all. Always type
`http://` explicitly for now.

## Pending: mywheai.com domain cutover (owner action required)

Checked 2026-09-24: **neither `mywheai.com` nor `www.mywheai.com` resolve in DNS at all**
(`getaddrinfo ENOTFOUND` for both) — this is a registrar/DNS panel step, nothing in this
repository can do it. Once DNS is pointed at this server, three env vars on the **server's own
`.env`** (not `.env.example`) still need setting before the app will actually answer on that
hostname:

```
ALLOWED_HOSTS=141.148.220.88,mywheai.com,www.mywheai.com
CSRF_TRUSTED_ORIGINS=https://mywheai.com,https://www.mywheai.com
SITE_URL=https://mywheai.com
```

then `docker compose up -d --build` to pick them up. `SITE_URL` feeds absolute links in emails
(password reset, invoice share links, etc.) — leaving it as the IP would send those wrong once
the domain is live.

HTTPS is a separate step, still needed either way (see the gotcha above — nothing listens on
port 443 today): either put Cloudflare in front (proxy the DNS record, set SSL/TLS mode to
**Full (strict)**, which requires a real origin certificate — Cloudflare's own free origin CA
cert is the simplest option) or issue a certificate directly on the VPS (`certbot --nginx`) and
set `FORCE_HTTPS=True` in the server `.env` once a cert exists. Do this *before* relying on
`ALLOWED_HOSTS` alone for the domain to feel "live" — otherwise visitors on `https://mywheai.com`
get a connection error identical to the one this doc already describes for the bare IP.

## Restart / rebuild after a code change

```bash
git pull origin main
docker compose up -d --build
```
Migrations and `collectstatic` run automatically on `web` startup — no
separate step needed.

## Check what's actually running

```bash
docker compose ps                    # container status
docker compose logs web --tail=100   # web container's recent logs
docker compose logs --tail=50        # all services
```

## Full stack restart (rare — e.g. after a reboot doesn't come back cleanly)

```bash
docker compose down
docker compose up -d
```
(Containers are `restart: unless-stopped`, so a normal server reboot alone
already brings everything back without needing this.)
