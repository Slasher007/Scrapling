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
    "apply_frequency_per_minute": 1,
    "max_applications_per_session": 20,
    "max_search_pages": 10,
    "headless": False,
    "cookies_path": "cookies.json",
    "log_path": "applications.log",
    "applied_jobs_path": "applied_jobs.json",
    "human_delay_range": (3, 8),

    # ── Applicant profile (used to fill modal fields) ──────────────
    "profile": {
        "phone": "0600000000",          # Your phone number
        "resume_name": "",              # Partial name of resume to select; "" = pick first
        "default_text_answer": "3",     # Fallback for years-of-experience / numeric fields
        "years_of_experience": "3",
    },
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
    delay = random.uniform(*CONFIG["human_delay_range"]) + extra
    log.debug(f"Waiting {delay:.1f}s (human delay)...")
    await asyncio.sleep(delay)


def build_search_page_url(base_url: str, page_num: int) -> str:
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


def is_already_applied_text(text: str) -> bool:
    text = text.lower()
    indicators = (
        "already applied",
        "applied",
        "postulé",
        "déjà postulé",
        "candidature déjà envoy",
    )
    return any(indicator in text for indicator in indicators)


def extract_job_links(response, applied_jobs: set) -> list:
    links = []
    seen_on_page = set()

    cards = response.css("a.jcs-JobTitle, a[data-jk], .job_seen_beacon a[href*='/rc/clk']")
    for card in cards:
        href = card.attrib.get("href")
        if not href:
            continue
        job_url = normalize_job_url(href)
        data_jk = card.attrib.get("data-jk", "")
        job_id = extract_job_id(job_url, fallback=data_jk)
        card_text = card.get_all_text(strip=True) or ""
        if is_already_applied_text(card_text):
            continue
        if job_id in applied_jobs or job_id in seen_on_page:
            continue
        seen_on_page.add(job_id)
        links.append((job_id, job_url))

    return links


# ─────────────────────────────────────────────
# IFRAME UTILITIES
# ─────────────────────────────────────────────
async def get_apply_iframe(page):
    """
    Indeed's Easy Apply modal is rendered inside an <iframe>.
    Returns the frame object (Playwright Frame), or None if not found.
    """
    iframe_selectors = [
        "iframe[name='indeedapply-modal-preload-iframe']",
        "iframe[src*='indeedapply']",
        "iframe[src*='apply']",
        "iframe.ia-Iframe",
    ]
    for sel in iframe_selectors:
        try:
            locator = page.locator(sel).first
            if await locator.count() > 0:
                element = await locator.element_handle()
                if element:
                    frame = await element.content_frame()
                    if frame:
                        log.debug(f"Found apply iframe with selector: {sel}")
                        return frame
        except Exception:
            continue

    # Fallback: scan all frames by URL
    for frame in page.frames:
        if "indeedapply" in frame.url or "apply" in frame.url:
            log.debug(f"Found apply iframe by URL: {frame.url}")
            return frame

    return None


async def wait_for_apply_iframe(page, timeout: float = 15.0):
    """Poll until the apply iframe appears, then return it."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        frame = await get_apply_iframe(page)
        if frame:
            return frame
        await asyncio.sleep(0.5)
    log.warning("Apply iframe did not appear within timeout.")
    return None


# ─────────────────────────────────────────────
# FORM FIELD FILLERS
# ─────────────────────────────────────────────
async def fill_text_inputs(frame):
    """Fill empty text / textarea fields with profile data."""
    profile = CONFIG["profile"]
    try:
        inputs = await frame.query_selector_all(
            "input[type='text']:not([readonly]):not([disabled]), "
            "input[type='tel']:not([readonly]):not([disabled]), "
            "input[type='number']:not([readonly]):not([disabled]), "
            "textarea:not([readonly]):not([disabled])"
        )
        for inp in inputs:
            try:
                val = await inp.input_value()
                if val and val.strip():
                    continue  # already filled
                label_text = ""
                inp_id = await inp.get_attribute("id") or ""
                if inp_id:
                    label_el = await frame.query_selector(f"label[for='{inp_id}']")
                    if label_el:
                        label_text = (await label_el.inner_text()).lower()

                # Decide what to type
                if "phone" in label_text or "téléphone" in label_text or inp_id in ("phoneNumber", "phone"):
                    await inp.fill(profile["phone"])
                elif "year" in label_text or "année" in label_text or "experience" in label_text:
                    await inp.fill(profile["years_of_experience"])
                else:
                    await inp.fill(profile["default_text_answer"])

                await asyncio.sleep(random.uniform(0.3, 0.8))
            except Exception:
                continue
    except Exception as e:
        log.debug(f"fill_text_inputs: {e}")


async def fill_radio_buttons(frame):
    """
    Answer Yes/No radio questions — prefer 'Yes' / 'Oui'.
    Falls back to the first option if neither is found.
    """
    try:
        # Find all radio groups
        radios = await frame.query_selector_all("input[type='radio']:not([disabled])")
        groups: dict[str, list] = {}
        for r in radios:
            name = await r.get_attribute("name") or ""
            groups.setdefault(name, []).append(r)

        for name, options in groups.items():
            already_checked = False
            for r in options:
                if await r.is_checked():
                    already_checked = True
                    break
            if already_checked:
                continue

            chosen = None
            for r in options:
                val = (await r.get_attribute("value") or "").lower()
                label_for = await r.get_attribute("id") or ""
                label_text = ""
                if label_for:
                    lbl = await frame.query_selector(f"label[for='{label_for}']")
                    if lbl:
                        label_text = (await lbl.inner_text()).lower()
                if val in ("yes", "oui", "true") or label_text in ("yes", "oui"):
                    chosen = r
                    break

            if not chosen and options:
                chosen = options[0]

            if chosen:
                await chosen.click()
                await asyncio.sleep(random.uniform(0.2, 0.6))

    except Exception as e:
        log.debug(f"fill_radio_buttons: {e}")


async def fill_selects(frame):
    """Select the first non-empty option in any <select> that's still on its placeholder."""
    try:
        selects = await frame.query_selector_all("select:not([disabled])")
        for sel in selects:
            current = await sel.input_value()
            options = await sel.query_selector_all("option")
            if not options:
                continue

            # Collect non-empty option values
            valid_options = []
            for opt in options:
                val = await opt.get_attribute("value") or ""
                txt = (await opt.inner_text()).strip()
                if val and txt and txt not in ("Select...", "Sélectionner...", "--", ""):
                    valid_options.append(val)

            if not valid_options:
                continue

            # Only change if still on placeholder / default
            if current in ("", None) or not current.strip():
                await sel.select_option(value=valid_options[0])
                await asyncio.sleep(random.uniform(0.2, 0.5))

    except Exception as e:
        log.debug(f"fill_selects: {e}")


async def select_resume(frame):
    """Click the resume radio/button matching config resume_name, or pick the first one."""
    resume_name = CONFIG["profile"]["resume_name"].lower()
    try:
        # Resume cards / radio buttons
        resume_selectors = [
            "[data-testid='ResumePickerOption']",
            ".ia-ResumeSelection-option",
            "input[type='radio'][name*='resume']",
            "input[type='radio'][name*='Resume']",
        ]
        for sel in resume_selectors:
            options = await frame.query_selector_all(sel)
            if not options:
                continue
            chosen = None
            if resume_name:
                for opt in options:
                    text = (await opt.inner_text() if hasattr(opt, "inner_text") else "").lower()
                    if resume_name in text:
                        chosen = opt
                        break
            if not chosen:
                chosen = options[0]
            if chosen:
                await chosen.click()
                log.debug("Resume selected.")
                await asyncio.sleep(random.uniform(0.3, 0.8))
                return
    except Exception as e:
        log.debug(f"select_resume: {e}")


async def fill_all_fields(frame):
    """Run all field fillers in order."""
    await select_resume(frame)
    await fill_radio_buttons(frame)
    await fill_selects(frame)
    await fill_text_inputs(frame)


# ─────────────────────────────────────────────
# MODAL WALKER  (operates inside the iframe)
# ─────────────────────────────────────────────
SUCCESS_TEXTS = (
    "application submitted",
    "candidature envoy",
    "your application has been submitted",
    "application was sent",
    "candidature a été envoyée",
)

SUCCESS_SELECTORS = [
    "[data-testid='applicationSubmittedPage']",
    ".ia-Application-success",
    ".ia-ApplicationStatus--submitted",
]

CONTINUE_SELECTORS = [
    "button[data-testid='LockedContinueButton']",   # Indeed sometimes disables this one
    "button[data-testid='ContinueButton']",
    "button:has-text('Submit your application')",
    "button:has-text('Envoyer ma candidature')",
    "button:has-text('Continue')",
    "button:has-text('Continuer')",
    "button:has-text('Next')",
    "button:has-text('Suivant')",
    "button[type='submit']",
]


async def is_success(frame_or_page) -> bool:
    """Check for success indicators on a frame or page."""
    try:
        text = (await frame_or_page.content()).lower()
        if any(s in text for s in SUCCESS_TEXTS):
            return True
        for sel in SUCCESS_SELECTORS:
            if await frame_or_page.locator(sel).count() > 0:
                return True
    except Exception:
        pass
    return False


async def handle_application_modal(page) -> bool:
    """
    Walks the Easy Apply multi-step modal inside the Indeed iframe.
    Fills required fields at every step before clicking Continue/Submit.
    Returns True on success, False otherwise.
    """
    frame = await wait_for_apply_iframe(page, timeout=15.0)
    if not frame:
        log.warning("No apply iframe found — attempting to walk page-level modal.")
        ctx = page   # graceful degradation
    else:
        ctx = frame

    max_steps = 12
    for step in range(max_steps):
        await asyncio.sleep(2.0)
        log.debug(f"Modal step {step + 1}")

        # ── Check success ────────────────────────────────────────────
        if await is_success(ctx):
            log.info("Application submitted successfully (detected inside modal).")
            return True
        # Also check outer page in case modal closed
        if ctx is not page and await is_success(page):
            log.info("Application submitted successfully (detected on main page).")
            return True

        # ── Fill any visible fields first ────────────────────────────
        await fill_all_fields(ctx)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Find and click the action button ─────────────────────────
        clicked = False
        for selector in CONTINUE_SELECTORS:
            try:
                btn = ctx.locator(selector).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible():
                    continue
                is_disabled = await btn.is_disabled()
                if is_disabled:
                    # A disabled "Continue" usually means a required field is missing
                    log.debug(f"Button '{selector}' is disabled — re-filling fields.")
                    await fill_all_fields(ctx)
                    await asyncio.sleep(0.8)
                    is_disabled = await btn.is_disabled()
                    if is_disabled:
                        log.warning(f"Button still disabled after re-fill at step {step + 1}.")
                        continue
                await btn.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.8, 1.8))
                await btn.click()
                log.debug(f"Step {step + 1}: clicked '{selector}'")
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            log.warning(f"Step {step + 1}: no actionable button found — stopping modal walk.")
            break

    # Final check
    return await is_success(ctx) or await is_success(page)


# ─────────────────────────────────────────────
# CORE APPLICATION LOGIC
# ─────────────────────────────────────────────
async def attempt_easy_apply(
    session: AsyncStealthySession, job_url: str, job_id: str
) -> str:
    """
    Opens a job page and attempts Easy Apply.
    Returns: "submitted" | "already_applied" | "failed"
    """
    state = {"already_applied": False, "submitted": False}

    async def apply_action(page):
        # ── Check if already applied ─────────────────────────────────
        page_text = (await page.locator("body").first.inner_text()).lower()
        if is_already_applied_text(page_text):
            state["already_applied"] = True
            return

        # ── Find the Easy Apply button ───────────────────────────────
        easy_apply_selectors = [
            "button:has-text('Candidature simplifiée')",
            "button:has-text('Easy Apply')",
            "button:has-text('Postuler maintenant')",
            "[data-testid='indeedApplyButton']",
            ".ia-IndeedApplyButton",
            "button[data-tn-element='applyButton']",
        ]

        apply_btn = None
        for selector in easy_apply_selectors:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                apply_btn = locator
                log.info(f"Found apply button: {selector}")
                break

        if not apply_btn:
            log.warning(f"No Easy Apply button found on {job_url}")
            return

        await apply_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await apply_btn.click()

        # Wait a moment for iframe / modal to initialise
        await asyncio.sleep(3.0)

        result = await handle_application_modal(page)
        if result:
            state["submitted"] = True

    try:
        log.info(f"Opening job: {job_url}")
        response = await session.fetch(
            job_url,
            timeout=60_000,
            network_idle=True,
            load_dom=True,
            solve_cloudflare=True,
            page_action=apply_action,
        )

        if state["submitted"]:
            log.info(f"✅  Successfully applied to job {job_id}")
            return "submitted"

        # Fallback: check page text after action
        text = response.get_all_text().lower()
        if state["already_applied"] or is_already_applied_text(text):
            log.info(f"Already applied to job {job_id} — skipping.")
            return "already_applied"

        if any(s in text for s in SUCCESS_TEXTS):
            log.info(f"✅  Applied to job {job_id} (detected in page text)")
            return "submitted"

        log.warning(f"⚠️  Could not auto-complete application for {job_id}")
        return "failed"

    except Exception as e:
        log.error(f"Error applying to {job_url}: {e}")
        return "failed"


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

    cookies_path = CONFIG["cookies_path"]
    if not os.path.exists(cookies_path):
        log.error(f"cookies.json not found at {cookies_path}.")
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
                timeout=60_000,
                load_dom=True,
                network_idle=True,
                solve_cloudflare=True,
                wait_selector="a.jcs-JobTitle, a[data-jk]",
            )
            await human_delay()

            job_links = extract_job_links(response, applied_jobs)
            log.info(f"Found {len(job_links)} new job(s) on this page.")

            for job_id, job_url in job_links:
                if applications_this_session >= CONFIG["max_applications_per_session"]:
                    log.info("Session cap reached. Stopping.")
                    break

                if applications_this_session > 0:
                    wait = interval_seconds + random.uniform(-5, 5)
                    wait = max(wait, 10)
                    log.info(f"Rate-limit pause: {wait:.1f}s ...")
                    await asyncio.sleep(wait)

                result = await attempt_easy_apply(session, job_url, job_id)

                if result == "submitted":
                    save_applied_job(job_id, applied_jobs)
                    applications_this_session += 1
                    log.info(
                        f"Progress: {applications_this_session}/"
                        f"{CONFIG['max_applications_per_session']}"
                    )
                elif result == "already_applied":
                    save_applied_job(job_id, applied_jobs)

            page_num += 1

    log.info("=" * 60)
    log.info(f"Session complete. Applied to {applications_this_session} job(s).")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_bot())