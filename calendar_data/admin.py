"""Admin is a working surface here, not decoration.

§8: "Event metadata is small, highly relational, and benefits from Django
admin for manual inspection and correction — which you'll need for the
canonical-code mapping."
"""

from django.contrib import admin

from calendar_data.models import (
    EventRelease,
    Indicator,
    IndicatorAlias,
    ReleaseGroup,
    SourceObservation,
    ValueRevision,
)


class IndicatorAliasInline(admin.TabularInline):
    model = IndicatorAlias
    extra = 0
    fields = ("source", "source_key", "source_name", "currency", "times_seen")
    readonly_fields = ("source", "source_key", "source_name", "currency", "times_seen")
    can_delete = False


@admin.register(Indicator)
class IndicatorAdmin(admin.ModelAdmin):
    list_display = ("__str__", "currency", "name", "canonical_code", "mapping_status", "importance", "release_group")
    list_filter = ("mapping_status", "currency", "importance")
    list_editable = ("canonical_code", "mapping_status", "importance", "release_group")
    search_fields = ("name", "canonical_code")
    inlines = [IndicatorAliasInline]


@admin.register(IndicatorAlias)
class IndicatorAliasAdmin(admin.ModelAdmin):
    list_display = ("source", "currency", "source_name", "indicator", "times_seen", "first_seen_at")
    list_filter = ("source", "currency")
    search_fields = ("source_key", "source_name")
    autocomplete_fields = ("indicator",)


@admin.register(ReleaseGroup)
class ReleaseGroupAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "currency", "country")
    search_fields = ("key", "name")


class SourceObservationInline(admin.TabularInline):
    model = SourceObservation
    extra = 0
    readonly_fields = ("source", "fetch_run", "observed_at", "raw_json")
    can_delete = False


class ValueRevisionInline(admin.TabularInline):
    model = ValueRevision
    extra = 0
    readonly_fields = ("observed_at", "field", "old_value", "new_value", "source", "fetch_run")
    can_delete = False


@admin.register(EventRelease)
class EventReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "release_time_utc",
        "scheduled_time_utc",
        "trading_day",
        "cross_source",
        "forecast_provenance",
        "vol_check",
    )
    list_filter = ("cross_source", "forecast_provenance", "actual_provenance", "vol_check", "timestamp_confidence")
    date_hierarchy = "scheduled_time_utc"
    search_fields = ("indicator__name", "indicator__canonical_code", "reference_period")
    autocomplete_fields = ("indicator",)
    inlines = [SourceObservationInline, ValueRevisionInline]
    readonly_fields = ("source_map_json", "first_seen_at", "last_seen_at", "trading_day")


@admin.register(ValueRevision)
class ValueRevisionAdmin(admin.ModelAdmin):
    """Append-only by design — read here, never edited."""

    list_display = ("observed_at", "event_release", "field", "old_value", "new_value", "source")
    list_filter = ("field", "source")
    readonly_fields = tuple(f.name for f in ValueRevision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
