"""Database package for Foodpanda scraper storage.

Named `scraperdb`, not `db`, because the workspace root has its own `db`
package (the agent's read-only query layer). Two top-level packages with the
same name resolve to whichever is first on sys.path, which made importing both
in one process — e.g. any test that touches the agent and the scraper — depend
on import order.
"""

from scraperdb.database import (
    clear_all_data,
    count_menu_items,
    get_connection,
    init_db,
    insert_restaurant_with_menu,
    restaurant_exists,
)

__all__ = [
    "clear_all_data",
    "count_menu_items",
    "get_connection",
    "init_db",
    "insert_restaurant_with_menu",
    "restaurant_exists",
]
