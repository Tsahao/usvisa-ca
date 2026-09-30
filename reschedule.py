import random
import re
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


def jittered_delay(base_delay: float) -> float:
    return base_delay + random.uniform(0, DATE_REQUEST_JITTER)


class SoftBanDetected(Exception):
    pass


# Consecutive appointment-page setup failures across driver sessions.
# NEW_SESSION_AFTER_FAILURES=5 per session, so 15 == ~3 fully failed sessions.
SETUP_FAILURE_SOFT_BAN_THRESHOLD = 15
_consecutive_setup_failures = 0

def get_chrome_driver() -> WebDriver:
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
    options.add_argument(f'--user-data-dir=/tmp/chrome-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    driver = webdriver.Chrome(options=options)
    return driver


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


def _find_visible_action(driver: WebDriver, label: str):
    locators = (
        (By.LINK_TEXT, label),
        (By.XPATH, f"//button[normalize-space()='{label}']"),
        (By.XPATH, f"//input[@value='{label}']"),
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
    if not _click_action_if_present(driver, "Schedule Appointment", 2):
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
    if response.status_code != 200:
        log_message(f"Failed with status code {response.status_code}")
        log_message(f"Response Text: {response.text[:300]}")
        return None
    try:
        dates_json = response.json()
    except:
        if "sign_in" in response.text or response.text.lstrip().startswith("<"):
            log_message("Received HTML instead of JSON - session likely expired or request was blocked, starting a new session")
        else:
            log_message("Failed to decode json")
            log_message(f"Response Text: {response.text[:300]}")
        return None
    dates = [datetime.strptime(item["date"], "%Y-%m-%d").date() for item in dates_json]
    return dates


def reschedule(driver: WebDriver, retryCount: int = 0) -> bool:
    date_request_tracker = RequestTracker(
        retryCount if (retryCount > 0) else DATE_REQUEST_MAX_RETRY,
        DATE_REQUEST_DELAY * retryCount if (retryCount > 0) else DATE_REQUEST_MAX_TIME
    )
    empty_streak = 0
    while date_request_tracker.should_retry():
        iteration_start = time.time()
        try:
            dates = get_available_dates(driver, date_request_tracker)
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
        log_message(f"FOUND SLOT ON {target_date}!!!")
        try:
            if legacy_reschedule(driver, target_date):
                gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
                msg = Message(
                    f"Visa Appointment Rescheduled for {target_date}",
                    to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
                    text=f"Your visa appointment has been successfully rescheduled to {target_date} at {USER_CONSULATE} consulate."
                )
                gmail.send(msg)
                gmail.close()
                log_message("SUCCESSFULLY RESCHEDULED!!!")
                return True
            return False
        except UnverifiedReschedule as e:
            log_message(f"STOPPING: {e}")
            gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
            msg = Message(
                f"Visa Rescheduler: MANUAL VERIFICATION NEEDED",
                to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
                text=f"The rescheduler clicked confirm for {target_date} at {USER_CONSULATE} but could not verify success. "
                     f"Please log in to ais.usvisa-info.com and check your appointment. "
                     f"The program has stopped to avoid wasting reschedule attempts."
            )
            gmail.send(msg)
            gmail.close()
            return True
        except Exception as e:
            if isinstance(e, WebDriverException):
                log_message(f"Browser session died during booking - starting a new session: {e}")
                return False
            log_message(f"Rescheduling failed: {e}")
            traceback.print_exc()
            continue
    return False


def reschedule_with_new_session(retryCount: int = DATE_REQUEST_MAX_RETRY) -> bool:
    global _consecutive_setup_failures
    driver = get_chrome_driver()
    try:
        session_failures = 0
        timeout = TIMEOUT
        setup_ok = False
        while session_failures < NEW_SESSION_AFTER_FAILURES:
            try:
                login(driver)
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
                if isinstance(e, WebDriverException):
                    # Dead driver will never recover with retries on the
                    # same instance -- bail out for a fresh driver.
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
        rescheduled = reschedule(driver, retryCount)
        if rescheduled:
            return True
        else:
            return False
    except SoftBanDetected:
        log_message(f"Soft-ban detected - cooling down for {SOFT_BAN_COOLDOWN // 60} minutes before retrying")
        _consecutive_setup_failures = 0
        sleep(SOFT_BAN_COOLDOWN)
        return False
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    session_count = 0
    log_message(f"Attempting to reschedule for email: {USER_EMAIL}")
    log_message(f"User Consulate: {USER_CONSULATE}")
    log_message(f"Earliest Acceptable Date: {EARLIEST_ACCEPTABLE_DATE}")
    log_message(f"Latest Acceptable Date: {LATEST_ACCEPTABLE_DATE}")

    if EXCLUSION_DATE_RANGES:
        log_message("Excluded Date Ranges:")
        for i, (start, end) in enumerate(EXCLUSION_DATE_RANGES, 1):
            log_message(f"  Range {i}: {start} to {end}")
    else:
        log_message("No date ranges excluded")

    while True:
        session_count += 1
        log_message(f"Attempting with new session #{session_count}")
        try:
            rescheduled = reschedule_with_new_session()
        except WebDriverException as e:
            log_message(f"Browser died outside poll loop ({e}) - starting a new session")
            rescheduled = False
        except Exception as e:
            log_message(f"Unexpected error in session #{session_count}: {e}")
            traceback.print_exc()
            rescheduled = False
        sleep(NEW_SESSION_DELAY)
        if rescheduled:
            break
    gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
    msg = Message(
        f"Rescheduler Program Exited",
        to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
        text=f"The rescheduler program has exited on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
    )
    gmail.send(msg)
    gmail.close()
