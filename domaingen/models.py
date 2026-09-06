from django.conf import settings
from django.db import models


class DomainSearch(models.Model):
    """One "Generate" click in the Domain Generator - logs real usage (the
    daily quota and the admin Dashboard's stats are both backed by this,
    same pattern as playground.models.PlaygroundRun) plus the real AI
    cost/token accounting for the generation call, since unlike Code
    Playground's simulated execution, this feature's AI step is a genuine
    provider call."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="domain_searches")
    query = models.CharField(max_length=300)
    provider_model = models.ForeignKey(
        "providers.ProviderModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="domain_searches"
    )
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user}: {self.query!r} at {self.created_at}"
