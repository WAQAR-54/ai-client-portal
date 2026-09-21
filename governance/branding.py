"""Global branding: four looks (Branding 1, 2, 3, Custom) for the whole application, defined in ONE place.

How it works
------------
The application already runs on CSS custom properties (static/css/main.css: --accent, --secondary, --color-*,
--sidebar-*, --font-sans, radii, shadows ...), light and dark. So a branding is not a second stylesheet: it is a
small set of validated inputs (three brand colours, five neutrals per theme, two fonts) from which this module
DERIVES the values of those same properties and emits them as a `<style>` block after main.css.

* Branding 1 is the current appearance, unchanged. Its preset data mirrors main.css exactly (a test compares the two);
  when it is active NO override is emitted, so the stylesheet alone renders it.
* Branding 2 is the Web Host Era Brand Kit; Branding 3 is a separate technology identity; Custom is entered by a
  SuperAdmin. They differ in colour, type, radii, sidebar treatment and the login showcase - not just hue.
* Everything a value can reach the page through is validated here: colours must be #RRGGBB, font names a safe
  character set, the brand name plain text. Nothing else is ever written into the generated CSS (no free text).

The brand tokens the spec names map onto the tokens the app already consumes:

    --brand-primary   -> --accent            interactive fills: buttons, active state, focus ring, links on dark
    --brand-secondary -> --secondary         structural colour: headings, links, secondary buttons, avatars
    --brand-accent    -> (new)               highlight: text selection, pinned dot
    --brand-background/-surface/-text/-muted/-border -> --color-bg/-surface/-text/-text-muted/-border
    --brand-success/-warning/-danger -> --color-success/--warn/--color-danger
    --font-primary/--font-secondary -> --font-sans (body) / --font-display (headings)

Contrast is checked for every branding (`contrast_report`): a Custom branding that misses WCAG AA is reported to the
SuperAdmin before it is applied; the chosen colours are never changed silently.
"""

import colorsys
import re
from urllib.parse import quote_plus

from django.conf import settings
from django.core.cache import cache

# ---------------------------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------------------------
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SHORT_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3}$")
FONT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 \-]{0,38}[A-Za-z0-9])?$")
NAME_RE = re.compile(r"^[^<>{}\\\x00-\x1f]{1,60}$")

PRESET_KEYS = ("branding_1", "branding_2", "branding_3", "custom")
DEFAULT_PRESET = "branding_1"
DEFAULT_PRODUCT_NAME = "AI Client Portal"

COLOR_LABELS = {
    "primary": "Primary color",
    "secondary": "Secondary color",
    "accent": "Accent color",
}
THEME_COLOR_LABELS = {
    "background": "Background color",
    "surface": "Surface color",
    "text": "Text color",
    "muted": "Muted text color",
    "border": "Border color",
}
THEMES = ("light", "dark")


def normalize_hex(value):
    """'#abc' / '#AABBCC' -> '#aabbcc'; anything else -> None."""
    value = (value or "").strip()
    if SHORT_HEX_RE.match(value):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    return value.lower() if HEX_RE.match(value) else None


def clean_font_name(value):
    value = " ".join((value or "").split())
    return value if FONT_RE.match(value) else None


def clean_brand_name(value):
    value = " ".join((value or "").split())
    return value if NAME_RE.match(value) else None


# ---------------------------------------------------------------------------------------------------------------------
# Colour maths (WCAG 2.x relative luminance / contrast ratio)
# ---------------------------------------------------------------------------------------------------------------------
def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a, b, t):
    """t = 0 -> a, t = 1 -> b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def luminance(hex_color):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _shift_lightness(hex_color, delta):
    hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in _rgb(hex_color)))
    light = max(0.0, min(1.0, light + delta))
    return _hex(tuple(c * 255 for c in colorsys.hls_to_rgb(hue, light, sat)))


def darken(hex_color, amount):
    return _shift_lightness(hex_color, -amount)


def lighten(hex_color, amount):
    return _shift_lightness(hex_color, amount)


def readable_on(background, light="#ffffff", dark="#111111"):
    """The better of white / near-black as a label colour on `background`."""
    return light if contrast(light, background) >= contrast(dark, background) else dark


def ensure_contrast(color, background, target=4.5, step=0.03):
    """Move `color`'s lightness away from `background` until it reaches `target` (or cannot get any better)."""
    direction = 1 if luminance(background) < 0.5 else -1
    current = color
    for _ in range(40):
        if contrast(current, background) >= target:
            return current
        current = _shift_lightness(current, direction * step)
    return current


def _rgba(hex_color, alpha):
    r, g, b = _rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


# ---------------------------------------------------------------------------------------------------------------------
# Preset data
# ---------------------------------------------------------------------------------------------------------------------
# Status colours stay conventional in every branding (a warning must never look like a button), exactly like main.css.
SEMANTIC = {
    "light": {
        "success": "#1e9a6c", "success_soft": "#e3f5ec", "success_text": "#15694a",
        "danger": "#c7443f", "danger_soft": "#fbe7e8", "danger_text": "#a8332f", "danger_hover": "#a8332f",
        "warn": "#b5761e", "warn_soft": "#fcf0dc", "warn_dim": "#3d2f18",
    },
    "dark": {
        "success": "#3ecb8f", "success_soft": "#1b3324", "success_text": "#3ecb8f",
        "danger": "#f1817e", "danger_soft": "#3a1e1e", "danger_text": "#f1817e", "danger_hover": "#f5a19e",
        "warn": "#e3ac5c", "warn_soft": "#3a2a10", "warn_dim": "#3d2f18",
    },
}  # fmt: skip

_B1_FONTS = {
    "primary": "Manrope",
    "secondary": "Space Grotesk",
    "primary_stack": '"Manrope", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    "secondary_stack": '"Space Grotesk", var(--font-sans)',
    # The exact URL base.html has always loaded (kept byte-for-byte so Branding 1 changes nothing).
    "google_url": (
        "https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800"
        "&family=Space+Grotesk:wght@400;500;600;700"
        "&family=JetBrains+Mono:wght@400;500;600&family=Noto+Nastaliq+Urdu:wght@400;600;700"
        "&family=Noto+Naskh+Arabic:wght@400;500;600;700&display=swap"
    ),
}

# The CURRENT appearance, verbatim from static/css/main.css (governance/test_branding.py compares the two).
BRANDING_1 = {
    "key": "branding_1",
    "label": "Branding 1",
    "tagline": "Current application branding",
    "brand_name": None,  # None = the name saved under Settings > Branding (SiteBranding.site_name)
    "logo": None,  # None = the logo saved under Settings > Branding, if any
    "colors": {"primary": "#00aef0", "secondary": "#122268", "accent": "#00aef0"},
    "fonts": _B1_FONTS,
    "exact": {
        "light": {
            "--color-bg": "#f1f0eb", "--color-surface": "#f9f8f4", "--color-surface-muted": "#ecebe4",
            "--color-surface-active": "#dfdcd2", "--color-border": "rgba(32, 26, 10, 0.09)",
            "--color-border-strong": "#c9c4b5", "--color-text": "#232019", "--color-text-muted": "#67645a",
            "--color-text-faint": "#6c685e",
            "--secondary": "#122268", "--secondary-hover": "#0b1848", "--secondary-soft": "#e3e7f3",
            "--secondary-text": "#93a5f2", "--secondary-ink": "#93a5f2", "--brand-navy": "#122268",
            "--accent": "#00aef0", "--accent-hover": "#0299d6", "--accent-soft": "#e3f6fd", "--accent-dim": "#0f3d52",
            "--accent-text": "#122268", "--on-accent": "#122268",
            "--color-success": "#1e9a6c", "--color-success-soft": "#e3f5ec", "--color-success-text": "#15694a",
            "--color-danger": "#c7443f", "--color-danger-soft": "#fbe7e8", "--color-danger-text": "#a8332f",
            "--color-danger-hover": "#a8332f", "--warn": "#b5761e", "--warn-soft": "#fcf0dc", "--warn-dim": "#3d2f18",
            "--brand-accent": "#00aef0", "--on-brand-accent": "#122268", "--on-brand-navy": "#ffffff",
            "--sidebar-bg": "#f9f8f4", "--sidebar-text": "#232019", "--sidebar-text-muted": "#67645a",
            "--sidebar-hover-bg": "#ecebe4", "--sidebar-active-bg": "#ecebe4", "--sidebar-active-text": "#232019",
            "--sidebar-border": "rgba(32, 26, 10, 0.09)",
            "--shadow-sm": "0 1px 2px rgba(32, 26, 10, 0.05)",
            "--shadow-md": "0 1px 2px rgba(32, 26, 10, 0.05), 0 8px 22px -8px rgba(32, 26, 10, 0.11)",
            "--shadow-lg": "0 4px 10px -2px rgba(32, 26, 10, 0.09), 0 18px 38px -14px rgba(32, 26, 10, 0.17)",
            "--shadow-card": "0 1px 2px rgba(32, 26, 10, 0.05), 0 8px 22px -8px rgba(32, 26, 10, 0.11)",
            "--shadow-card-hover": "0 4px 10px -2px rgba(32, 26, 10, 0.09), 0 18px 38px -14px rgba(32, 26, 10, 0.17)",
            "--on-danger": "#ffffff", "--on-success": "#ffffff", "--check-mark": "#ffffff",
            "--showcase-text": "#ffffff", "--showcase-icon": "#7fddfb",
            "--alert-danger-text": "#c7443f", "--alert-success-text": "#1e9a6c", "--alert-info-text": "#00aef0",
        },
        "dark": {
            "--color-bg": "#0e1015", "--color-surface": "#12151c", "--color-surface-muted": "#1a1e28",
            "--color-surface-active": "#232836", "--color-border": "#262b37", "--color-border-strong": "#3a4152",
            "--color-text": "#edeef2", "--color-text-muted": "#aeb2be", "--color-text-faint": "#8a91a1",
            "--secondary": "#93a5f2", "--secondary-hover": "#a8b7f7", "--secondary-soft": "#262c4a",
            "--secondary-text": "#93a5f2", "--secondary-ink": "#122268",
            "--brand-navy": "#122268", "--on-brand-navy": "#ffffff",
            "--accent": "#00aef0", "--accent-hover": "#35c5fb", "--accent-soft": "#123244", "--accent-dim": "#0f3d52",
            "--accent-text": "#7fddfb", "--on-accent": "#122268",
            "--color-success": "#3ecb8f", "--color-success-soft": "#1b3324", "--color-success-text": "#3ecb8f",
            "--color-danger": "#f1817e", "--color-danger-soft": "#3a1e1e", "--color-danger-text": "#f1817e",
            "--color-danger-hover": "#f5a19e", "--warn": "#e3ac5c", "--warn-soft": "#3a2a10", "--warn-dim": "#3d2f18",
            "--brand-accent": "#00aef0", "--on-brand-accent": "#122268",
            "--sidebar-bg": "#12151c", "--sidebar-text": "#edeef2", "--sidebar-text-muted": "#aeb2be",
            "--sidebar-hover-bg": "#1a1e28", "--sidebar-active-bg": "#232836", "--sidebar-active-text": "#edeef2",
            "--sidebar-border": "#262b37",
            "--shadow-sm": "0 1px 2px rgba(0, 0, 0, 0.3)",
            "--shadow-md": "0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -8px rgba(0, 0, 0, 0.5)",
            "--shadow-lg": "0 6px 14px -2px rgba(0, 0, 0, 0.4), 0 24px 48px -14px rgba(0, 0, 0, 0.6)",
            "--shadow-card": "0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -8px rgba(0, 0, 0, 0.5)",
            "--shadow-card-hover": "0 6px 14px -2px rgba(0, 0, 0, 0.4), 0 24px 48px -14px rgba(0, 0, 0, 0.6)",
            "--color-scheme": "dark",
            "--on-danger": "#ffffff", "--on-success": "#ffffff", "--check-mark": "#ffffff",
            "--showcase-text": "#ffffff", "--showcase-icon": "#7fddfb",
            "--alert-danger-text": "#f1817e", "--alert-success-text": "#3ecb8f", "--alert-info-text": "#00aef0",
        },
    },
    # Values the contrast report needs that main.css expresses through var() indirection.
    "report": {
        "light": {
            "background": "#f1f0eb", "surface": "#f9f8f4", "text": "#232019", "muted": "#67645a", "faint": "#6c685e",
            "primary": "#00aef0", "on_primary": "#122268", "link": "#122268",
            "accent_soft": "#e3f6fd", "accent_text": "#122268", "nav_bg": "#f9f8f4", "nav_text": "#232019",
        },
        "dark": {
            "background": "#0e1015", "surface": "#12151c", "text": "#edeef2", "muted": "#aeb2be", "faint": "#8a91a1",
            "primary": "#00aef0", "on_primary": "#122268", "link": "#93a5f2",
            "accent_soft": "#123244", "accent_text": "#7fddfb", "nav_bg": "#12151c", "nav_text": "#edeef2",
        },
    },
}  # fmt: skip

# Branding 2 - Web Host Era Brand Kit (Brand Kit.pdf). Hex values are the printed labels on pages 7-9:
#   Too Blue to be True 008CFF | Matt Black 151515 | White FFFFFF | Void 00172A | Soulstone Blue 0055A5
#   High Seas 7DB5DC | Halloween F96939 | Fennel Fiesta 01C970 | Lime Fizz C7FC35 | Pearl Powder F9FFEB
#   Dark Charcoal 333333 | Nickel 737373 | Philippine Silver B3B3B3 | Light Silver D9D9D9 | Plaster E9EAE9
# (The page-7 swatch and the logo are drawn in #2C6EF8 while its label says 008CFF; the printed value is used for the
# token and the logo artwork is left exactly as supplied. See docs/OPERATIONS.md.)
# Derived (not in the kit): #4d4d4d muted text (Dark Charcoal-Nickel midpoint, so muted text passes AA on Plaster),
# #f3f4f3 muted surface (Plaster lightened).
BRANDING_2 = {
    "key": "branding_2",
    "label": "Branding 2",
    "tagline": "Web Host Era - Brand Kit",
    "sidebar_dark": True,
    "brand_name": "Web Host Era",
    "logo": {
        "light": "branding/whe-logo-blue.png",
        "dark": "branding/whe-logo-white.png",
        "favicon": "branding/whe-mark.png",
    },
    "colors": {"primary": "#008cff", "secondary": "#0055a5", "accent": "#c7fc35"},
    "light": {
        "background": "#ffffff", "surface": "#ffffff", "text": "#151515", "muted": "#4d4d4d", "border": "#d9d9d9",
        "surface_muted": "#f3f4f3", "surface_active": "#e9eae9", "border_strong": "#b3b3b3", "faint": "#737373",
        "sidebar": {
            "bg": "#00172a", "text": "#ffffff", "muted": "#7db5dc", "hover": "#0a2740", "active_bg": "#008cff",
            "active_text": "#151515", "border": "#0a2740",
        },
    },
    "dark": {
        "background": "#00172a", "surface": "#062338", "text": "#ffffff", "muted": "#b9c9d6", "border": "#12395a",
        "surface_muted": "#0b2b45", "surface_active": "#12395a", "border_strong": "#1f4f7a", "faint": "#93a9bb",
        "secondary_text": "#7db5dc",
        "sidebar": {
            "bg": "#001220", "text": "#ffffff", "muted": "#7db5dc", "hover": "#0a2740", "active_bg": "#008cff",
            "active_text": "#151515", "border": "#0a2740",
        },
    },
    "semantic": {
        "light": {"success": "#01c970", "success_soft": "#e0f9ec", "success_text": "#00693a", "danger": "#f96939",
                  "danger_soft": "#feeae3", "danger_text": "#b33a14", "danger_hover": "#d9541f"},
        "dark": {"success": "#01c970", "success_soft": "#06332a", "success_text": "#3ee39a", "danger": "#f96939",
                 "danger_soft": "#3a1a10", "danger_text": "#ff9a78", "danger_hover": "#ff8256"},
    },
    "radii": ("10px", "14px", "22px"),
    "showcase": "linear-gradient(160deg, #00172a 0%, #0055a5 60%, #008cff 100%)",
    "fonts": {
        "primary": "Gilroy",
        "secondary": "Mont-Trial",
        # Gilroy and Mont-Trial are commercial fonts and are NOT bundled or downloaded. Until licensed copies are added
        # as @font-face, the closest open Google Fonts (Urbanist ~ Gilroy, Montserrat ~ Mont) are used, then Inter.
        "primary_stack": '"Gilroy", "Urbanist", Inter, system-ui, sans-serif',
        "secondary_stack": '"Mont-Trial", "Montserrat", Inter, system-ui, sans-serif',
        "google_url": (
            "https://fonts.googleapis.com/css2?family=Urbanist:wght@400;500;600;700;800"
            "&family=Montserrat:wght@500;600;700;800&family=Inter:wght@400;500;600;700"
            "&family=JetBrains+Mono:wght@400;500;600&family=Noto+Nastaliq+Urdu:wght@400;600;700"
            "&family=Noto+Naskh+Arabic:wght@400;500;600;700&display=swap"
        ),
    },
}  # fmt: skip

# Branding 3 - a modern premium-technology identity: deep indigo + electric cyan on cool slate, crisp radii, a
# light sidebar with an indigo pill (Branding 2 uses a dark sidebar, Branding 1 a warm neutral one), Sora + Inter.
BRANDING_3 = {
    "key": "branding_3",
    "label": "Branding 3",
    "tagline": "Modern technology",
    "brand_name": None,
    "mark_only": True,
    "logo": {"light": "branding/modern-mark-light.svg", "dark": "branding/modern-mark-dark.svg",
             "favicon": "branding/modern-mark-light.svg"},
    "colors": {"primary": "#4f3fe0", "secondary": "#1f2a5c", "accent": "#22d3ee"},
    "light": {
        "background": "#f4f6fb", "surface": "#ffffff", "text": "#151a2e", "muted": "#4c5470", "border": "#dfe3ef",
        "surface_muted": "#eceff8", "surface_active": "#dde2f2", "border_strong": "#c4cadf", "faint": "#5e6684",
        "sidebar": {
            "bg": "#ffffff", "text": "#151a2e", "muted": "#4c5470", "hover": "#eceff8", "active_bg": "#4f3fe0",
            "active_text": "#ffffff", "border": "#dfe3ef",
        },
    },
    "dark": {
        "background": "#0a0c17", "surface": "#11142a", "text": "#eceefb", "muted": "#aab0cf", "border": "#262b4d",
        "surface_muted": "#181c38", "surface_active": "#212650", "border_strong": "#363d6b", "faint": "#8f96b8",
        "secondary_text": "#67e8f9",
        "sidebar": {
            "bg": "#0d1024", "text": "#eceefb", "muted": "#aab0cf", "hover": "#181c38", "active_bg": "#8b7cff",
            "active_text": "#0a0c17", "border": "#262b4d",
        },
    },
    "semantic": None,
    "radii": ("4px", "6px", "10px"),
    "showcase": "linear-gradient(150deg, #151a4a 0%, #4f3fe0 62%, #22d3ee 100%)",
    "fonts": {
        "primary": "Inter",
        "secondary": "Sora",
        "primary_stack": 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        "secondary_stack": 'Sora, Inter, system-ui, sans-serif',
        "google_url": (
            "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Sora:wght@400;500;600;700"
            "&family=JetBrains+Mono:wght@400;500;600&family=Noto+Nastaliq+Urdu:wght@400;600;700"
            "&family=Noto+Naskh+Arabic:wght@400;500;600;700&display=swap"
        ),
    },
}  # fmt: skip

PRESETS = {"branding_1": BRANDING_1, "branding_2": BRANDING_2, "branding_3": BRANDING_3}

# Starting values offered in the Custom form (a neutral, accessible pair of themes the SuperAdmin then edits).
CUSTOM_DEFAULTS = {
    "brand_name": "",
    "primary": "#0d6efd", "secondary": "#1b2a4e", "accent": "#f5b301",
    "light": {
        "background": "#f5f6f8", "surface": "#ffffff", "text": "#1a1d24", "muted": "#4f5665", "border": "#dcdfe6",
    },
    "dark": {"background": "#0f1115", "surface": "#171a21", "text": "#eef0f4", "muted": "#aab0be", "border": "#2a2f3b"},
    "font_primary": "Inter", "font_secondary": "Inter", "google_fonts": True,
}  # fmt: skip


# ---------------------------------------------------------------------------------------------------------------------
# Custom configuration: validation -> a brand definition shaped like the presets
# ---------------------------------------------------------------------------------------------------------------------
def validate_custom(data):
    """(clean_config, errors). `data` is a flat mapping of form values. Nothing invalid survives into `clean`."""
    errors = {}
    clean = {"light": {}, "dark": {}}

    name = clean_brand_name(data.get("brand_name", ""))
    if not name:
        errors["brand_name"] = "Enter a brand name (1-60 characters, no < > { } or backslash)."
    clean["brand_name"] = name or ""

    for key in COLOR_LABELS:
        value = normalize_hex(data.get(key, ""))
        if value is None:
            errors[key] = "Use a 6-digit hex color like #0d6efd."
        clean[key] = value or CUSTOM_DEFAULTS[key]
    for theme in THEMES:
        for key in THEME_COLOR_LABELS:
            field = f"{theme}_{key}"
            value = normalize_hex(data.get(field, ""))
            if value is None:
                errors[field] = "Use a 6-digit hex color like #0d6efd."
            clean[theme][key] = value or CUSTOM_DEFAULTS[theme][key]

    for key in ("font_primary", "font_secondary"):
        raw = data.get(key, "")
        value = clean_font_name(raw)
        if not value:
            errors[key] = "Use a font name of letters, numbers, spaces and hyphens (e.g. Inter or Open Sans)."
        clean[key] = value or CUSTOM_DEFAULTS[key]
    clean["google_fonts"] = str(data.get("google_fonts", "")).lower() in ("1", "true", "on", "yes")
    return clean, errors


def custom_brand(config, brand_name=None):
    """A brand definition (same shape as the presets) from a validated custom config."""
    config = config or {}
    merged = {
        **CUSTOM_DEFAULTS,
        **{k: v for k, v in config.items() if k not in ("light", "dark")},
        "light": {**CUSTOM_DEFAULTS["light"], **(config.get("light") or {})},
        "dark": {**CUSTOM_DEFAULTS["dark"], **(config.get("dark") or {})},
    }
    google = merged["google_fonts"]
    families = []
    for name in (merged["font_primary"], merged["font_secondary"]):
        if name not in families:
            families.append(name)
    google_url = ""
    if google:
        google_url = (
            "https://fonts.googleapis.com/css2?"
            + "&".join(f"family={quote_plus(name)}:wght@400;500;600;700" for name in families)
            + "&family=JetBrains+Mono:wght@400;500;600&family=Noto+Nastaliq+Urdu:wght@400;600;700"
            "&family=Noto+Naskh+Arabic:wght@400;500;600;700&display=swap"
        )
    return {
        "key": "custom",
        "label": "Custom",
        "tagline": "Your own branding",
        "brand_name": brand_name if brand_name is not None else (merged["brand_name"] or None),
        "logo": None,  # filled from the uploaded custom logos by effective_brand()
        "colors": {k: merged[k] for k in COLOR_LABELS},
        "light": dict(merged["light"]),
        "dark": dict(merged["dark"]),
        "semantic": None,
        "radii": None,
        "showcase": None,
        "fonts": {
            "primary": merged["font_primary"],
            "secondary": merged["font_secondary"],
            "primary_stack": f'"{merged["font_primary"]}", Inter, system-ui, sans-serif',
            "secondary_stack": f'"{merged["font_secondary"]}", var(--font-sans)',
            "google_url": google_url or _B1_FONTS["google_url"],
        },
    }


# ---------------------------------------------------------------------------------------------------------------------
# Deriving the token values
# ---------------------------------------------------------------------------------------------------------------------
def _theme_inputs(brand, theme):
    """The resolved inputs for one theme: colours, neutrals, sidebar and semantic sets."""
    colors = brand["colors"]
    t = brand[theme]
    primary, secondary, accent = colors["primary"], colors["secondary"], colors["accent"]
    bg, surface, text, muted, border = t["background"], t["surface"], t["text"], t["muted"], t["border"]
    dark = theme == "dark"
    surface_muted = t.get("surface_muted") or mix(surface, text, 0.05)
    surface_active = t.get("surface_active") or mix(surface, text, 0.11)
    border_strong = t.get("border_strong") or mix(border, text, 0.28)
    faint = t.get("faint") or mix(muted, bg, 0.12)

    # Structural colour as a TEXT role: on dark surfaces it is lifted until it reads (AA), on light ones used as chosen.
    secondary_role = t.get("secondary_text") or (ensure_contrast(secondary, surface, 4.5) if dark else secondary)
    soft = mix(surface, primary, 0.22 if dark else 0.12)  # 12-22 % of the brand colour over the surface
    on_primary = readable_on(primary)
    if dark:
        accent_text = ensure_contrast(lighten(primary, 0.05), soft, 4.5)
    else:
        accent_text = next(
            (c for c in (secondary, text, "#111111") if min(contrast(c, soft), contrast(c, primary)) >= 4.5),
            max((secondary, text, "#111111"), key=lambda c: min(contrast(c, soft), contrast(c, primary))),
        )
    sidebar = t.get("sidebar") or {
        "bg": surface, "text": text, "muted": muted, "hover": surface_muted, "active_bg": surface_active,
        "active_text": text, "border": border,
    }  # fmt: skip
    semantic = dict((brand.get("semantic") or {}).get(theme) or {})
    base = dict(SEMANTIC[theme])
    base.update(semantic)
    return {
        "dark": dark, "primary": primary, "secondary": secondary, "accent": accent, "bg": bg, "surface": surface,
        "text": text, "muted": muted, "border": border, "surface_muted": surface_muted,
        "surface_active": surface_active, "border_strong": border_strong, "faint": faint,
        "secondary_role": secondary_role, "soft": soft, "on_primary": on_primary, "accent_text": accent_text,
        "sidebar": sidebar, "semantic": base,
    }  # fmt: skip


def tokens_for(brand, theme):
    """{css custom property: value} for one theme, derived from the brand's inputs (Branding 1: verbatim main.css)."""
    if brand.get("exact"):
        return dict(brand["exact"][theme])
    i = _theme_inputs(brand, theme)
    dark = i["dark"]
    sec = i["secondary_role"]
    sb = i["sidebar"]
    sem = i["semantic"]

    def status(name):  # soft / text variants, kept legible whatever the surface
        colour = sem[name]
        soft = sem.get(f"{name}_soft") or mix(i["surface"], colour, 0.14)
        text = sem.get(f"{name}_text") or ensure_contrast(colour, soft, 4.5)
        return colour, soft, text

    ok, ok_soft, ok_text = status("success")
    bad, bad_soft, bad_text = status("danger")
    tokens = {
        "--color-bg": i["bg"], "--color-surface": i["surface"], "--color-surface-muted": i["surface_muted"],
        "--color-surface-active": i["surface_active"], "--color-border": i["border"],
        "--color-border-strong": i["border_strong"], "--color-text": i["text"], "--color-text-muted": i["muted"],
        "--color-text-faint": i["faint"],
        "--secondary": sec,
        "--secondary-hover": lighten(sec, 0.08) if dark else darken(sec, 0.08),
        "--secondary-soft": mix(sec, i["surface"], 0.82 if dark else 0.88),
        "--secondary-text": sec if dark else ensure_contrast(mix(i["secondary"], "#ffffff", 0.6), i["secondary"], 4.5),
        "--secondary-ink": readable_on(sec),
        "--brand-navy": i["secondary"],
        "--on-brand-navy": readable_on(i["secondary"]),
        "--accent": i["primary"],
        "--accent-hover": lighten(i["primary"], 0.07) if dark else darken(i["primary"], 0.07),
        "--accent-soft": i["soft"],
        "--accent-dim": mix(i["primary"], "#000000", 0.7),
        "--accent-text": i["accent_text"],
        "--on-accent": i["on_primary"],
        "--brand-accent": i["accent"],
        "--on-brand-accent": readable_on(i["accent"]),
        "--sidebar-bg": sb["bg"], "--sidebar-text": sb["text"], "--sidebar-text-muted": sb["muted"],
        "--sidebar-hover-bg": sb["hover"], "--sidebar-active-bg": sb["active_bg"],
        "--sidebar-active-text": sb["active_text"], "--sidebar-border": sb["border"],
        "--color-success": ok, "--color-success-soft": ok_soft, "--color-success-text": ok_text,
        "--color-danger": bad, "--color-danger-soft": bad_soft, "--color-danger-text": bad_text,
        "--color-danger-hover": sem["danger_hover"],
        "--warn": sem["warn"], "--warn-soft": sem["warn_soft"], "--warn-dim": sem["warn_dim"],
        "--on-danger": readable_on(bad), "--on-success": readable_on(ok), "--check-mark": i["on_primary"],
        "--alert-danger-text": bad_text, "--alert-success-text": ok_text, "--alert-info-text": i["accent_text"],
        "--console-accent": ensure_contrast(i["primary"], "#0b0d12", 4.5),
        "--console-accent-dim": mix("#0b0d12", i["primary"], 0.25),
        "--console-hover": mix("#0b0d12", i["primary"], 0.32),
        "--console-on-accent": readable_on(ensure_contrast(i["primary"], "#0b0d12", 4.5), dark="#04141a"),
        "--showcase-text": readable_on(i["secondary"]),
        "--showcase-icon": mix(i["primary"], "#ffffff", 0.55),
    }  # fmt: skip
    r, g, b = _rgb(i["text"])
    if dark:
        tokens.update(
            {
                "--shadow-sm": "0 1px 2px rgba(0, 0, 0, 0.3)",
                "--shadow-md": "0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -8px rgba(0, 0, 0, 0.5)",
                "--shadow-lg": "0 6px 14px -2px rgba(0, 0, 0, 0.4), 0 24px 48px -14px rgba(0, 0, 0, 0.6)",
                "--color-scheme": "dark",
            }
        )
    else:
        tokens.update(
            {
                "--shadow-sm": f"0 1px 2px rgba({r}, {g}, {b}, 0.06)",
                "--shadow-md": f"0 1px 2px rgba({r}, {g}, {b}, 0.06), 0 8px 22px -8px rgba({r}, {g}, {b}, 0.14)",
                "--shadow-lg": f"0 4px 10px -2px rgba({r}, {g}, {b}, 0.1), 0 18px 38px -14px rgba({r}, {g}, {b}, 0.2)",
            }
        )
    tokens["--shadow-card"] = tokens["--shadow-md"]
    tokens["--shadow-card-hover"] = tokens["--shadow-lg"]
    return tokens


def shared_tokens(brand):
    """Tokens that do not change with the light/dark theme: fonts, radii, the login showcase gradient."""
    fonts = brand["fonts"]
    tokens = {"--font-sans": fonts["primary_stack"], "--font-display": fonts["secondary_stack"]}
    if brand.get("exact"):
        tokens.update(
            {
                "--radius-sm": "6px", "--radius-md": "10px", "--radius-lg": "16px",
                "--showcase-bg": "linear-gradient(160deg, #0b1848 0%, #122268 55%, #00aef0 100%)",
            }
        )  # fmt: skip
    if brand.get("radii"):
        tokens["--radius-sm"], tokens["--radius-md"], tokens["--radius-lg"] = brand["radii"]
    if brand.get("showcase"):
        tokens["--showcase-bg"] = brand["showcase"]
    elif not brand.get("exact"):
        secondary, primary = brand["colors"]["secondary"], brand["colors"]["primary"]
        tokens["--showcase-bg"] = (
            f"linear-gradient(160deg, {darken(secondary, 0.06)} 0%, {secondary} 60%, "
            f"{mix(secondary, primary, 0.45)} 100%)"
        )
    return tokens


def _declarations(tokens, indent="    "):
    lines = []
    for name, value in tokens.items():
        if name == "--color-scheme":
            lines.append(f"{indent}color-scheme: {value};")
        else:
            lines.append(f"{indent}{name}: {value};")
    return "\n".join(lines)


def render_css(brand, scope=None):
    """The override stylesheet for `brand`. scope=None: the real page (light, then dark via media query and
    [data-theme=dark], exactly the structure main.css uses). scope='.sel': the preview box, where the theme is picked
    by [data-preview-theme]. Branding 1 on the real page needs no override (main.css already is Branding 1)."""
    shared = shared_tokens(brand)
    light, dark = tokens_for(brand, "light"), tokens_for(brand, "dark")
    if scope is None:
        if brand.get("exact"):
            return ""
        return (
            "::selection { background: var(--brand-accent); color: var(--on-brand-accent); }\n"
            f":root {{\n{_declarations({**shared, **light})}\n}}\n"
            "@media (prefers-color-scheme: dark) {\n"
            '    :root:not([data-theme="light"]) {\n'
            f"{_declarations(dark, '        ')}\n    }}\n}}\n"
            f':root[data-theme="dark"] {{\n{_declarations(dark)}\n}}\n'
        )
    return (
        f'{scope}[data-preview-theme="light"] {{\n{_declarations({**shared, **light})}\n}}\n'
        f'{scope}[data-preview-theme="dark"] {{\n{_declarations({**shared, **dark})}\n}}\n'
    )


# ---------------------------------------------------------------------------------------------------------------------
# Accessibility report
# ---------------------------------------------------------------------------------------------------------------------
AA_TEXT = 4.5


def _report_inputs(brand, theme):
    if brand.get("report"):
        return brand["report"][theme]
    i = _theme_inputs(brand, theme)
    tokens = tokens_for(brand, theme)
    return {
        "background": i["bg"], "surface": i["surface"], "text": i["text"], "muted": i["muted"], "faint": i["faint"],
        "primary": i["primary"], "on_primary": i["on_primary"], "link": i["secondary_role"],
        "accent_soft": i["soft"], "accent_text": i["accent_text"],
        "nav_bg": i["sidebar"]["bg"], "nav_text": i["sidebar"]["text"], "nav_active_bg": i["secondary"],
        "nav_active_text": readable_on(i["secondary"]), "tokens": tokens,
    }  # fmt: skip


def contrast_report(brand):
    """[{theme, label, ratio, required, ok}] for the pairs that decide readability. Ratios are WCAG 2 contrast."""
    rows = []
    for theme in THEMES:
        r = _report_inputs(brand, theme)
        pairs = [
            ("Body text on background", r["text"], r["background"]),
            ("Body text on surface", r["text"], r["surface"]),
            ("Muted text on surface", r["muted"], r["surface"]),
            ("Muted text on background", r["muted"], r["background"]),
            ("Form labels / faint text on surface", r["faint"], r["surface"]),
            ("Primary button label", r["on_primary"], r["primary"]),
            ("Links and headings on background", r["link"], r["background"]),
            ("Badge text on tinted background", r["accent_text"], r["accent_soft"]),
            ("Navigation text", r["nav_text"], r["nav_bg"]),
        ]
        if r.get("nav_active_bg"):
            pairs.append(("Active navigation item", r["nav_active_text"], r["nav_active_bg"]))
        for label, fg, bg in pairs:
            ratio = contrast(fg, bg)
            rows.append(
                {"theme": theme, "label": label, "ratio": round(ratio, 2), "required": AA_TEXT, "ok": ratio >= AA_TEXT}
            )
    return rows


def contrast_warnings(brand):
    return [row for row in contrast_report(brand) if not row["ok"]]


# ---------------------------------------------------------------------------------------------------------------------
# The active branding
# ---------------------------------------------------------------------------------------------------------------------
class Logo:
    """Just enough of an ImageFieldFile for the templates: truthiness and .url."""

    def __init__(self, url):
        self.url = url

    def __bool__(self):
        return bool(self.url)


class ActiveBranding:
    """What every template sees as `site_branding` (same attribute names the old SiteBranding row exposed) plus the
    generated CSS, fonts URL and favicon. Built from the SiteBranding row; the row itself is not modified."""

    def __init__(self, row, brand):
        self.preset = brand["key"]
        self.brand = brand
        self.row = row
        self.site_name = brand["brand_name"] or row.site_name
        self.tagline = row.tagline
        logos = logos_for(row, brand)
        self.logo = Logo(logos["light"])
        self.logo_dark = Logo(logos["dark"])
        self.favicon = Logo(logos["favicon"])
        self.sidebar_dark = bool(brand.get("sidebar_dark"))  # the sidebar is dark even in the light theme
        self.mark_only = bool(brand.get("mark_only"))  # the logo is a symbol only: the name is written beside it
        self.fonts_url = brand["fonts"]["google_url"]
        self.css = _cached_css(row, brand)


def logos_for(row, brand):
    """{"light", "dark", "favicon"} URLs (or "") of a brand: presets 2 and 3 ship their own files under
    static/branding/, Branding 1 uses the logo/favicon saved under Settings > Branding, Custom its own uploaded
    light/dark logos."""
    static = settings.STATIC_URL
    if brand["key"] == "custom":
        return {
            "light": row.custom_logo_light.url if row.custom_logo_light else "",
            "dark": row.custom_logo_dark.url if row.custom_logo_dark else "",
            "favicon": row.favicon.url if row.favicon else "",
        }
    if brand["key"] == "branding_1":
        return {
            "light": row.logo.url if row.logo else "",
            "dark": "",
            "favicon": row.favicon.url if row.favicon else "",
        }
    logo = brand["logo"]
    return {"light": static + logo["light"], "dark": static + logo["dark"], "favicon": static + logo["favicon"]}


def brand_for_row(row):
    """The brand definition selected by a SiteBranding row (unknown/invalid presets fall back to Branding 1)."""
    preset = getattr(row, "preset", DEFAULT_PRESET)
    if preset == "custom":
        config = row.custom_config or {}
        if config.get("brand_name") and not validate_custom(_flatten_config(config))[1]:
            return custom_brand(config)
        return BRANDING_1  # a saved custom config that no longer validates never breaks the site
    return PRESETS.get(preset, BRANDING_1)


def _flatten_config(config):
    flat = {k: v for k, v in config.items() if k not in ("light", "dark")}
    for theme in THEMES:
        for key, value in (config.get(theme) or {}).items():
            flat[f"{theme}_{key}"] = value
    return flat


def _cached_css(row, brand):
    """The generated CSS, cached per (preset, row version): saving the branding bumps the version, so the next request
    builds (and caches) the new CSS - nothing stale survives, and nothing is polled."""
    key = f"branding:css:{brand['key']}:{getattr(row, 'version', 1)}"
    try:
        css = cache.get(key)
        if css is None:
            css = render_css(brand)
            cache.set(key, css, 3600)
        logos = logos_for(row, brand)
        cache.set(
            LAST_KNOWN_KEY,
            {
                "css": css,
                "fonts_url": brand["fonts"]["google_url"],
                "name": brand["brand_name"] or row.site_name,
                "logo": logos["light"],
                "logo_dark": logos["dark"],
                "mark_only": bool(brand.get("mark_only")),
            },
            None,
        )
        return css
    except Exception:  # noqa: BLE001 - branding must never take a page down
        return render_css(brand)


LAST_KNOWN_KEY = "branding:last_known"


def active_branding(row=None):
    from governance.models import SiteBranding

    row = row or SiteBranding.load()
    return ActiveBranding(row, brand_for_row(row))


def last_known_branding():
    """{"css", "fonts_url", "name"} of the most recently rendered branding, from the cache only - no database. For
    pages that must render while the database may be down (the error page, the maintenance page)."""
    try:
        return cache.get(LAST_KNOWN_KEY) or {}
    except Exception:  # noqa: BLE001
        return {}


def _email_logo(url):
    """An absolute URL for an email (SITE_URL + path), or "" for no logo / an SVG (mail clients do not render SVG)."""
    if not url or url.lower().endswith(".svg"):
        return ""
    return url if url.startswith("http") else getattr(settings, "SITE_URL", "").rstrip("/") + url


def _email_tokens_from(brand, name, logo_url=""):
    logo_url = _email_logo(logo_url)
    tokens = tokens_for(brand, "light")
    primary = tokens["--accent"]
    return {
        "brand_name": name,
        "preset": brand["key"],
        "logo_url": logo_url,
        "font": brand["fonts"]["primary_stack"].replace('"', "'"),
        "background": tokens["--color-bg"],
        "surface": tokens["--color-surface"],
        "surface_muted": tokens["--color-surface-muted"],
        "border": _solid(tokens["--color-border"], tokens["--color-surface"], tokens["--color-text"]),
        "text": tokens["--color-text"],
        "muted": tokens["--color-text-muted"],
        "faint": tokens["--color-text-faint"],
        "primary": primary,
        "on_primary": tokens["--on-accent"],
        "primary_soft": tokens["--accent-soft"],
        "primary_text": tokens["--accent-text"],
        "secondary": tokens["--brand-navy"],
        "accent": tokens.get("--brand-accent", primary),
        "success": tokens["--color-success"],
        "success_soft": tokens["--color-success-soft"],
        "danger": tokens["--color-danger"],
        "danger_soft": tokens["--color-danger-soft"],
        "warn": tokens["--warn"],
        "warn_soft": tokens["--warn-soft"],
    }


def email_tokens(row=None):
    """Concrete colours (no CSS variables, no dark theme) for emails, invoices and PDFs, from the ACTIVE branding's
    light theme. Never raises: if the branding cannot be read (database down while a task runs) it is Branding 1."""
    try:
        ab = active_branding(row)
        return _email_tokens_from(ab.brand, ab.site_name, ab.logo.url)
    except Exception:  # noqa: BLE001 - an email must still be sendable
        return _email_tokens_from(BRANDING_1, DEFAULT_PRODUCT_NAME)


def _solid(value, surface, text):
    """An rgba() border token flattened onto the surface (emails cannot rely on alpha everywhere)."""
    m = re.match(r"rgba\((\d+), (\d+), (\d+), ([0-9.]+)\)", value)
    if not m:
        return value
    r, g, b, a = int(m[1]), int(m[2]), int(m[3]), float(m[4])
    return mix(surface, _hex((r, g, b)), a)


def brand_name():
    """The active brand name (for email subjects and other plain text)."""
    try:
        return active_branding().site_name
    except Exception:  # noqa: BLE001
        return DEFAULT_PRODUCT_NAME


def localize_product_name(text):
    """Replace the literal default product name inside a (possibly translated) sentence by the active brand name."""
    name = brand_name()
    return text if name == DEFAULT_PRODUCT_NAME else text.replace(DEFAULT_PRODUCT_NAME, name)
