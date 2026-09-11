from django.db import models

from governance.models import Plan


class RegionalPrice(models.Model):
    """What a client organization (a Department, see accounts.Department)
    actually pays for a Plan in one region - set exactly by a SuperAdmin,
    never auto-converted from another region's price. `region_code` is
    always one of billing.regions.ALL_REGIONS, enforced at the form layer
    (not a DB-level choices= constraint, so a region can be added to the
    registry without a migration).

    extra_team_price lives on this same row (not a separate per-team
    model) because it's denominated in the same region/currency as
    `price` - a department billed in PKR pays its extra-team fee in PKR
    too. Null means "no extra-team charge configured for this region" -
    an over-the-included-count department simply isn't charged for it
    yet, rather than the invoice sweep guessing a number.
    """

    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="regional_prices")
    region_code = models.CharField(max_length=10)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    extra_team_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plan", "region_code"], name="unique_plan_region_price"),
        ]
        ordering = ["plan_id", "region_code"]

    def __str__(self):
        return f"{self.plan.name} / {self.region_code}: {self.price}"
