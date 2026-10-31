from config import current_settings, GroupFiles
from scraper import scrape_posts
from utils import backfill_post_urls, log_message

if __name__ == "__main__":
    log_message("=== Scraper Started ===")
    for url in current_settings()["GROUPS"]:
        n = backfill_post_urls(GroupFiles(url))
        if n:
            log_message(f"Filled post_url for {n} saved posts in {GroupFiles(url).slug}")
    scrape_posts()
    log_message("=== Scraper Ended ===")
