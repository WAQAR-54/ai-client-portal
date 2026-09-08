from governance.models import SiteBranding


def branding(request):
    """Makes `site_branding` available in every template without each view
    passing it explicitly - see SiteBranding's docstring."""
    return {"site_branding": SiteBranding.load()}
