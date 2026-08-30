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
