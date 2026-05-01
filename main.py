import asyncio
import json
import logging
import os
import random
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from scrapling.fetchers import AsyncStealthySession

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
CONFIG = {
    "search_url": "https://www.indeed.com/",
    "apply_frequency_per_minute": 1,       # Max applications per minute
    "max_applications_per_session": 20,    # Safety cap per run
    "max_search_pages": 10,                # Safety cap for paginated result pages
    "headless": False,                     # Set True for background mode
    "cookies_path": "cookies.json",
    "log_path": "applications.log",
    "applied_jobs_path": "applied_jobs.json",  # Tracks already-applied jobs
    "human_delay_range": (3, 8),           # Random delay (seconds) between actions
}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_path"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def load_applied_jobs() -> set:
    if os.path.exists(CONFIG["applied_jobs_path"]):
        with open(CONFIG["applied_jobs_path"], "r") as f:
            return set(json.load(f))
    return set()


def save_applied_job(job_id: str, applied: set):
    applied.add(job_id)
    with open(CONFIG["applied_jobs_path"], "w") as f:
        json.dump(list(applied), f, indent=2)


async def human_delay(extra: float = 0):
    """Mimics human thinking time with random delay."""
    delay = random.uniform(*CONFIG["human_delay_range"]) + extra
    log.debug(f"Waiting {delay:.1f}s (human delay)...")
    await asyncio.sleep(delay)


def build_search_page_url(base_url: str, page_num: int) -> str:
    """Build Indeed page URL using the `start` query param."""
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["start"] = [str((page_num - 1) * 10)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def normalize_job_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"https://www.indeed.com{href}"


def extract_job_id(job_url: str, fallback: str = "") -> str:
    parsed = urlparse(job_url)
    jk = parse_qs(parsed.query).get("jk", [""])[0]
    return jk or fallback or job_url


def extract_job_links(response, applied_jobs: set[str]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen_on_page: set[str] = set()

    cards = response.css("a.jcs-JobTitle, a[data-jk], .job_seen_beacon a[href*='/rc/clk']")
    for card in cards:
        href = card.attrib.get("href")
        if not href:
            continue
        job_url = normalize_job_url(href)
        data_jk = card.attrib.get("data-jk", "")
        job_id = extract_job_id(job_url, fallback=data_jk)
        if job_id in applied_jobs or job_id in seen_on_page:
            continue
        seen_on_page.add(job_id)
        links.append((job_id, job_url))

    return links


# ─────────────────────────────────────────────
# CORE APPLICATION LOGIC
# ─────────────────────────────────────────────
async def attempt_easy_apply(session: AsyncStealthySession, job_url: str, job_id: str) -> bool:
    """
    Opens a job page and tries to click the Easy Apply button.
    Returns True if application was submitted successfully.
    """
    async def apply_action(page):
        # Indeed uses several selectors depending on locale/version
        easy_apply_selectors = [
            "button:has-text('Candidature simplifiée')",
            "button:has-text('Easy Apply')",
            "button:has-text('Postuler maintenant')",
            "[data-testid='indeedApplyButton']",
            ".ia-IndeedApplyButton",
        ]

        apply_btn = None
        for selector in easy_apply_selectors:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                apply_btn = locator
                log.info(f"Found apply button with selector: {selector}")
                break

        if not apply_btn:
            return

        await apply_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await apply_btn.click()
        await asyncio.sleep(2.0)
        await handle_application_modal(page)

    try:
        log.info(f"Opening job: {job_url}")
        response = await session.fetch(
            job_url,
            timeout=60000,
            network_idle=True,
            load_dom=True,
            solve_cloudflare=True,
            page_action=apply_action,
        )

        text = response.get_all_text().lower()
        success_indicators = (
            "application submitted",
            "candidature envoy",
            "your application has been submitted",
        )
        submitted = any(indicator in text for indicator in success_indicators)

        if submitted:
            log.info(f"Successfully applied to job {job_id}")
        else:
            log.warning(f"Application modal could not be auto-completed for {job_id}")

        return submitted
    except Exception as e:
        log.error(f"Error applying to {job_url}: {e}")
        return False


async def handle_application_modal(page) -> bool:
    """
    Handles the Indeed Easy Apply modal.
    Walks through steps clicking 'Continue' / 'Submit'.
    """
    max_steps = 10
    for step in range(max_steps):
        await asyncio.sleep(2.0)

        # Check for success
        success_indicators = [
            "text=Application submitted",
            "text=Candidature envoyée",
            "text=Your application has been submitted",
            "[data-testid='applicationSubmittedPage']",
        ]
        for indicator in success_indicators:
            try:
                if await page.locator(indicator).count() > 0:
                    return True
            except Exception:
                pass

        # Try "Continue" or "Submit" buttons
        action_selectors = [
            "button:has-text('Submit your application')",
            "button:has-text('Envoyer ma candidature')",
            "button:has-text('Continue')",
            "button:has-text('Continuer')",
            "button:has-text('Next')",
            "button:has-text('Suivant')",
            "button[type='submit']",
        ]

        clicked = False
        for selector in action_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    await btn.click()
                    log.debug(f"Step {step+1}: clicked '{selector}'")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            log.warning(f"Step {step+1}: No actionable button found — stopping modal walk.")
            break

    return False


# ─────────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────────
async def run_bot():
    applied_jobs = load_applied_jobs()
    applications_this_session = 0
    interval_seconds = 60 / CONFIG["apply_frequency_per_minute"]

    log.info("=" * 60)
    log.info("Indeed Easy Apply Bot — Starting")
    log.info(f"  Frequency  : {CONFIG['apply_frequency_per_minute']} application(s)/min")
    log.info(f"  Session cap: {CONFIG['max_applications_per_session']}")
    log.info(f"  Already applied to {len(applied_jobs)} jobs (will skip these)")
    log.info("=" * 60)

    # ── Load session cookies ─────────────────────────────────────────
    cookies_path = CONFIG["cookies_path"]
    if not os.path.exists(cookies_path):
        log.error(f"cookies.json not found at {cookies_path}. Please export your session first.")
        return

    with open(cookies_path, "r") as f:
        cookies = json.load(f)

    async with AsyncStealthySession(
        headless=CONFIG["headless"],
        cookies=cookies,
        solve_cloudflare=True,
        block_webrtc=True,
        hide_canvas=True,
        disable_resources=False,
    ) as session:
        page_num = 1
        while (
            applications_this_session < CONFIG["max_applications_per_session"]
            and page_num <= CONFIG["max_search_pages"]
        ):
            search_page_url = build_search_page_url(CONFIG["search_url"], page_num)
            log.info(f"Scraping results page {page_num}: {search_page_url}")

            response = await session.fetch(
                search_page_url,
                timeout=60000,
                load_dom=True,
                network_idle=True,
                solve_cloudflare=True,
                wait_selector="a.jcs-JobTitle, a[data-jk]",
            )
            await human_delay()

            job_links = extract_job_links(response, applied_jobs)
            log.info(f"Found {len(job_links)} new job(s) on this page.")
            if not job_links:
                log.info("No new jobs found on this page.")

            for job_id, job_url in job_links:
                if applications_this_session >= CONFIG["max_applications_per_session"]:
                    log.info("Session cap reached. Stopping.")
                    break

                if applications_this_session > 0:
                    wait = interval_seconds + random.uniform(-5, 5)
                    wait = max(wait, 10)
                    log.info(f"Rate-limit pause: {wait:.1f}s before next application...")
                    await asyncio.sleep(wait)

                success = await attempt_easy_apply(session, job_url, job_id)
                if success:
                    save_applied_job(job_id, applied_jobs)
                    applications_this_session += 1
                    log.info(
                        f"Progress: {applications_this_session}/{CONFIG['max_applications_per_session']} "
                        f"applied this session"
                    )

            page_num += 1

    log.info("=" * 60)
    log.info(f"Session complete. Applied to {applications_this_session} job(s).")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_bot())