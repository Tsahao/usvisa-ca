from time import sleep
from datetime import datetime, date
from typing import Sequence, Tuple, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.chrome.webdriver import WebDriver

from settings import (
    TEST_MODE,
    NUM_PARTICIPANTS,
    USER_CONSULATE,
    USER_CONSULATE_ID,
    VERIFY_UNVERIFIED_BOOKING,
)


class UnverifiedReschedule(Exception):
    """Confirm clicked but success unproven. Carries the attempted date.

    target_date is the calendar-selected date (date-only) that was
    attempted, so the caller can verify it against the dashboard without
    re-reading any page state. None when unknown (back-compat).
    """

    def __init__(self, message, target_date=None):
        super().__init__(message)
        self.target_date = target_date


def _select_configured_consulate(driver, timeout=10) -> bool:
    """Select and verify the .env consulate before using the date calendar.

    Returns True when the page selection changed and False when the correct
    facility was already selected.
    """
    selector_id = "appointments_consulate_appointment_facility_id"

    def _select_with_expected_option(current_driver):
        try:
            element = current_driver.find_element(By.ID, selector_id)
            select = Select(element)
            if any(
                option.get_attribute("value") == str(USER_CONSULATE_ID)
                for option in select.options
            ):
                return select
        except Exception:
            return False
        return False

    try:
        select = WebDriverWait(driver, timeout).until(_select_with_expected_option)
    except Exception as exc:
        available = []
        try:
            page_select = Select(driver.find_element(By.ID, selector_id))
            available = [
                f"{option.text.strip()} ({option.get_attribute('value')})"
                for option in page_select.options
            ]
        except Exception:
            pass
        detail = ", ".join(available) if available else "unavailable"
        raise RuntimeError(
            f"{USER_CONSULATE} facility {USER_CONSULATE_ID} is not available "
            f"in the appointment-page selector; page options: {detail}"
        ) from exc
    selected_value = select.first_selected_option.get_attribute("value")
    changed = selected_value != str(USER_CONSULATE_ID)
    if changed:
        print(
            f"Selecting {USER_CONSULATE} consulate "
            f"(facility {USER_CONSULATE_ID})..."
        )
        select.select_by_value(str(USER_CONSULATE_ID))

    def _selection_matches(current_driver):
        try:
            current = Select(current_driver.find_element(By.ID, selector_id))
            option = current.first_selected_option
            selected_id = option.get_attribute("value")
            selected_text = option.text.strip()
            return (
                selected_id == str(USER_CONSULATE_ID)
                and USER_CONSULATE.casefold() in selected_text.casefold()
            )
        except Exception:
            return False

    try:
        WebDriverWait(driver, timeout).until(_selection_matches)
    except Exception as exc:
        available = []
        try:
            available = [
                f"{option.text.strip()} ({option.get_attribute('value')})"
                for option in select.options
            ]
        except Exception:
            pass
        detail = ", ".join(available) if available else "unavailable"
        raise RuntimeError(
            f"Could not select {USER_CONSULATE} facility "
            f"{USER_CONSULATE_ID}; page options: {detail}"
        ) from exc

    # Changing the facility reloads the city-dependent date control. Do not
    # open the calendar until the control is present and usable again.
    WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable(
            (By.ID, "appointments_consulate_appointment_date_input")
        )
    )
    print(
        f"Verified {USER_CONSULATE} consulate "
        f"(facility {USER_CONSULATE_ID}) on the appointment page."
    )
    return changed


# This is frankly very, very bad and should be rewritten with requests
# when I get a test account

def _find_continue_button(driver):
    """Find the applicant-selection 'Continue' submit button robustly.

    The old positional XPath (form/div[2]/div/input) could match a
    participant checkbox after site layout changes, toggling off one
    participant. Prefer explicit value/type matches instead.
    """
    candidates = [
        (By.XPATH, "//form//input[@type='submit' and translate(@value, 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')='CONTINUE']"),
        (By.XPATH, "//form//input[@value='Continue']"),
        (By.XPATH, "//form//button[normalize-space()='Continue']"),
        (By.XPATH, "//main[@id='main']//form//input[@type='submit']"),
        # Legacy positional fallback, kept last on purpose.
        (By.XPATH, "//main[@id='main']/div[@class='mainContent']/form/div[2]/div/input"),
    ]
    for by, locator in candidates:
        for element in driver.find_elements(by, locator):
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
    return None


def _ensure_checkbox_checked(driver, checkbox) -> bool:
    """Robustly check a single checkbox, handling iCheck-styled boxes.

    The site hides the raw <input> (opacity:0 / hidden) and shows a
    div.icheckbox wrapper instead. Clicking the hidden input via JS alone
    does not always flip iCheck state, and is_displayed() is False for
    hidden inputs, so callers must NOT skip non-displayed inputs.
    Returns True if the box ends up selected.
    """
    try:
        if checkbox.is_selected():
            return True
    except Exception:
        pass
    # Try strategies in order; stop as soon as the box reads as selected.
    strategies = []

    def _js_click_input():
        driver.execute_script("arguments[0].click();", checkbox)

    def _click_icheck_wrapper():
        wrapper = driver.execute_script(
            "var el = arguments[0];"
            "while (el && el !== document.body) {"
            "  if (el.classList && el.classList.contains('icheckbox')) return el;"
            "  el = el.parentElement;"
            "}"
            "return null;",
            checkbox,
        )
        if wrapper is None:
            raise RuntimeError("no icheckbox wrapper found")
        wrapper.click()

    def _click_helper():
        helper = driver.execute_script(
            "var el = arguments[0];"
            "var p = el.parentElement;"
            "if (!p) return null;"
            "return p.querySelector('ins.iCheck-helper');",
            checkbox,
        )
        if helper is None:
            raise RuntimeError("no iCheck helper found")
        helper.click()

    def _click_label():
        label = driver.execute_script(
            "var el = arguments[0];"
            "if (el.id) { var l = document.querySelector(\"label[for='\" + el.id + \"']\"); if (l) return l; }"
            "var p = el.parentElement;"
            "while (p && p !== document.body) {"
            "  if (p.tagName === 'LABEL') return p;"
            "  p = p.parentElement;"
            "}"
            "return null;",
            checkbox,
        )
        if label is None:
            raise RuntimeError("no label found")
        label.click()

    def _plain_click():
        checkbox.click()

    strategies = [_js_click_input, _click_icheck_wrapper, _click_helper, _click_label, _plain_click]
    for strategy in strategies:
        try:
            strategy()
            sleep(0.5)
        except Exception:
            continue
        try:
            if checkbox.is_selected():
                return True
        except Exception:
            continue
    try:
        return bool(checkbox.is_selected())
    except Exception:
        return False


def _ensure_all_checkboxes_checked(driver, checkboxes, what: str) -> bool:
    """Check every box in list via _ensure_checkbox_checked. Never unchecks."""
    ok = True
    for checkbox in checkboxes:
        try:
            if checkbox.is_selected():
                continue
        except Exception:
            pass
        print(f"Checking unchecked {what} checkbox...")
        if not _ensure_checkbox_checked(driver, checkbox):
            print(f"WARNING: could not check a {what} checkbox.")
            ok = False
    return ok


def _ensure_warning_acknowledged(driver) -> bool:
    """Check the 'I understand' warning box (confirmed_limit_message).

    HTML (from the live page, when the warning is shown):
      <div class="icheckbox ..."><input type="checkbox"
        name="confirmed_limit_message" id="confirmed_limit_message"
        value="1" class="icheck-input ..."></div>
      <label for="confirmed_limit_message">I understand</label>

    On the date-picker page the site now renders it as
      <input type="hidden" name="confirmed_limit_message" ... value="1">
    i.e. already acknowledged server-side with nothing to click.
    The old code matched by ID/NAME regardless of type, then tried to
    "check" the hidden input -- is_selected() is always False for
    type=hidden so every strategy failed and the attempt aborted.

    The raw input is hidden by iCheck, so plain .click() on the input
    misses -- _ensure_checkbox_checked falls back to the visible
    div.icheckbox wrapper / label. Returns True if the box ends up
    checked (or is absent / hidden, nothing to do).
    """
    box = None
    for locator in (
        (By.XPATH, "//input[@name='confirmed_limit_message' and @type='checkbox']"),
        (By.XPATH, "//input[@id='confirmed_limit_message' and @type='checkbox']"),
        (By.ID, "confirmed_limit_message"),
        (By.NAME, "confirmed_limit_message"),
        (By.XPATH, "//input[@name='confirmed_limit_message']"),
    ):
        try:
            found = driver.find_elements(*locator)
        except Exception:
            continue
        if found:
            # Prefer an actual checkbox; a type=hidden input means the
            # warning is already acknowledged server-side -- nothing to do.
            for candidate in found:
                try:
                    ctype = (candidate.get_attribute("type") or "").lower()
                except Exception:
                    ctype = ""
                if ctype == "checkbox":
                    box = candidate
                    break
            if box is not None:
                break
            # Only non-checkbox (e.g. hidden) matches at this locator --
            # try next locator in case a real checkbox also exists,
            # otherwise fall through to the hidden-input early-out below.
            continue
    if box is None:
        try:
            hidden = driver.find_elements(By.ID, "confirmed_limit_message")
            if hidden:
                htype = (hidden[0].get_attribute("type") or "").lower()
                if htype and htype != "checkbox":
                    return True  # hidden input: already acknowledged
        except Exception:
            pass
        return True  # no warning box on this page, nothing to do
    try:
        if box.is_selected():
            return True
    except Exception:
        pass
    print("Checking 'I understand' warning checkbox (confirmed_limit_message)...")
    return _ensure_checkbox_checked(driver, box)


def _ensure_all_applicants_selected_and_continue(driver, timeout=10):
    """If on the applicant-selection step, check every box and press Continue.

    Returns True if we handled (and submitted) the selection step,
    False if no applicant checkboxes / Continue button were found
    (i.e. we are already on the date-picker page).

    Critical safety rule: NEVER click an already-checked box, since that
    would deselect that participant. Only click unchecked boxes.
    """
    all_checkboxes = driver.find_elements(By.XPATH, "//main[@id='main']//form//input[@type='checkbox']")
    if not all_checkboxes:
        # Fallback: any checkbox inside the main form.
        all_checkboxes = driver.find_elements(By.XPATH, "//form//input[@type='checkbox']")

    continue_btn = _find_continue_button(driver)

    if not all_checkboxes and continue_btn is None:
        return False

    # NOTE: do NOT skip hidden inputs -- iCheck hides the raw <input> and
    # shows a div.icheckbox wrapper instead. _ensure_checkbox_checked tries
    # the wrapper/helper/label fallbacks. Only disabled boxes are skipped
    # (they cannot be checked at all).
    # The "I understand" warning box (confirmed_limit_message) may share
    # this step with the applicant boxes -- it is NOT a participant, but it
    # MUST still be checked before Continue, so it stays in the list.
    applicant_boxes = []
    for cb in all_checkboxes:
        try:
            if (cb.get_attribute("id") or "") == "confirmed_limit_message":
                continue
            if (cb.get_attribute("name") or "") == "confirmed_limit_message":
                continue
        except Exception:
            pass
        applicant_boxes.append(cb)
    _ensure_all_checkboxes_checked(driver, all_checkboxes, "checkbox")

    # Verify selection state before continuing. NEVER click Continue while
    # any enabled box is still unchecked.
    try:
        enabled = []
        for c in all_checkboxes:
            try:
                if not c.is_enabled():
                    continue
            except Exception:
                pass
            enabled.append(c)
        unchecked = [c for c in enabled if not c.is_selected()]
        selected_applicants = sum(1 for c in applicant_boxes if c.is_selected())
        print(f"Participants selected: {selected_applicants}/{len(applicant_boxes)} (expected {NUM_PARTICIPANTS})")
        if NUM_PARTICIPANTS > 1 and applicant_boxes and len(applicant_boxes) != NUM_PARTICIPANTS:
            print(
                f"Warning: found {len(applicant_boxes)} participant checkboxes "
                f"but NUM_PARTICIPANTS={NUM_PARTICIPANTS}. "
                f"Set NUM_PARTICIPANTS={len(applicant_boxes)} in .env to silence this. "
                f"Proceeding with all {len(applicant_boxes)} selected."
            )
        if unchecked:
            print(f"ERROR: {len(unchecked)} checkbox(es) still unchecked, refusing to click Continue.")
            return False
    except Exception:
        pass

    if continue_btn is None:
        return False

    # Guard: make sure the element we are about to click is really the
    # Continue submit, not a checkbox.
    try:
        btn_type = (continue_btn.get_attribute("type") or "").lower()
        btn_value = (continue_btn.get_attribute("value") or "")
        tag = (continue_btn.tag_name or "").lower()
        if tag == "input" and btn_type == "checkbox":
            print("ERROR: Continue locator matched a checkbox, refusing to click (would deselect a participant).")
            return False
        print(f"Clicking Continue ({tag} type={btn_type} value={btn_value}) with all checkboxes selected...")
    except Exception:
        pass

    continue_btn.click()
    return True


_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _read_datepicker_header(driver) -> Optional[Tuple[int, int]]:
    """Read the visible datepicker month as (year, month). None if unparseable."""
    import re as _re

    container_candidates = [
        (By.ID, "ui-datepicker-div"),
        (By.XPATH, "//div[@id='ui-datepicker-div']"),
    ]
    container = None
    for by, value in container_candidates:
        try:
            found = driver.find_elements(by, value)
        except Exception:
            continue
        if found:
            container = found[0]
            break
    if container is None:
        return None
    # Preferred: jQuery-UI month/year spans.
    try:
        month_els = container.find_elements(By.CLASS_NAME, "ui-datepicker-month")
        year_els = container.find_elements(By.CLASS_NAME, "ui-datepicker-year")
        if month_els and year_els:
            month_num = _MONTH_NAME_TO_NUM.get(month_els[0].text.strip().lower())
            year_num = int(year_els[0].text.strip())
            if month_num:
                return (year_num, month_num)
    except Exception:
        pass
    # Fallback: title text like "January 2027".
    try:
        title_els = container.find_elements(By.CLASS_NAME, "ui-datepicker-title")
        texts = [el.text for el in title_els] or [container.text]
        for text in texts:
            m = _re.search(r"([A-Za-z]+)\s+(\d{4})", text or "")
            if m:
                month_num = _MONTH_NAME_TO_NUM.get(m.group(1).lower())
                if month_num:
                    return (int(m.group(2)), month_num)
    except Exception:
        pass
    return None


def _click_datepicker_next(driver) -> bool:
    locators = [
        (By.CSS_SELECTOR, "a.ui-datepicker-next"),
        (By.XPATH, "//div[@id='ui-datepicker-div']//a[contains(@class,'ui-datepicker-next')]"),
        # Legacy positional selector, kept last.
        (By.XPATH, "//div[@id='ui-datepicker-div']/div[2]/div/a"),
    ]
    for by, value in locators:
        try:
            for el in driver.find_elements(by, value):
                try:
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _navigate_to_target_month(driver, target: date, max_clicks: int = 24) -> Optional[bool]:
    """Advance the datepicker to target's month. True=there, False=overshot/cap, None=header unreadable.

    ZERO-SLEEP experiment: no fixed sleeps. Each step logs header
    before->after, wait_ms for the header to change, and day-cell count
    so a log review can judge reliability.
    """
    import time as _time

    step_wait_timeout = 5
    for step in range(max_clicks + 1):
        step_start = _time.time()
        header_before = _read_datepicker_header(driver)
        if header_before is None:
            print(f"[NAV] step {step}: header unreadable before click (target {target})")
            return None
        if header_before == (target.year, target.month):
            try:
                cells = driver.find_elements(By.XPATH, "//div[@id='ui-datepicker-div']/div[1]/table//td")
                cell_count = len(cells)
            except Exception:
                cell_count = -1
            print(f"[NAV] step {step}: already at {header_before[0]}-{header_before[1]:02d} "
                  f"(target {target}), wait_ms={int((_time.time() - step_start) * 1000)}, cells={cell_count}")
            return True
        if header_before > (target.year, target.month):
            print(f"[NAV] step {step}: overshoot {header_before[0]}-{header_before[1]:02d} "
                  f"past target {target}, wait_ms={int((_time.time() - step_start) * 1000)}")
            return False
        if not _click_datepicker_next(driver):
            print(f"[NAV] step {step}: next-arrow not clickable "
                  f"(from {header_before[0]}-{header_before[1]:02d} toward {target}), "
                  f"wait_ms={int((_time.time() - step_start) * 1000)}")
            return False
        # Event-driven: wait until the header text actually changes.
        wait_start = _time.time()
        try:
            WebDriverWait(driver, step_wait_timeout).until(
                lambda d: _read_datepicker_header(d) not in (None, header_before)
            )
            event_fired = True
        except Exception:
            event_fired = False
        header_after = _read_datepicker_header(driver)
        try:
            cells = driver.find_elements(By.XPATH, "//div[@id='ui-datepicker-div']/div[1]/table//td")
            cell_count = len(cells)
        except Exception:
            cell_count = -1
        wait_ms = int((_time.time() - wait_start) * 1000)
        total_ms = int((_time.time() - step_start) * 1000)
        after_txt = f"{header_after[0]}-{header_after[1]:02d}" if header_after else "unreadable"
        print(f"[NAV] step {step}: {header_before[0]}-{header_before[1]:02d}->{after_txt} "
              f"toward {target}, event_fired={event_fired}, wait_ms={wait_ms}, total_ms={total_ms}, cells={cell_count}")
        if header_after is None:
            return None
    print(f"[NAV] cap reached ({max_clicks} clicks) without reaching {target}")
    return False


def _click_exact_day(driver, target: date) -> bool:
    """Click target.day in the visible month if it is available. No fallback to other days."""
    import time as _time
    start = _time.time()
    try:
        month = driver.find_element(By.XPATH, "//div[@id='ui-datepicker-div']/div[1]/table/tbody")
    except Exception as e:
        print(f"[PICK] day {target.day}: month table not found ({e}), ms={int((_time.time() - start) * 1000)}")
        return False
    try:
        cells = month.find_elements(By.TAG_NAME, "td")
    except Exception as e:
        print(f"[PICK] day {target.day}: could not read cells ({e}), ms={int((_time.time() - start) * 1000)}")
        return False
    for cell in cells:
        try:
            links = cell.find_elements(By.TAG_NAME, "a")
        except Exception:
            continue
        if not links:
            continue
        try:
            if links[0].text.strip() != str(target.day):
                continue
            cls = cell.get_attribute("class") or ""
        except Exception:
            continue
        if "unselectable" in cls or "ui-datepicker-other-month" in cls:
            print(f"[PICK] day {target.day}: shown but disabled (slot gone), class={cls!r}, ms={int((_time.time() - start) * 1000)}")
            return False  # day shown but disabled = slot gone
        if "undefined" not in cls:
            # Keep the site's original availability signal; unknown markup = do not click.
            continue
        try:
            links[0].click()
            print(f"[PICK] day {target.day}: clicked, class={cls!r}, ms={int((_time.time() - start) * 1000)}")
            return True
        except Exception as e:
            print(f"[PICK] day {target.day}: click failed ({e}), ms={int((_time.time() - start) * 1000)}")
            return False
    print(f"[PICK] day {target.day}: not found among {len(cells)} cells, ms={int((_time.time() - start) * 1000)}")
    return False


def _date_in_exclusions(candidate: date, exclusions: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    for start, end in exclusions or ():
        try:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            e = datetime.strptime(end, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if s <= candidate <= e:
            return (start, end)
    return None


def legacy_reschedule(
    driver: WebDriver,
    date_to_book: date,
    earliest: Optional[date] = None,
    latest: Optional[date] = None,
    exclusions: Sequence[Tuple[str, str]] = (),
    current_date: Optional[date] = None,
    only_earlier: bool = True,
):
    driver.refresh()

    # Wait for the post-refresh page to load: either the date picker
    # (single-applicant or already-continued) or the applicant-selection
    # step (group flow: checkboxes + Continue).
    try:
        WebDriverWait(driver, 10).until(
            lambda d: len(d.find_elements(By.ID, 'appointments_consulate_appointment_date_input')) > 0
            or _find_continue_button(d) is not None
        )
    except Exception:
        pass

    # Group flow: after refresh we may land on the applicant-selection
    # page (checkboxes + Continue) instead of the date picker. Handle it
    # regardless of NUM_PARTICIPANTS so single-applicant accounts still
    # work and multi-applicant accounts never lose a participant.
    try:
        if _ensure_all_applicants_selected_and_continue(driver, timeout=10):
            # Give the date-picker page a moment to load after Continue.
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.ID, 'appointments_consulate_appointment_date_input')
                    )
                )
            except Exception:
                pass
    except Exception as e:
        print(f"Applicant-selection step handling failed (continuing): {e}")

    _select_configured_consulate(driver, timeout=10)

    date_selection_box = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (
                By.ID, 'appointments_consulate_appointment_date_input'
            )
        )
    )
    sleep(2)
    date_selection_box.click()

    # Target-aware picking: navigate to date_to_book's month and click
    # that exact day. Never fall back to "nearest available" -- that is
    # what booked out-of-window dates (e.g. API said 01-21, calendar
    # clicked 01-08).
    print("Trying to pick time and reschedule...")
    nav_result = _navigate_to_target_month(driver, date_to_book)
    if nav_result is True:
        if not _click_exact_day(driver, date_to_book):
            print(f"{datetime.now().strftime('%H:%M:%S')} SLOT '{date_to_book}' not clickable in calendar (slot gone / API-UI mismatch)\n")
            return False
    elif nav_result is None:
        # Header unreadable (site markup changed?) -- legacy fallback to
        # nearest available, still protected by the guard below. Log loudly
        # so we notice and fix the header parser.
        print("WARNING: datepicker month header unreadable, falling back to nearest-available (guard still applies)")
        month = driver.find_element(By.XPATH, "//div[@id='ui-datepicker-div']/div[1]/table/tbody")
        dates = month.find_elements(By.TAG_NAME, "td")
        ava_date_btn = None
        for cell in dates:
            if cell.get_attribute("class") == " undefined":
                ava_date_btn = cell.find_element(By.TAG_NAME, "a")
                break
        if ava_date_btn is None:
            print(f"{datetime.now().strftime('%H:%M:%S')} No clickable day in calendar\n")
            return False
        ava_date_btn.click()
    else:
        print(f"{datetime.now().strftime('%H:%M:%S')} Could not navigate calendar to {date_to_book} (overshot/cap)\n")
        return False

    # confirm selected date
    sleep(2)
    date_box = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (
                By.ID, 'appointments_consulate_appointment_date'
            )
        )
    )
    date_selected = datetime.strptime(date_box.get_attribute('value'), "%Y-%m-%d").date()
    print(date_selected)
    # Guard: never book outside the caller's criteria, no matter what the
    # calendar was clicked on.
    if earliest is not None and date_selected < earliest:
        print(f"{datetime.now().strftime('%H:%M:%S')} BLOCKED: calendar picked {date_selected}, earlier than earliest {earliest} (wanted {date_to_book})\n")
        return False
    if latest is not None and date_selected > latest:
        print(f"{datetime.now().strftime('%H:%M:%S')} BLOCKED: calendar picked {date_selected}, later than latest {latest} (wanted {date_to_book})\n")
        return False
    excluded_range = _date_in_exclusions(date_selected, exclusions)
    if excluded_range is not None:
        print(f"{datetime.now().strftime('%H:%M:%S')} BLOCKED: calendar picked {date_selected}, in excluded range {excluded_range[0]} to {excluded_range[1]} (wanted {date_to_book})\n")
        return False
    if only_earlier and current_date is not None and date_selected >= current_date:
        print(f"{datetime.now().strftime('%H:%M:%S')} BLOCKED: calendar picked {date_selected}, not earlier than currently booked {current_date} (wanted {date_to_book})\n")
        return False
    if date_selected != date_to_book:
        print(f"{datetime.now().strftime('%H:%M:%S')} NOTE: calendar picked {date_selected}, wanted {date_to_book} -- in-window so proceeding\n")
    else:
        print(f"{datetime.now().strftime('%H:%M:%S')} SLOT '{date_selected}' is still available. Booking....\n")
    if current_date is not None and date_selected >= current_date and not only_earlier:
        print(f"{datetime.now().strftime('%H:%M:%S')} WARNING: {date_selected} is not earlier than {current_date}, proceeding because only_earlier=False\n")

    # Select time of the date:
    sleep(2)
    appointment_time = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.ID, "appointments_consulate_appointment_time"))
    )
    appointment_time.click()
    appointment_time_options = appointment_time.find_elements(By.TAG_NAME, "option")
    appointment_time_options[len(appointment_time_options) - 1].click()

    # The date page requires acknowledging the warning ("I understand" /
    # confirmed_limit_message) before Reschedule will submit. Ensure it is
    # checked -- without this the booking silently does nothing.
    try:
        if not _ensure_warning_acknowledged(driver):
            print("ERROR: warning checkbox (confirmed_limit_message) could not be checked, aborting this attempt.")
            return False
    except Exception as e:
        print(f"Warning checkbox handling failed: {e}")
        return False

    # Click "Reschedule"
    driver.find_element(
        By.XPATH,
        "//form[@id='appointment-form']/div[2]/fieldset/ol/li/input",
    ).click()
    sleep(2)
    confirm = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, "/html/body/div[6]/div/div/a[2]"))
    )
    sleep(2)
    driver.implicitly_wait(0.1)
    if TEST_MODE:
        # SAFETY: never click confirm in TEST_MODE. When verification is
        # enabled, raise UnverifiedReschedule (instead of returning False)
        # so the caller's fresh-driver dashboard check + continue path is
        # exercised end-to-end. The check itself is read-only (dashboard
        # GETs) and, since nothing was booked, verifies as "still old" =>
        # silent continue. No appointment can be made on this path.
        print(f"{datetime.now().strftime('%H:%M:%S')} TEST_MODE enabled - skipping final confirmation click\n")
        if VERIFY_UNVERIFIED_BOOKING:
            raise UnverifiedReschedule(
                "TEST_MODE: confirmation click skipped; simulated unverified "
                f"attempt for {date_selected} to exercise verification.",
                target_date=date_selected,
            )
        return False
    confirm.click()
    sleep(5)
    # Verify the booking actually went through instead of assuming success.
    # Reschedule attempts are limited, so a silent failure must not be
    # treated as either success (wrong confirmation) or a routine retry
    # (wastes attempts) -- raise so the caller stops for manual review.
    page_source = driver.page_source.lower()
    success_indicators = [
        "successfully scheduled",
        "successfully rescheduled",
        "you have successfully",
    ]
    if any(indicator in page_source for indicator in success_indicators):
        print(f"{datetime.now().strftime('%H:%M:%S')} Reschedule confirmed by page message\n")
        return True
    try:
        WebDriverWait(driver, 10).until(
            EC.invisibility_of_element_located((By.ID, "appointments_consulate_appointment_date_input"))
        )
        print(f"{datetime.now().strftime('%H:%M:%S')} Reschedule likely succeeded (appointment form closed). PLEASE VERIFY YOUR APPOINTMENT MANUALLY at ais.usvisa-info.com\n")
        return True
    except Exception:
        raise UnverifiedReschedule(
            "Confirm was clicked but reschedule success could not be verified. "
            "Stopping to avoid wasting limited reschedule attempts. "
            "PLEASE CHECK YOUR APPOINTMENT MANUALLY at ais.usvisa-info.com",
            target_date=date_selected,
        )
