from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        import core.signals  # noqa: F401
        from core.admin import register_unregistered_models

        register_unregistered_models()
