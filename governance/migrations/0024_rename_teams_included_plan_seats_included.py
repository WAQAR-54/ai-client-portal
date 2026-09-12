from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("governance", "0023_plan_teams_included"),
    ]

    operations = [
        migrations.RenameField(
            model_name="plan",
            old_name="teams_included",
            new_name="seats_included",
        ),
        migrations.AlterField(
            model_name="plan",
            name="seats_included",
            field=models.PositiveIntegerField(
                blank=True,
                default=1,
                help_text="Seats (users) a department gets before being billed per extra person. Blank = unlimited.",
                null=True,
            ),
        ),
    ]
