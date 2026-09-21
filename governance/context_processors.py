from governance.branding import active_branding
from governance.models import SiteBranding


def branding(request):
    """Makes `site_branding` available in every template without each view passing it explicitly - see
    SiteBranding's docstring. It is the ACTIVE branding (governance/branding.py): the same attribute names the saved
    row has (site_name, tagline, logo, favicon) resolved through the selected Branding 1 / 2 / 3 / Custom, plus the
    generated CSS, the fonts URL and the dark logo."""
    return {"site_branding": active_branding(SiteBranding.load())}
