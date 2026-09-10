from django.apps import AppConfig


class SourcesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sources"
    verbose_name = "Sources & ingestion"

    def ready(self):
        # Importing the modules is what registers their @jobs.handler entries.
        from sources import ingest  # noqa: F401
        from sources import jobs

        if jobs.should_start_worker():
            jobs.start_worker()
