"""Amazon Ads placement search term report automation.

Playwright runs the saved ``placment_search_term_Automation`` report on the
Amazon Ads reporting/history page (advertising.amazon.com,
ENTITY1YST5GXMAIR36) every hour, waits for it to finish, downloads the CSV,
validates it with pandas, and upserts it into ``itrend.search_term_placement_data``
on a UNIQUE row key, so no row is ever stored twice.

Built from Amazon_ad_search_term_automation_cron: the browser session, sign-in,
selector, retry, lock and logging modules are reused from it.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
