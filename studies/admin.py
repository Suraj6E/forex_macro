from django.contrib import admin

from studies.models import CurrencyState, DecayCurve, EventImpact, Hypothesis, StudyRun, StudySpec


@admin.register(StudySpec)
class StudySpecAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "created_at", "parent_version")
    search_fields = ("name",)


@admin.register(StudyRun)
class StudyRunAdmin(admin.ModelAdmin):
    list_display = ("study_spec", "started_at", "finished_at", "sample", "n_observations", "engine_version")
    list_filter = ("sample", "engine_version")


@admin.register(Hypothesis)
class HypothesisAdmin(admin.ModelAdmin):
    list_display = ("question_text", "status", "study_spec", "created_at")
    list_filter = ("status",)
    search_fields = ("question_text",)


@admin.register(EventImpact)
class EventImpactAdmin(admin.ModelAdmin):
    list_display = ("event_release", "instrument", "horizon", "abnormal_ret", "n_bars", "bars_missing", "is_outlier", "engine_version")
    list_filter = ("instrument", "horizon", "window_scheme", "engine_version", "is_outlier")


@admin.register(DecayCurve)
class DecayCurveAdmin(admin.ModelAdmin):
    list_display = ("__str__", "mode", "horizon", "effect_size", "r_squared", "p_raw", "p_fdr", "n", "attribution_confidence")
    list_filter = ("mode", "instrument", "regime", "attribution_confidence")


@admin.register(CurrencyState)
class CurrencyStateAdmin(admin.ModelAdmin):
    list_display = ("currency", "ts_utc", "cum_abn_move_5d", "days_since_tier1", "rel_volatility", "regime")
    list_filter = ("currency", "regime")
    date_hierarchy = "ts_utc"
