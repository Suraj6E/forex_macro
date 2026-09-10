from django.contrib import admin

from commentary.models import Commentary


@admin.register(Commentary)
class CommentaryAdmin(admin.ModelAdmin):
    list_display = ("__str__", "scope_type", "scope_id", "basis", "epistemic_status", "author", "created_at")
    list_filter = ("basis", "epistemic_status", "scope_type")
    search_fields = ("body_md", "citation")
