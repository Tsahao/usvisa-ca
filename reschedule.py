import random
import re
import shutil
import time
import traceback
from datetime import datetime
from time import sleep
from typing import Union, List

import requests
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from legacy.gmail import GMail, Message
from legacy_rescheduler import (
    legacy_reschedule,
    UnverifiedReschedule,
    _ensure_all_applicants_selected_and_continue,
    _ensure_warning_acknowledged,
    _find_continue_button,
    _select_configured_consulate,
)
from request_tracker import RequestTracker
from settings import *


def log_message(message: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")


def _send_gmail_notification(subject: str, body: str) -> None:
    """Send a Gmail notification, or log-only in TEST_MODE.

    TEST_MODE must never spam real recipients while exercising the
    verification flow, so all notification sends funnel through here.
    Live behavior is unchanged; send failures are logged, never fatal.
    """
    if TEST_MODE:
        log_message(f"[TEST_MODE] Would send email '{subject}': {body}")
        return
    try:
        gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
        msg = Message(
            subject,
            to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
            text=body,
        )
        gmail.send(msg)
        gmail.close()
    except Exception as e:
        log_message(f"Email notification failed (continuing): {e}")


def verify_booking_after_unverified(previous_date, target_date) -> str:
    """Re-check the dashboard with a fresh login. Read-only, never books.

    Returns "success" (dashboard == target), "failure" (repeated reads ==
    previous), or "unknown" (anything ambiguous: None, third date, login
    failure, missing target). Callers must treat "unknown" as manual-check
    + quit, never as failure. Date-only comparison per configuration.
    """
    if target_date is None:
        log_message("Verification skipped: no target date carried by UnverifiedReschedule")
        return "unknown"
    max_reads = VERIFY_MAX_READS if VERIFY_MAX_READS > 0 else 3
    read_delay = VERIFY_READ_DELAY if VERIFY_READ_DELAY >= 0 else 10
    driver, user_data_dir = get_chrome_driver()
    try:
        try:
            login(driver)
        except Exception as e:
            log_message(f"Verification login failed: {e}")
            return "unknown"
        readings = []
        for attempt in range(max_reads):
            verified = None
            try:
                try:
                    WebDriverWait(driver, TIMEOUT).until(_dashboard_ready)
                except TimeoutException:
                    pass
                verified = get_current_appointment_date(driver)
            except Exception as e:
                log_message(f"Verification read {attempt + 1}/{max_reads} errored: {e}")
                verified = None
            readings.append(verified)
            log_message(
                f"Verification read {attempt + 1}/{max_reads}: dashboard shows "
                f"{verified}, expected {target_date}, previous {previous_date}"
            )
            if verified is not None and verified == target_date:
                return "success"
            if attempt < max_reads - 1:
                sleep(read_delay)
                try:
                    driver.refresh()
                except Exception:
                    pass
        # No read matched the target. Only a confident "still old" counts
        # as failure; everything else is unknown (fail-safe: quit).
        if previous_date is not None and all(
            r is not None and r == previous_date for r in readings
        ):
            return "failure"
        return "unknown"
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def jittered_delay(base_delay: float) -> float:
    return base_delay + random.uniform(0, DATE_REQUEST_JITTER)


class SoftBanDetected(Exception):
    pass


class SessionExpired(Exception):
    """Login cookies dead (redirect to sign_in / 401/403 / HTML login page).

    Non-retryable on the same driver -- caller must quit and start a fresh
    session immediately instead of burning retry budget.
    """
    pass


# Consecutive appointment-page setup failures across driver sessions.
# NEW_SESSION_AFTER_FAILURES=5 per session, so 15 == ~3 fully failed sessions.
SETUP_FAILURE_SOFT_BAN_THRESHOLD = 15
_consecutive_setup_failures = 0

def get_chrome_driver() -> tuple:
    options = webdriver.ChromeOptions()
    if not SHOW_GUI:
        options.add_argument("headless")
        options.add_argument("window-size=1920x1080")
        options.add_argument("disable-gpu")
        options.add_argument('user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36')
    options.add_experimental_option("detach", DETACH)
    options.add_argument('--incognito')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    user_data_dir = f'/tmp/chrome-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{random.randint(1000, 9999)}'
    options.add_argument(f'--user-data-dir={user_data_dir}')
    try:
        driver = webdriver.Chrome(options=options)
    except BaseException:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise
    return driver, user_data_dir


def login(driver: WebDriver) -> None:
    driver.get(LOGIN_URL)
    timeout = TIMEOUT

    email_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_email"))
    )
    email_input.send_keys(USER_EMAIL)

    password_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_password"))
    )
    password_input.send_keys(USER_PASSWORD)

    policy_checkbox = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
    )
    policy_checkbox.click()

    login_button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.NAME, "commit"))
    )
    login_button.click()


def parse_consular_appointment_date(text: str):
    """Parse the booked date out of a consular-appt block's text.

    Handles "14 January, 2027" (site format from p.consular-appt) plus
    "January 14, 2027" and ISO "2027-01-14" variants. Returns a date or
    None when no recognizable date is present. Pure function (no driver)
    so it is unit-testable.
    """
    if not text:
        return None
    match = re.search(r"(\d{1,2})\s+([A-Za-z]+),\s+(\d{4})", text)
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}",
                "%d %B %Y",
            ).date()
        except ValueError:
            pass
    match = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}",
                "%B %d %Y",
            ).date()
        except ValueError:
            pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def get_current_appointment_date(driver: WebDriver):
    """Read the currently booked date from the dashboard.

    Looks for <p class="consular-appt">Consular Appointment: 14 January,
    2027, ...</p>. Returns None when the block is absent (paid but never
    booked) or unparseable. Must be called while on the post-login
    dashboard, before navigating to the appointment page. When an IVR is
    configured, only read its group; a missing/ambiguous group must not
    be mistaken for a first booking or another group's appointment.
    """
    scope = _get_dashboard_scope(driver)
    try:
        elements = scope.find_elements(By.CSS_SELECTOR, "p.consular-appt")
    except Exception:
        return None
    for element in elements:
        try:
            parsed = parse_consular_appointment_date(element.text)
        except Exception:
            continue
        if parsed is not None:
            return parsed
    return None


def _find_visible_action(driver: WebDriver, label: str):
    locators = (
        (By.LINK_TEXT, label),
        (By.XPATH, f".//button[normalize-space()='{label}']"),
        (By.XPATH, f".//input[@value='{label}']"),
    )
    for locator in locators:
        try:
            elements = driver.find_elements(*locator)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                # Page re-rendered mid-read (stale element) -- treat as
                # not visible rather than crashing setup.
                continue
    return False


_IVR_ACCOUNT_PATTERN = re.compile(
    r"\bIVR\s+Account\s+Number\s*:\s*([0-9]+)\b", re.IGNORECASE
)
_IVR_LABEL_XPATH = (
    "//*[contains(normalize-space(.), 'IVR Account Number')"
    " and not(.//*[contains(normalize-space(.), 'IVR Account Number')])]"
)
_GROUP_ANCESTOR_XPATH = (
    "ancestor::*[.//a[normalize-space()='Continue']"
    " or .//button[normalize-space()='Continue']"
    " or .//input[@value='Continue']][1]"
)


def _find_dashboard_group(
    driver: WebDriver, ivr_account_number=None, *, single_group_fallback=False
):
    """Find one IVR-labelled card, never a container spanning other groups.

    Start at the smallest IVR label node and find its nearest ancestor
    containing Continue. This avoids depending on the site's card classes.
    Only an explicitly blank selector with single_group_fallback enabled
    may select the sole visible group; paid callers keep their old behavior.
    """
    selected_ivr = (
        PAID_IVR_ACCOUNT_NUMBER if ivr_account_number is None else ivr_account_number
    )
    auto_select = single_group_fallback and ivr_account_number == ""
    groups = []
    for label in driver.find_elements(By.XPATH, _IVR_LABEL_XPATH):
        try:
            if not label.is_displayed():
                continue
            ancestors = label.find_elements(By.XPATH, _GROUP_ANCESTOR_XPATH)
            if not ancestors:
                if auto_select:
                    raise RuntimeError("Cannot identify every visible dashboard group")
                continue
            group = ancestors[0]
            group_ivrs = _IVR_ACCOUNT_PATTERN.findall(group.text)
            if auto_select:
                if len(group_ivrs) != 1:
                    raise RuntimeError("Dashboard group has an ambiguous IVR identity")
            elif group_ivrs != [selected_ivr]:
                continue
            if group not in groups:
                groups.append(group)
        except WebDriverException:
            # Re-rendered dashboard: wait for a fresh lookup, not another group.
            if auto_select:
                return False
            continue
    if len(groups) > 1:
        if auto_select:
            raise RuntimeError(
                "Multiple dashboard groups; set UNPAID_IVR_ACCOUNT_NUMBER"
            )
        raise RuntimeError(
            "Multiple dashboard groups match the configured IVR; "
            "refusing to select a group"
        )
    return groups[0] if groups else False


def _get_dashboard_scope(driver: WebDriver, ivr_account_number=None):
    selected_ivr = (
        PAID_IVR_ACCOUNT_NUMBER if ivr_account_number is None else ivr_account_number
    )
    if not selected_ivr:
        return driver
    try:
        return WebDriverWait(driver, TIMEOUT).until(
            lambda current_driver: _find_dashboard_group(current_driver, selected_ivr)
        )
    except TimeoutException as exc:
        raise TimeoutException(
            "Could not find a unique dashboard group for the configured IVR; "
            "refusing to use another group's Continue or appointment"
        ) from exc


def _dashboard_ready(driver: WebDriver):
    if PAID_IVR_ACCOUNT_NUMBER:
        return _find_dashboard_group(driver)
    return (
        driver.find_elements(By.CSS_SELECTOR, "p.consular-appt")
        or _find_visible_action(driver, "Schedule Appointment")
        or _find_visible_action(driver, "Continue")
    )


def _click_action_if_present(
    driver: WebDriver, label: str, timeout: float
) -> bool:
    try:
        action = WebDriverWait(driver, timeout).until(
            lambda current_driver: _find_visible_action(current_driver, label)
        )
    except TimeoutException:
        return False
    action.click()
    sleep(2)
    return True


def _has_visible_element(driver: WebDriver, locator) -> bool:
    try:
        elements = driver.find_elements(*locator)
    except Exception:
        return False
    for element in elements:
        try:
            if element.is_displayed():
                return True
        except Exception:
            # Page re-rendered mid-read (stale element) -- ignore it.
            continue
    return False


def _appointment_page_state(driver: WebDriver):
    if _has_visible_element(
        driver, (By.ID, "appointments_consulate_appointment_date_input")
    ):
        return "ready"
    # Group flow: applicant-selection page shows one checkbox per
    # participant plus a Continue submit. This MUST be checked before the
    # generic policy-checkbox branch below, because the applicant page
    # also uses .icheckbox-styled checkboxes. Treating it as the policy
    # page would click the first participant checkbox and deselect them.
    if _find_continue_button(driver) is not None:
        checkboxes = driver.find_elements(
            By.XPATH, "//main[@id='main']//form//input[@type='checkbox']"
        )
        if checkboxes:
            return "applicants"
    if _find_visible_action(driver, "Schedule Appointment"):
        return "schedule"
    if _has_visible_element(driver, (By.CLASS_NAME, "icheckbox")):
        return "policy"
    return False


def _prepare_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT
    for _ in range(5):
        state = WebDriverWait(driver, timeout).until(_appointment_page_state)
        if state == "ready":
            _select_configured_consulate(driver, timeout=timeout)
            # Date page carries the "I understand" warning box
            # (confirmed_limit_message) that must be checked before the
            # Reschedule submit will work. Check it now so the later
            # booking step never hits an unchecked box.
            try:
                _ensure_warning_acknowledged(driver)
            except Exception as e:
                print(f"Warning checkbox handling failed (continuing): {e}")
            return
        if state == "schedule":
            _click_action_if_present(driver, "Schedule Appointment", timeout)
            continue
        if state == "applicants":
            # Select ALL participants (never deselect) then Continue.
            _ensure_all_applicants_selected_and_continue(driver, timeout=timeout)
            continue

        policy_checkbox = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
        )
        try:
            policy_checkbox.click()
        except Exception:
            # Re-render between locate and click (stale element) -- retry
            # once with a fresh lookup before giving up.
            sleep(1)
            policy_checkbox = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
            )
            policy_checkbox.click()
        continue_button = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.NAME, "commit"))
        )
        continue_button.click()

    WebDriverWait(driver, timeout).until(
        lambda current_driver: _appointment_page_state(current_driver) == "ready"
    )


def get_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT

    # Newer flows show a group-action page with this link. Older flows first
    # show a Continue link and then expose Schedule Appointment.
    if PAID_IVR_ACCOUNT_NUMBER:
        group = _get_dashboard_scope(driver)
        if not _click_action_if_present(group, "Continue", timeout):
            raise TimeoutException(
                "Could not find Continue in the PAID_IVR_ACCOUNT_NUMBER group"
            )
        _click_action_if_present(driver, "Schedule Appointment", timeout)
    elif not _click_action_if_present(driver, "Schedule Appointment", 2):
        if not _click_action_if_present(driver, "Continue", timeout):
            raise TimeoutException(
                "Could not find either 'Schedule Appointment' or 'Continue'"
            )
        _click_action_if_present(driver, "Schedule Appointment", timeout)

    current_url = driver.current_url
    schedule_match = re.search(r"/schedule/(\d+)", current_url)
    if not schedule_match:
        raise RuntimeError(f"Could not find schedule id in URL: {current_url}")

    appointment_url = APPOINTMENT_PAGE_URL.format(id=schedule_match.group(1))
    driver.get(appointment_url)


def get_available_dates(
    driver: WebDriver, request_tracker: RequestTracker
) -> Union[List[datetime.date], None]:
    request_tracker.log_retry()
    request_tracker.retry()
    try:
        current_url = driver.current_url
        request_header_cookie = "".join(
            [f"{cookie['name']}={cookie['value']};" for cookie in driver.get_cookies()]
        )
        user_agent = driver.execute_script("return navigator.userAgent")
        referer = driver.current_url
    except WebDriverException as e:
        # Browser crashed / window closed manually / DevTools disconnected.
        # Polling the same dead driver is pointless -- let the caller
        # start a fresh session.
        log_message(f"Browser session died during date request: {e}")
        raise
    if "/users/sign_in" in current_url:
        # Browser already bounced back to the login page -- cookies dead.
        log_message("Browser on sign_in page during date request - session expired, starting a new session")
        raise SessionExpired("browser redirected to sign_in")
    schedule_base = current_url.split("/appointment")[0]
    request_url = schedule_base + "/appointment" + AVAILABLE_DATE_REQUEST_SUFFIX
    request_headers = REQUEST_HEADERS.copy()
    request_headers["Cookie"] = request_header_cookie
    request_headers["User-Agent"] = user_agent
    request_headers["Referer"] = referer
    try:
        response = requests.get(request_url, headers=request_headers)
    except Exception as e:
        log_message(f"Get available dates request failed: {e}")
        return None
    if response.status_code in (401, 403):
        log_message(f"Session expired (HTTP {response.status_code}) - starting a new session")
        raise SessionExpired(f"HTTP {response.status_code}")
    if response.status_code != 200:
        log_message(f"Failed with status code {response.status_code}")
        log_message(f"Response Text: {response.text[:300]}")
        return None
    try:
        dates_json = response.json()
    except:
        if "sign_in" in response.text or response.text.lstrip().startswith("<"):
            log_message("Received HTML instead of JSON - session expired, starting a new session immediately")
            raise SessionExpired("HTML sign_in instead of JSON")
        else:
            log_message("Failed to decode json")
            log_message(f"Response Text: {response.text[:300]}")
        return None
    dates = [datetime.strptime(item["date"], "%Y-%m-%d").date() for item in dates_json]
    return dates


def reschedule(driver: WebDriver, retryCount: int = 0, current_date=None) -> bool:
    date_request_tracker = RequestTracker(
        retryCount if (retryCount > 0) else DATE_REQUEST_MAX_RETRY,
        DATE_REQUEST_DELAY * retryCount if (retryCount > 0) else DATE_REQUEST_MAX_TIME
    )
    # Wall-clock backstop for healthy sessions: forgiven polls refund
    # RequestTracker time, so without this a session could live for hours on
    # one login (observed logouts ~60min).
    session_wall_start = time.time()
    empty_streak = 0
    while date_request_tracker.should_retry():
        if time.time() - session_wall_start > MAX_HEALTHY_SESSION_AGE:
            log_message(f"Max healthy session age ({MAX_HEALTHY_SESSION_AGE // 60}min) reached - starting a new session")
            return False
        iteration_start = time.time()
        try:
            dates = get_available_dates(driver, date_request_tracker)
        except SessionExpired as e:
            log_message(f"Session expired - starting a new session immediately: {e}")
            return False
        except WebDriverException as e:
            log_message(f"Browser session died - starting a new session: {e}")
            return False
        if dates is None:
            log_message("Error occured when requesting available dates")
            sleep(jittered_delay(DATE_REQUEST_DELAY))
            continue
        if len(dates) == 0:
            empty_streak += 1
            if empty_streak >= 4:
                raise SoftBanDetected
            log_message(f"Empty date list received ({empty_streak}/3 tolerated) - continuing to poll")
            sleep(jittered_delay(DATE_REQUEST_DELAY))
            continue
        empty_streak = 0
        earliest_acceptable_date = datetime.strptime(EARLIEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        latest_acceptable_date = datetime.strptime(LATEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        # Scan the full availability list for the earliest candidate that is
        # inside the acceptable window and outside all exclusion ranges.
        # (Only looking at dates[0] would miss a bookable later date, and
        # could even attempt to book an excluded one.)
        target_date = None
        for candidate in sorted(dates):
            if not (earliest_acceptable_date <= candidate <= latest_acceptable_date):
                continue
            excluded = False
            for i, (start, end) in enumerate(EXCLUSION_DATE_RANGES, 1):
                if datetime.strptime(start, "%Y-%m-%d").date() <= candidate <= datetime.strptime(end, "%Y-%m-%d").date():
                    log_message(f"Skipping {candidate}: falls in excluded date range {start} to {end}")
                    excluded = True
                    break
            if not excluded:
                target_date = candidate
                break
        if target_date is None:
            log_message(f"No acceptable date found. Earliest available date is {dates[0]}")
            sleep(jittered_delay(DATE_REQUEST_DELAY))
            # Healthy response, just nothing in range -- poll forever on
            # the same login. Exempt from retry/time budget so other
            # errors (network/empty/booking) are what trigger a restart.
            date_request_tracker.forgive_last_retry(time.time() - iteration_start)
            continue
        if (
            ONLY_EARLIER_THAN_CURRENT_APPOINTMENT
            and current_date is not None
            and target_date >= current_date
        ):
            log_message(
                f"Skipping {target_date}: not earlier than currently booked "
                f"{current_date} (ONLY_EARLIER_THAN_CURRENT_APPOINTMENT=True; set it to False "
                f"in .env to allow later dates, e.g. different consulate). "
                f"Earliest available on calendar is {min(dates)}."
            )
            sleep(jittered_delay(DATE_REQUEST_DELAY))
            # Same as out-of-window: healthy poll, keep the session alive.
            date_request_tracker.forgive_last_retry(time.time() - iteration_start)
            continue
        if (
            current_date is not None
            and target_date >= current_date
        ):
            log_message(
                f"WARNING: {target_date} is not earlier than currently booked "
                f"{current_date}, but ONLY_EARLIER_THAN_CURRENT_APPOINTMENT=False so proceeding."
            )
        log_message(f"FOUND SLOT ON {target_date}!!!")
        try:
            if legacy_reschedule(
                driver,
                target_date,
                earliest_acceptable_date,
                latest_acceptable_date,
                EXCLUSION_DATE_RANGES,
                current_date,
                ONLY_EARLIER_THAN_CURRENT_APPOINTMENT,
            ):
                _send_gmail_notification(
                    f"Visa Appointment Rescheduled for {target_date}",
                    f"Your visa appointment has been successfully rescheduled to {target_date} at {USER_CONSULATE} consulate.",
                )
                log_message("SUCCESSFULLY RESCHEDULED!!!")
                return True
            return False
        except UnverifiedReschedule as e:
            if not VERIFY_UNVERIFIED_BOOKING:
                log_message(f"STOPPING: {e}")
                _send_gmail_notification(
                    "Visa Rescheduler: MANUAL VERIFICATION NEEDED",
                    f"The rescheduler clicked confirm for {target_date} at {USER_CONSULATE} but could not verify success. "
                    f"Please log in to ais.usvisa-info.com and check your appointment. "
                    f"The program has stopped to avoid wasting reschedule attempts.",
                )
                return True
            # Verification enabled: propagate to reschedule_with_new_session,
            # which quits this (possibly stale) driver first, then re-checks
            # with a fresh login. Attach the attempted date if missing.
            if getattr(e, "target_date", None) is None:
                try:
                    e.target_date = target_date
                except Exception:
                    pass
            raise
        except SessionExpired as e:
            log_message(f"Session expired during booking - starting a new session immediately: {e}")
            return False
        except Exception as e:
            if isinstance(e, WebDriverException):
                log_message(f"Browser session died during booking - starting a new session: {e}")
                return False
            log_message(f"Rescheduling failed: {e}")
            traceback.print_exc()
            continue
    return False


def reschedule_with_new_session(retryCount: int = DATE_REQUEST_MAX_RETRY) -> tuple:
    """Run one polling session. Returns (done, skip_delay).

    done=True means quit the program (booked or manual-check needed).
    skip_delay=True means the caller should start the next session
    immediately without sleeping NEW_SESSION_DELAY (used only after a
    verified-failed unverified attempt, whose ~40s verification already
    acted as cool-down).
    """
    global _consecutive_setup_failures
    driver, user_data_dir = get_chrome_driver()
    old_quit = False
    try:
        session_failures = 0
        timeout = TIMEOUT
        setup_ok = False
        current_date = None
        while session_failures < NEW_SESSION_AFTER_FAILURES:
            try:
                login(driver)
                # Dashboard settles here: capture the booked date BEFORE
                # navigating to the appointment page (the p.consular-appt
                # node is only on the dashboard). Absent => first-book flow.
                try:
                    WebDriverWait(driver, timeout).until(_dashboard_ready)
                except TimeoutException:
                    pass
                current_date = get_current_appointment_date(driver)
                if current_date is not None:
                    log_message(
                        f"Currently booked appointment: {current_date} "
                        f"(ONLY_EARLIER_THAN_CURRENT_APPOINTMENT={ONLY_EARLIER_THAN_CURRENT_APPOINTMENT})"
                    )
                else:
                    log_message(
                        "No currently booked appointment detected (first-book flow). "
                        f"ONLY_EARLIER_THAN_CURRENT_APPOINTMENT={ONLY_EARLIER_THAN_CURRENT_APPOINTMENT} "
                        "(guard inactive without a current date)."
                    )
                get_appointment_page(driver)
                _prepare_appointment_page(driver)
                setup_ok = True
                break
            except Exception as e:
                try:
                    current_page = driver.current_url
                except Exception:
                    current_page = "<browser session dead>"
                log_message(f"Unable to get appointment page at {current_page}: {e}")
                if isinstance(e, (WebDriverException, SessionExpired)):
                    # Dead driver / dead login will never recover with retries
                    # on the same instance -- bail out for a fresh driver.
                    session_failures = NEW_SESSION_AFTER_FAILURES
                    _consecutive_setup_failures += 1
                    break
                session_failures += 1
                _consecutive_setup_failures += 1
                if _consecutive_setup_failures >= SETUP_FAILURE_SOFT_BAN_THRESHOLD:
                    log_message(f"{_consecutive_setup_failures} consecutive setup failures - treating as soft-ban")
                    raise SoftBanDetected
                sleep(FAIL_RETRY_DELAY)
                continue
        if setup_ok:
            _consecutive_setup_failures = 0
        else:
            # Never poll or book after setup failed (including an IVR mismatch).
            return False, False
        try:
            rescheduled = reschedule(driver, retryCount, current_date)
        except UnverifiedReschedule as e:
            # Quit the possibly-stale polling driver BEFORE verification so
            # only one Chrome is alive at a time.
            try:
                driver.quit()
            except Exception:
                pass
            shutil.rmtree(user_data_dir, ignore_errors=True)
            old_quit = True
            target = getattr(e, "target_date", None)
            log_message(f"Unverified attempt for {target} - re-checking with a fresh login...")
            try:
                outcome = verify_booking_after_unverified(current_date, target)
            except Exception as ve:
                log_message(f"Verification crashed: {ve}")
                outcome = "unknown"
            if outcome == "success":
                _send_gmail_notification(
                    f"Visa Appointment Rescheduled for {target}",
                    f"Your visa appointment has been successfully rescheduled to {target} at {USER_CONSULATE} consulate. "
                    f"(Verified with a fresh login after an unverified confirm.)",
                )
                log_message("SUCCESSFULLY RESCHEDULED (verified after unverified)!!!")
                return True, False
            elif outcome == "failure":
                log_message(
                    f"Verification: still booked {current_date}, {target} was not booked - "
                    f"continuing with a new session immediately (no {NEW_SESSION_DELAY}s delay)"
                )
                return False, True
            else:
                log_message(f"STOPPING: {e}")
                _send_gmail_notification(
                    "Visa Rescheduler: MANUAL VERIFICATION NEEDED",
                    f"The rescheduler clicked confirm for {target} at {USER_CONSULATE} but could not verify success. "
                    f"Please log in to ais.usvisa-info.com and check your appointment. "
                    f"The program has stopped to avoid wasting reschedule attempts.",
                )
                return True, False
        if rescheduled:
            return True, False
        else:
            return False, False
    except SessionExpired as e:
        log_message(f"Session expired in this session ({e}) - starting a new session immediately")
        return False, False
    except SoftBanDetected:
        log_message(f"Soft-ban detected - cooling down for {SOFT_BAN_COOLDOWN // 60} minutes before retrying")
        _consecutive_setup_failures = 0
        sleep(SOFT_BAN_COOLDOWN)
        return False, False
    finally:
        if not old_quit:
            try:
                driver.quit()
            except Exception:
                pass
            shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    session_count = 0
    log_message(f"Attempting to reschedule for email: {USER_EMAIL}")
    if TEST_MODE:
        log_message("TEST MODE ENABLED - final confirmation click will be SKIPPED (no booking will be made)")
    else:
        log_message("LIVE MODE - final confirmation click WILL book the appointment!")
    log_message(f"User Consulate: {USER_CONSULATE}")
    log_message(f"Earliest Acceptable Date: {EARLIEST_ACCEPTABLE_DATE}")
    log_message(f"Latest Acceptable Date: {LATEST_ACCEPTABLE_DATE}")
    log_message(f"Only Earlier Than Current Appointment: {ONLY_EARLIER_THAN_CURRENT_APPOINTMENT}")

    if EXCLUSION_DATE_RANGES:
        log_message("Excluded Date Ranges:")
        for i, (start, end) in enumerate(EXCLUSION_DATE_RANGES, 1):
            log_message(f"  Range {i}: {start} to {end}")
    else:
        log_message("No date ranges excluded")

    stopped_by_user = False
    try:
        while True:
            session_count += 1
            log_message(f"Attempting with new session #{session_count}")
            skip_delay = False
            try:
                rescheduled, skip_delay = reschedule_with_new_session()
            except KeyboardInterrupt:
                stopped_by_user = True
                break
            except (WebDriverException, SessionExpired) as e:
                log_message(f"Browser/session died outside poll loop ({e}) - starting a new session")
                rescheduled = False
            except Exception as e:
                log_message(f"Unexpected error in session #{session_count}: {e}")
                traceback.print_exc()
                rescheduled = False
            if skip_delay and not rescheduled:
                log_message("Skipping session delay after verified-failed attempt - starting next session immediately")
            else:
                try:
                    sleep(NEW_SESSION_DELAY)
                except KeyboardInterrupt:
                    stopped_by_user = True
                    break
            if rescheduled:
                break
    except KeyboardInterrupt:
        stopped_by_user = True
    if stopped_by_user:
        log_message("Stopped by user (Ctrl-C) - exiting cleanly without sending exit email.")
        raise SystemExit(0)
    _send_gmail_notification(
        "Rescheduler Program Exited",
        f"The rescheduler program has exited on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
    )
