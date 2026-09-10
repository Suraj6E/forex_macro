"""Commentary — planning.md §9, locked decision 10.

Data and commentary are separated *in the data model*, not by convention.
Commentary never lives on a data table; it points at one.  A row whose basis
is `generated` can never render inside a data panel (§9 note).
"""

from django.db import models

from quality.enums import EpistemicStatus


class Basis(models.TextChoices):
    LITERATURE = "literature", "literature — cited, peer-reviewed"
    COMPUTED = "computed", "computed — derived from our own measurements"
    GENERATED = "generated", "generated — model-written, labelled as such"
    USER_NOTE = "user_note", "user note"


class ScopeType(models.TextChoices):
    INDICATOR = "indicator", "indicator"
    RELEASE_GROUP = "release_group", "release group"
    EVENT_RELEASE = "event_release", "event release"
    INSTRUMENT = "instrument", "instrument"
    STUDY_RUN = "study_run", "study run"
    MARKET_EVENT = "market_event", "market event"
    GENERAL = "general", "general"


class Commentary(models.Model):
    scope_type = models.CharField(max_length=20, choices=ScopeType.choices)
    scope_id = models.IntegerField(null=True, blank=True)
    body_md = models.TextField()

    basis = models.CharField(max_length=16, choices=Basis.choices)
    epistemic_status = models.CharField(max_length=16, choices=EpistemicStatus.choices)

    citation = models.TextField(blank=True)
    author = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "commentary"
        indexes = [models.Index(fields=["scope_type", "scope_id"])]

    def __str__(self):
        return f"[{self.epistemic_status}] {self.body_md[:60]}"

    @property
    def may_render_in_data_panel(self) -> bool:
        """Structural enforcement of locked decision 10."""
        return self.basis != Basis.GENERATED
