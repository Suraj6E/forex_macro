from django.contrib import admin

from sources.models import FetchRun, Job, RawSnapshot, Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "kind", "enabled", "health", "last_fetch_at", "consecutive_failures")
    list_filter = ("kind", "health", "enabled", "robots_status")
    search_fields = ("key", "name", "base_url")
    fieldsets = (
        (None, {"fields": ("key", "name", "kind", "base_url", "enabled", "supports_date_range")}),
        ("What it gives (§5)", {"fields": ("gives", "depth_note", "role")}),
        ("Clock (§4.1)", {"fields": ("timezone_rule",)}),
        ("Policy (§5.4)", {"fields": ("fetch_policy_json", "terms_note", "robots_status")}),
        ("Health", {"fields": ("last_fetch_at", "last_success_at", "consecutive_failures", "health")}),
    )


class RawSnapshotInline(admin.TabularInline):
    model = RawSnapshot
    extra = 0
    readonly_fields = ("fetched_at", "url", "path", "sha256", "size_bytes", "http_status", "duration_ms")
    can_delete = False


@admin.register(FetchRun)
class FetchRunAdmin(admin.ModelAdmin):
    list_display = ("id", "source", "started_at", "status", "rows_seen", "rows_new", "rows_changed", "rows_unmapped")
    list_filter = ("source", "status")
    date_hierarchy = "started_at"
    inlines = [RawSnapshotInline]
    readonly_fields = tuple(f.name for f in FetchRun._meta.fields)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "progress_percent", "message", "created_at", "duration_seconds")
    list_filter = ("kind", "status")
    readonly_fields = ("created_at", "started_at", "finished_at", "log", "result_json", "error_text")
