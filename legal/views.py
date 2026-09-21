from django.conf import settings
from django.views.generic import TemplateView

from legal import policy


class PolicyView(TemplateView):
    """A public (no sign-in) legal page. The wording lives in templates/legal/<key>.html."""

    key = ""

    def get_template_names(self):
        return [f"legal/{self.key}.html"]

    def get_context_data(self, **kwargs):
        title = next(page[3] for page in policy.PAGES if page[0] == self.key)
        return super().get_context_data(**kwargs) | {
            "policy": {"key": self.key, "title": title, "version": policy.VERSION, "last_updated": policy.LAST_UPDATED},
            "policy_pages": [{"key": k, "url_name": u, "label": label} for k, u, label, _t in policy.PAGES],
            "providers": policy.PROVIDERS,
            "support_email": getattr(settings, "SUPPORT_EMAIL", ""),
        }
