"""SuperAdmin management of the global branding (Branding 1 / 2 / 3 / Custom) - see governance/branding.py.

Flow: pick a card -> Preview (nothing is saved, nothing changes for anyone) -> Apply (confirm dialog, then saved).
Every view here is SuperAdmin-only; the server re-validates everything it is sent, including the contrast warnings
(the browser is never trusted to say "I reviewed them"). Branding is presentation only: these views touch the
SiteBranding row and nothing else.
"""

from django import forms as django_forms
from django.contrib import messages as django_messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from accounts.models import User
from accounts.permissions import SuperAdminRequiredMixin, role_required
from governance import branding
from governance.audit import log_action
from governance.models import SiteBranding

MAX_LOGO_BYTES = 2 * 1024 * 1024
SCOPE = ".brand-preview"
LOGO_FIELDS = (("logo_light", "custom_logo_light"), ("logo_dark", "custom_logo_dark"))


def _flat_custom(row):
    """The Custom form's values: what was saved, else the starting defaults (flat: 'dark_text', 'primary', ...)."""
    saved = row.custom_config or {}
    merged = {**branding.CUSTOM_DEFAULTS, **{k: v for k, v in saved.items() if k not in ("light", "dark")}}
    for theme in branding.THEMES:
        for key, value in {**branding.CUSTOM_DEFAULTS[theme], **(saved.get(theme) or {})}.items():
            merged[f"{theme}_{key}"] = value
    return merged


def _cards(row):
    cards = []
    for key in branding.PRESET_KEYS:
        if key == "custom":
            brand = branding.custom_brand(
                row.custom_config or {}, brand_name=(row.custom_config or {}).get("brand_name")
            )
            swatches = brand["colors"]
            fonts = f'{brand["fonts"]["primary"]} / {brand["fonts"]["secondary"]}'
            description = branding.custom_brand({}, brand_name="")["tagline"]
        else:
            brand = branding.PRESETS[key]
            swatches = brand["colors"]
            fonts = f'{brand["fonts"]["primary"]} / {brand["fonts"]["secondary"]}'
            description = brand["tagline"]
        cards.append(
            {
                "key": key,
                "label": brand["label"],
                "tagline": description,
                "swatches": [swatches["primary"], swatches["secondary"], swatches["accent"]],
                "fonts": fonts,
            }
        )
    return cards


def _check_logo(uploaded):
    if uploaded.size > MAX_LOGO_BYTES:
        return _("That file is too big - keep it under 2 MB.")
    try:
        django_forms.ImageField().clean(uploaded)
    except ValidationError:
        return _("That doesn't look like a valid image file.")
    return None


def _selected(post, row):
    """(preset key, brand definition, custom clean config, errors) for what the form currently says."""
    key = post.get("preset") if post.get("preset") in branding.PRESET_KEYS else row.preset
    if key != "custom":
        return key, branding.PRESETS[key], None, {}
    clean, errors = branding.validate_custom(post)
    return key, branding.custom_brand(clean, brand_name=clean.get("brand_name") or None), clean, errors


def _preview_context(row, brand):
    logos = branding.logos_for(row, brand)
    report = branding.contrast_report(brand)
    return {
        "brand": brand,
        "name": brand["brand_name"] or row.site_name,
        "logos": logos,
        "mark_only": bool(brand.get("mark_only")),
        "scoped_css": branding.render_css(brand, scope=SCOPE),
        "fonts_url": brand["fonts"]["google_url"],
        "report": report,
        "warnings": [r for r in report if not r["ok"]],
    }


class BrandThemeView(SuperAdminRequiredMixin, TemplateView):
    template_name = "governance/branding_theme.html"

    def get_context_data(self, **kwargs):
        row = SiteBranding.load()
        errors = kwargs.pop("errors", None) or {}
        values = {**_flat_custom(row), **(kwargs.pop("posted", None) or {})}
        selected = kwargs.pop("selected", None) or row.preset

        def field(name, label):
            return {"name": name, "label": label, "value": values.get(name, ""), "error": errors.get(name, "")}

        return super().get_context_data(**kwargs) | {
            "row": row,
            "current": row.preset,
            "current_label": dict(SiteBranding.PRESET_CHOICES)[row.preset],
            "selected": selected,
            "cards": _cards(row),
            "values": values,
            "color_rows": [field(key, _(label)) for key, label in branding.COLOR_LABELS.items()],
            "theme_sections": [
                {
                    "label": _("Light theme") if theme == "light" else _("Dark theme"),
                    "rows": [field(f"{theme}_{key}", _(label)) for key, label in branding.THEME_COLOR_LABELS.items()],
                }
                for theme in branding.THEMES
            ],
            "logo_fields": [
                ("logo_light", _("Logo for light mode"), row.custom_logo_light),
                ("logo_dark", _("Logo for dark mode"), row.custom_logo_dark),
            ],
            "errors": errors,
        }


@role_required(User.Role.SUPERADMIN)
@require_POST
def brand_theme_preview(request):
    """The preview pane for whatever the form currently says. Saves nothing."""
    row = SiteBranding.load()
    key, brand, _clean, errors = _selected(request.POST, row)
    if errors:
        return render(request, "governance/_brand_preview.html", {"errors": errors, "invalid": True})
    context = _preview_context(row, brand)
    context["preset_key"] = key
    return render(request, "governance/_brand_preview.html", context)


@role_required(User.Role.SUPERADMIN)
@require_POST
def brand_theme_apply(request):
    """Save the chosen branding for everyone. Custom is validated first; a Custom with poor contrast is applied only
    when the SuperAdmin has explicitly acknowledged the warnings (the colours are never changed for them)."""
    row = SiteBranding.load()
    key, brand, clean, errors = _selected(request.POST, row)
    if request.POST.get("preset") not in branding.PRESET_KEYS:
        django_messages.error(request, _("Choose one of the four brandings first."))
        return redirect("governance:brand_theme")

    uploads = {}
    if key == "custom":
        for field, _model_field in LOGO_FIELDS:
            uploaded = request.FILES.get(field)
            if uploaded:
                problem = _check_logo(uploaded)
                if problem:
                    errors[field] = problem
                else:
                    uploads[field] = uploaded
        if not errors:
            warnings = branding.contrast_warnings(brand)
            if warnings and request.POST.get("acknowledge_contrast") != "1":
                errors["contrast"] = _(
                    "Some colour pairs are hard to read. Review them in the preview, "
                    "tick the acknowledgement, and apply again."
                )
    if errors:
        django_messages.error(request, _("Couldn't apply - see the messages below."))
        view = BrandThemeView(request=request)
        return view.render_to_response(view.get_context_data(errors=errors, posted=request.POST.dict(), selected=key))

    if key == "custom":
        row.custom_config = clean
        for field, model_field in LOGO_FIELDS:
            current = getattr(row, model_field)
            if field in uploads:
                if current:
                    current.delete(save=False)
                setattr(row, model_field, uploads[field])
            elif request.POST.get(f"remove_{field}") == "1" and current:
                current.delete(save=False)
                setattr(row, model_field, None)
    row.preset = key
    row.save()  # bumps `version`: the cached CSS is rebuilt on the next request
    log_action(request.user, "branding.theme_apply", row, new_value=key)
    django_messages.success(request, _("%(name)s is now applied for every user.") % {"name": brand["label"]})
    return redirect("governance:brand_theme")


@role_required(User.Role.SUPERADMIN)
@require_POST
def brand_theme_reset_custom(request):
    """Forget the Custom branding (settings and both logos). If it was applied, the app returns to Branding 1."""
    row = SiteBranding.load()
    for _field, model_field in LOGO_FIELDS:
        current = getattr(row, model_field)
        if current:
            current.delete(save=False)
        setattr(row, model_field, None)
    row.custom_config = {}
    if row.preset == "custom":
        row.preset = branding.DEFAULT_PRESET
    row.save()
    log_action(request.user, "branding.custom_reset", row)
    django_messages.success(request, _("Custom branding cleared."))
    return redirect("governance:brand_theme")


@role_required(User.Role.SUPERADMIN)
@require_GET
def brand_maintenance_preview(request):
    """The maintenance page as it would look under the active branding. (The app has no maintenance mode: nothing
    serves this page to visitors; it exists so the branding can be reviewed on it.)"""
    return render(request, "maintenance.html", {"preview": True})
