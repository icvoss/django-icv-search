from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class IcvSearchConfig(AppConfig):
    name = "icv_search"
    label = "icv_search"
    verbose_name = _("ICV Search")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.core.checks import register

        from . import (
            checks,  # noqa: F401  # register system checks
            handlers,  # noqa: F401  # connect signal handlers
            merchandising_handlers,  # noqa: F401  # merchandising cache invalidation
        )
        from .auto_index import connect_auto_index_signals
        from .checks import check_base_model_is_abstract

        register(check_base_model_is_abstract)
        connect_auto_index_signals()

        # Eagerly validate the query preprocessor setting (BR-026)
        from icv_search.services.preprocessing import load_preprocessor

        load_preprocessor()
