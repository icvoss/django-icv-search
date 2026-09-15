"""Settings used only by the CI migration-state gate.

The normal test settings disable package migrations so pytest can create the
test app's unmigrated models. This module keeps the real icv_search migration
graph enabled for ``makemigrations --check``.
"""

from settings import *  # noqa: F403

MIGRATION_MODULES = {  # noqa: F405
    app_label: module
    for app_label, module in MIGRATION_MODULES.items()  # noqa: F405
    if app_label != "icv_search"
}
