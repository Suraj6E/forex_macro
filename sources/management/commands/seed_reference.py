"""CLI wrapper around sources.seeding.seed_reference.

The console button and this command run the same function — see
`sources/seeding.py`.
"""

from django.core.management.base import BaseCommand

from sources.seeding import seed_reference


class Command(BaseCommand):
    help = "Seed sources, instruments, release groups and known market dislocations."

    def handle(self, *args, **options):
        result = seed_reference(log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(result["summary"]))
