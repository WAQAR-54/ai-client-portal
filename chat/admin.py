from django.contrib import admin

from chat.models import Conversation, Message, ModelConfig, UserModelPermission


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = ["model_name", "provider", "tier", "input_cost_per_1m", "output_cost_per_1m", "is_enabled"]
    list_filter = ["provider", "tier", "is_enabled"]
    list_editable = ["is_enabled"]
    search_fields = ["model_name"]


@admin.register(UserModelPermission)
class UserModelPermissionAdmin(admin.ModelAdmin):
    list_display = ["user", "model_config", "is_allowed"]
    list_filter = ["is_allowed", "model_config"]
    search_fields = ["user__email"]


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ["role", "content", "model_used", "input_tokens", "output_tokens", "estimated_cost", "created_at"]
    can_delete = False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "created_at"]
    search_fields = ["title", "user__email"]
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = [
        "conversation", "role", "model_used", "input_tokens", "output_tokens",
        "estimated_cost", "attachment_original_name", "created_at",
    ]
    list_filter = ["role", "model_used"]
