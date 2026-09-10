from django.contrib import admin

from prices.models import Instrument, MarketEvent, PriceCoverage


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "base_ccy", "quote_ccy", "pip_size", "enabled")
    search_fields = ("symbol",)


@admin.register(PriceCoverage)
class PriceCoverageAdmin(admin.ModelAdmin):
    list_display = ("instrument", "source", "month", "bar_count", "expected_bar_count", "gap_count", "updated_at")
    list_filter = ("instrument", "source")
    date_hierarchy = "month"


@admin.register(MarketEvent)
class MarketEventAdmin(admin.ModelAdmin):
    """§4.6 — so the UI can annotate known dislocations rather than have you
    rediscover them as inexplicable dots on a scatter plot."""

    list_display = ("ts_utc", "label", "kind", "regime")
    list_filter = ("kind", "regime")
    filter_horizontal = ("instruments",)
    date_hierarchy = "ts_utc"
