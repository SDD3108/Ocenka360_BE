from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Category, FormRevision, Service
from apps.catalog.seed_data import CATEGORIES, SERVICES, SUMMARIES, definition_for
from apps.catalog.services import publish_form


class Command(BaseCommand):
    help = "Create the starting catalog without overwriting any existing categories/services/forms."

    @transaction.atomic
    def handle(self, *args, **options):
        categories = {}
        for i, (slug, title) in enumerate(CATEGORIES):
            categories[slug], _ = Category.objects.get_or_create(
                slug=slug, defaults={"title": title, "sort_order": i}
            )
        created_count = 0
        for i, (slug, category, title, template) in enumerate(SERVICES):
            service, created = Service.objects.get_or_create(
                slug=slug,
                defaults={
                    "category": categories[category],
                    "title": title,
                    "summary": SUMMARIES[template],
                    "sort_order": i,
                },
            )
            if not created:
                continue
            form = FormRevision.objects.create(
                service=service, version=1, definition=definition_for(template)
            )
            publish_form(form.pk)
            created_count += 1
        self.stdout.write(
            self.style.SUCCESS(f"Created {created_count} services. Existing records were not overwritten.")
        )
        self.stdout.write("Document lists are preliminary. Review them with the appraiser before public use.")
