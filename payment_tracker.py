"""Monitor pre-payment earliest dates. Never pay, book, or reschedule."""

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from selenium.common.exceptions import (
    StaleElementReferenceException, TimeoutException, WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from request_tracker import RequestTracker
from telegram_notifier import DeliveryStatus, send_telegram_message


SITE_ORIGIN = "https://ais.usvisa-info.com"
LOGIN_URL = SITE_ORIGIN + "/en-ca/niv/users/sign_in"
SETUP_FAILURE_SOFT_BAN_THRESHOLD = 15
STOP_NOTIFICATION_TIMEOUT = (5, 5)
DIAGNOSTIC_STAGES = frozenset((
    "browser-creation", "login", "dashboard", "group-selection",
    "continue", "payment-navigation", "summary",
))
ROUTE_CATEGORIES = frozenset((
    "trusted-https", "blank", "new-tab", "browser-error", "empty",
    "malformed", "unexpected-scheme", "unexpected-authority", "unavailable",
))
SUMMARY_HEADING_XPATH = (
    "//*[normalize-space(.)='First Available Appointments'"
    " and not(.//*[normalize-space(.)='First Available Appointments'])]"
)
SUMMARY_TABLE_XPATH = "ancestor::*[.//table][1]//table[not(ancestor::table)]"
MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"), 1
    )
}


class TrackerError(Exception):
    """A safe-to-display tracker error, without account/page data."""


class TrackerConfigurationError(TrackerError):
    pass


class SessionExpired(TrackerError):
    pass


class BrowserBlocked(TrackerError):
    pass


class ExtractionError(TrackerError):
    pass


class SetupError(TrackerError):
    """Temporary login/dashboard/payment-page setup failure."""


class FreshBrowserRequired(TrackerError):
    """Navigation or restoration failure requiring trusted re-establishment."""


def normalize_text(value):
    return " ".join(value.split())


def normalize_consulate(value):
    normalized = unicodedata.normalize("NFKD", normalize_text(value))
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = normalized.casefold()
    return "quebec" if normalized == "quebec city" else normalized


def parse_iso_date(value, setting):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TrackerConfigurationError(f"{setting} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise TrackerConfigurationError(f"{setting} is not a valid date") from None


def parse_displayed_date(value):
    text = normalize_text(value)
    if text.casefold() == "no appointments available":
        return None
    match = re.fullmatch(r"([0-9]{1,2}) ([A-Za-z]+),? ([0-9]{4})", text)
    if match:
        month = MONTHS.get(match[2].casefold())
        if month is not None:
            try:
                return date(int(match[3]), month, int(match[1]))
            except ValueError:
                pass
    raise ExtractionError("Selected consulate has an unrecognized appointment date")


def parse_availability_rows(rows, consulate):
    """Parse row cell text from the summary, never from the applicant fee table."""
    matches = []
    for cells in rows:
        if cells and normalize_consulate(cells[0]) == normalize_consulate(consulate):
            if len(cells) != 2:
                raise ExtractionError("Selected consulate row must have two cells")
            matches.append(cells[1])
    if len(matches) != 1:
        raise ExtractionError("Could not find one unique selected-consulate row")
    return parse_displayed_date(matches[0])


@dataclass(frozen=True)
class GroupIdentity:
    ivr: str = field(repr=False)
    schedule_id: str = field(repr=False)

    def __post_init__(self):
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value)
            for value in (self.ivr, self.schedule_id)
        ):
            raise TrackerConfigurationError("Selected group has an invalid identity")


@dataclass(frozen=True)
class TrackerConfig:
    email: str = field(repr=False)
    password: str = field(repr=False)
    unpaid_ivr: str = field(repr=False)
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)
    consulate: str
    earliest: date
    latest: date
    exclusions: tuple
    paid_ivr: str = field(default="", repr=False)
    alert_in_range_only: bool = False
    poll_delay: float = 180
    jitter: float = 30
    fail_delay: float = 180
    cooldown: float = 3600
    timeout: float = 10
    session_age: float = 5400
    new_session_delay: float = 60
    setup_max_attempts: int = 5
    poll_max_retries: int = 5
    poll_max_time: float = 900
    max_failures: int = 5
    state_path: Path = field(
        default_factory=lambda: Path(__file__).with_name(".payment_tracker_state.json")
    )

    @classmethod
    def from_settings(cls, settings):
        def required(name):
            value = getattr(settings, name, None)
            if not isinstance(value, str) or not value.strip():
                raise TrackerConfigurationError(f"Set {name} in the repository .env")
            return value.strip()

        unpaid_ivr = getattr(settings, "UNPAID_IVR_ACCOUNT_NUMBER", "")
        if unpaid_ivr is None:
            unpaid_ivr = ""
        if not isinstance(unpaid_ivr, str):
            raise TrackerConfigurationError("UNPAID_IVR_ACCOUNT_NUMBER must contain digits")
        unpaid_ivr = unpaid_ivr.strip()
        if unpaid_ivr and (not unpaid_ivr.isascii() or not unpaid_ivr.isdecimal()):
            raise TrackerConfigurationError("UNPAID_IVR_ACCOUNT_NUMBER must contain digits")
        paid_ivr = getattr(settings, "PAID_IVR_ACCOUNT_NUMBER", "") or ""
        if unpaid_ivr and unpaid_ivr == paid_ivr:
            raise TrackerConfigurationError("Paid and unpaid IVR selectors must differ")
        token = required("TELEGRAM_BOT_TOKEN")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise TrackerConfigurationError("TELEGRAM_BOT_TOKEN has an invalid format")
        earliest = parse_iso_date(settings.EARLIEST_ACCEPTABLE_DATE, "EARLIEST_ACCEPTABLE_DATE")
        latest = parse_iso_date(settings.LATEST_ACCEPTABLE_DATE, "LATEST_ACCEPTABLE_DATE")
        if earliest > latest:
            raise TrackerConfigurationError("Earliest acceptable date must not exceed latest")
        exclusions = tuple(
            (parse_iso_date(start, "Exclusion start"), parse_iso_date(end, "Exclusion end"))
            for start, end in settings.EXCLUSION_DATE_RANGES
        )
        if any(start > end for start, end in exclusions):
            raise TrackerConfigurationError("Exclusion ranges must not be reversed")
        range_setting = getattr(settings, "FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY", "NO")
        if not isinstance(range_setting, str) or range_setting.strip().casefold() not in (
            "", "no", "false", "0", "off", "yes", "true", "1", "on",
        ):
            raise TrackerConfigurationError(
                "FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY must be YES or NO"
            )
        alert_in_range_only = range_setting.strip().casefold() in ("yes", "true", "1", "on")
        timings = {}
        for field_name, setting_name in (
            ("poll_delay", "DATE_REQUEST_DELAY"), ("jitter", "DATE_REQUEST_JITTER"),
            ("fail_delay", "FAIL_RETRY_DELAY"), ("cooldown", "SOFT_BAN_COOLDOWN"),
            ("timeout", "TIMEOUT"), ("session_age", "MAX_HEALTHY_SESSION_AGE"),
            ("new_session_delay", "NEW_SESSION_DELAY"),
            ("poll_max_time", "DATE_REQUEST_MAX_TIME"),
        ):
            value = getattr(settings, setting_name)
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (field_name not in ("jitter", "new_session_delay") and value == 0)
            ):
                raise TrackerConfigurationError(f"{setting_name} has an invalid interval")
            timings[field_name] = value
        max_failures = settings.NEW_SESSION_AFTER_FAILURES
        if not isinstance(max_failures, int) or isinstance(max_failures, bool) or max_failures < 1:
            raise TrackerConfigurationError("NEW_SESSION_AFTER_FAILURES must be positive")
        poll_max_retries = settings.DATE_REQUEST_MAX_RETRY
        if (
            not isinstance(poll_max_retries, int) or isinstance(poll_max_retries, bool)
            or poll_max_retries < 1
        ):
            raise TrackerConfigurationError("DATE_REQUEST_MAX_RETRY must be positive")
        return cls(
            email=required("USER_EMAIL"), password=required("USER_PASSWORD"),
            unpaid_ivr=unpaid_ivr, bot_token=token, chat_id=required("TELEGRAM_CHAT_ID"),
            consulate=required("USER_CONSULATE"), earliest=earliest, latest=latest,
            exclusions=exclusions, paid_ivr=paid_ivr,
            alert_in_range_only=alert_in_range_only, max_failures=max_failures,
            setup_max_attempts=max_failures, poll_max_retries=poll_max_retries, **timings,
        )

    def fingerprint(self, group_identity):
        # Bot identity, not its secret token: rotating credentials preserves state.
        identity = (
            self.email, group_identity.ivr, group_identity.schedule_id,
            normalize_consulate(self.consulate),
            self.earliest.isoformat(), self.latest.isoformat(),
            [(start.isoformat(), end.isoformat()) for start, end in self.exclusions],
            self.bot_token.split(":", 1)[0], self.chat_id,
            self.alert_in_range_only,
        )
        return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()

    def qualifies(self, observed):
        return (
            observed is not None and (
                not self.alert_in_range_only or (
                    self.earliest <= observed <= self.latest
                    and not any(start <= observed <= end for start, end in self.exclusions)
                )
            )
        )


class AlertState:
    def __init__(self, config, group_identity):
        self.path = config.state_path
        self.fingerprint = config.fingerprint(group_identity)
        self.last_notified = None
        self.last_observed = None
        self.has_observation = False
        self.pending_notification = False
        try:
            with self.path.open(encoding="utf-8") as handle:
                saved = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            raise TrackerError("Cannot read alert state; check the local state file") from None
        if (
            not isinstance(saved, dict) or type(saved.get("version")) is not int
            or saved["version"] not in (1, 2)
        ):
            raise TrackerError("Invalid alert state; check the local state file")
        if not isinstance(saved.get("fingerprint"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", saved["fingerprint"]
        ):
            raise TrackerError("Invalid alert state fingerprint; check the local state file")
        if saved["version"] == 1:
            saved_date = parse_iso_date(saved.get("last_notified_date"), "Alert state")
            saved_observation = saved_date
            pending = False
        else:
            if not all(key in saved for key in (
                "last_notified_date", "last_observed_date", "pending_notification",
            )):
                raise TrackerError("Incomplete alert state; check the local state file")
            saved_date = (
                parse_iso_date(saved["last_notified_date"], "Alert state")
                if saved["last_notified_date"] is not None else None
            )
            saved_observation = (
                parse_iso_date(saved["last_observed_date"], "Alert observation")
                if saved["last_observed_date"] is not None else None
            )
            pending = saved["pending_notification"]
            if not isinstance(pending, bool) or (pending and saved_observation is None):
                raise TrackerError("Invalid pending alert state; check the local state file")
        if saved.get("fingerprint") == self.fingerprint:
            if (
                (saved_date is not None and not config.qualifies(saved_date))
                or (pending and not config.qualifies(saved_observation))
            ):
                raise TrackerError("Saved alert date does not match the configured preferences")
            self.last_notified = saved_date
            self.last_observed = saved_observation
            self.has_observation = True
            self.pending_notification = pending

    def should_notify(self, observed, config):
        return (
            self.pending_notification and observed == self.last_observed
            and config.qualifies(observed)
        )

    def observe(self, observed, config):
        """Record healthy observations; failed deliveries remain pending on repeats."""
        if self.has_observation and observed == self.last_observed:
            return False
        pending = config.qualifies(observed)
        self._save(self.last_notified, observed, pending)
        self.last_observed = observed
        self.has_observation = True
        self.pending_notification = pending
        return True

    def _save(self, last_notified, last_observed, pending):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({
                    "version": 2, "fingerprint": self.fingerprint,
                    "last_notified_date": last_notified.isoformat() if last_notified else None,
                    "last_observed_date": last_observed.isoformat() if last_observed else None,
                    "pending_notification": pending,
                }, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise TrackerError("Cannot save alert state; stopping to prevent lost or repeated alerts") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def mark_delivered(self, observed):
        self._save(observed, observed, False)
        self.last_notified = observed
        self.last_observed = observed
        self.has_observation = True
        self.pending_notification = False


def trusted_schedule_id(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "ais.usvisa-info.com"
        or parsed.username is not None or parsed.password is not None
    ):
        raise TrackerConfigurationError("Selected group left the trusted visa website")
    match = re.fullmatch(
        r"/en-ca/niv/schedule/([0-9]+)(?:/(?:continue_actions|payment))?/?",
        parsed.path,
    )
    return match[1] if match else None


def classify_browser_route(url):
    """Parse a route and return a fixed category without displaying URL contents."""
    if not isinstance(url, str):
        return None, "malformed"
    if not url:
        return None, "empty"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, "malformed"
    if url in ("about:blank", "data:,"):
        return parsed, "blank"
    if url in (
        "chrome://newtab", "chrome://newtab/",
        "chrome://new-tab-page", "chrome://new-tab-page/",
    ):
        return parsed, "new-tab"
    if url == "chrome-error://chromewebdata/":
        return parsed, "browser-error"
    if parsed.scheme != "https":
        return parsed, "unexpected-scheme"
    if parsed.netloc != "ais.usvisa-info.com":
        return parsed, "unexpected-authority"
    return parsed, "trusted-https"


class PaymentBrowser:
    def __init__(self, config, helpers=None):
        if helpers is None:
            # Reuse only login/browser and exact-group lookup; never call booking.
            import reschedule
            helpers = reschedule
        self.helpers = helpers
        self.config = config
        self.driver = None
        self.profile = None
        self.payment_url = None
        self.group_identity = None
        self.stage = "browser-creation"

    def _route_failure(self, error_type, message, category):
        error = error_type(message)
        error.browser_diagnostic = (self.stage, category)
        raise error from None

    def _diagnose_setup_failure(self, error):
        if getattr(error, "browser_diagnostic", None) is not None:
            return
        category = "unavailable"
        if self.driver is not None:
            try:
                _, category = classify_browser_route(self.driver.current_url)
            except Exception:
                pass
        error.browser_diagnostic = (self.stage, category)

    def check_session(self, *, allow_sign_in=False, polling=False):
        # Login/dashboard drift is recoverable; selected-group setup stays strict.
        route_error = (
            FreshBrowserRequired if polling or allow_sign_in else
            TrackerConfigurationError
        )
        parsed, category = classify_browser_route(self.driver.current_url)
        if category != "trusted-https":
            if not polling and category in ("blank", "new-tab", "browser-error", "empty"):
                self._route_failure(
                    SetupError, "Browser page temporarily failed to load", category,
                )
            self._route_failure(
                route_error, "Browser left the trusted visa website", category,
            )
        if polling:
            schedule = re.match(r"/en-ca/niv/schedule/([^/]+)(?:/|$)", parsed.path)
            if schedule and schedule[1] != self.group_identity.schedule_id:
                self._route_failure(
                    FreshBrowserRequired, "Browser left the selected group", category,
                )
        title = self.driver.title.casefold()
        if any(label in title for label in (
            "access denied", "too many requests", "just a moment",
            "verify you are human", "security check",
        )):
            raise BrowserBlocked("Website denied access or requires manual verification")
        challenges = self.driver.find_elements(
            By.CSS_SELECTOR,
            "iframe[src*='captcha'], iframe[src*='challenge'], "
            "#challenge-running, #cf-challenge-running",
        )
        if any(element.is_displayed() for element in challenges):
            raise BrowserBlocked("Website requires manual verification")
        if "/users/sign_in" in parsed.path:
            errors = self.driver.find_elements(
                By.CSS_SELECTOR, ".alert.alert-error, .alert.alert-danger, #error_explanation",
            )
            if any(
                element.is_displayed() and normalize_text(element.text).casefold()
                in ("invalid email or password.", "invalid email or password")
                for element in errors
            ):
                raise TrackerConfigurationError("Visa sign-in rejected the credentials")
            if not allow_sign_in:
                raise SessionExpired("Visa session expired")
        return parsed

    def _dashboard_conclusively_lacks_group(self):
        """A visible matching label may still be waiting for its Continue."""
        try:
            labels = self.driver.find_elements(By.XPATH, self.helpers._IVR_LABEL_XPATH)
            visible = [label for label in labels if label.is_displayed()]
            identities = [
                self.helpers._IVR_ACCOUNT_PATTERN.findall(label.text) for label in visible
            ]
            return bool(identities) and all(len(values) == 1 for values in identities) and (
                [self.config.unpaid_ivr] not in identities
            )
        except WebDriverException:
            return False

    def open(self):
        """Create once; transient setup failures leave the driver available."""
        self.stage = "browser-creation"
        try:
            if self.driver is None:
                self.driver, self.profile = self.helpers.get_chrome_driver()
                self.driver.set_page_load_timeout(self.config.timeout)
            self.payment_url = None
            self.group_identity = None
            self.stage = "login"
            try:
                self.helpers.login(self.driver)
            except TimeoutException:
                self.check_session(allow_sign_in=True)
                raise SetupError("Sign-in temporarily failed to complete") from None
            self.stage = "dashboard"

            def find_group(driver):
                self.check_session(allow_sign_in=True)
                if "/users/sign_in" in urlsplit(driver.current_url).path:
                    return False
                return self.helpers._find_dashboard_group(
                    driver, self.config.unpaid_ivr,
                    single_group_fallback=not self.config.unpaid_ivr,
                )
            try:
                group = WebDriverWait(self.driver, self.config.timeout).until(find_group)
            except TimeoutException:
                self.check_session(allow_sign_in=True)
                if (
                    self.config.unpaid_ivr
                    and "/users/sign_in" not in urlsplit(self.driver.current_url).path
                    and self._dashboard_conclusively_lacks_group()
                ):
                    raise TrackerConfigurationError(
                        "Could not find the configured unpaid IVR group"
                    ) from None
                raise SetupError("Sign-in or dashboard temporarily failed to load") from None
            except RuntimeError:
                raise TrackerConfigurationError(
                    "Unpaid IVR group is ambiguous"
                    if self.config.unpaid_ivr else
                    "Dashboard groups are ambiguous; set UNPAID_IVR_ACCOUNT_NUMBER"
                ) from None
            self.stage = "group-selection"
            self.check_session()
            group_ivrs = self.helpers._IVR_ACCOUNT_PATTERN.findall(group.text)
            if len(group_ivrs) != 1:
                raise TrackerConfigurationError("Selected group has an ambiguous IVR identity")
            resolved_ivr = group_ivrs[0]
            if self.config.unpaid_ivr and resolved_ivr != self.config.unpaid_ivr:
                raise TrackerConfigurationError("Selected group does not match the unpaid IVR")
            if resolved_ivr == self.config.paid_ivr:
                raise TrackerConfigurationError("Selected group matches the configured paid IVR")
            action = self.helpers._find_visible_action(group, "Continue")
            if not action:
                raise SetupError("Selected group Continue is temporarily unavailable")
            self.stage = "continue"
            try:
                action.click()
            except TimeoutException:
                self.check_session()
                raise

            def find_schedule(driver):
                self.check_session()
                return trusted_schedule_id(driver.current_url)
            schedule_id = WebDriverWait(self.driver, self.config.timeout).until(
                find_schedule
            )
            self.payment_url = (
                f"{SITE_ORIGIN}/en-ca/niv/schedule/{schedule_id}/payment"
            )
            self.stage = "payment-navigation"
            try:
                self.driver.get(self.payment_url)
            except TimeoutException:
                self.check_payment_page(establishing=True)
                raise
            self.check_payment_page(establishing=True)
            self.group_identity = GroupIdentity(resolved_ivr, schedule_id)
        except (SetupError, TimeoutException, StaleElementReferenceException) as exc:
            self._diagnose_setup_failure(exc)
            raise
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._diagnose_setup_failure(exc)
            self.close()
            raise

    def _poll_route(self):
        # Do not inspect or refresh an untrusted/different-group page.
        return self.check_session(polling=True)

    def check_payment_page(self, *, establishing=False):
        if establishing:
            self.check_session()
            parsed = urlsplit(self.driver.current_url)
            schedule = re.match(r"/en-ca/niv/schedule/([^/]+)(?:/|$)", parsed.path)
            expected = trusted_schedule_id(self.payment_url)
            if schedule and schedule[1] != expected:
                self._route_failure(
                    TrackerConfigurationError, "Payment navigation left the selected group",
                    "trusted-https",
                )
        else:
            parsed = self._poll_route()
        if parsed.path != urlsplit(self.payment_url).path:
            if establishing:
                raise SetupError("Selected payment page is temporarily unavailable")
            raise FreshBrowserRequired("Could not restore the selected payment page")

    def read_date(self, refresh=False):
        self.stage = "summary"
        if self.group_identity is None or self.payment_url is None:
            raise TrackerConfigurationError("Payment page has not been validated")
        parsed = self._poll_route()
        if parsed.path != urlsplit(self.payment_url).path:
            self.driver.get(self.payment_url)
        elif refresh:
            self.driver.refresh()
        self.check_payment_page()

        def find_heading(driver):
            self.check_payment_page()
            headings = [
                element for element in driver.find_elements(By.XPATH, SUMMARY_HEADING_XPATH)
                if element.is_displayed()
            ]
            if len(headings) > 1:
                raise ExtractionError("Appointment summary heading is ambiguous")
            return headings[0] if headings else False

        try:
            heading = WebDriverWait(self.driver, self.config.timeout).until(find_heading)
        except TimeoutException:
            raise ExtractionError("First Available Appointments summary was not found") from None
        tables = [
            table for table in heading.find_elements(By.XPATH, SUMMARY_TABLE_XPATH)
            if table.is_displayed()
        ]
        if len(tables) != 1:
            raise ExtractionError("Could not identify one appointment summary table")
        rows = [
            [cell.text for cell in row.find_elements(By.XPATH, "./td")]
            for row in tables[0].find_elements(By.XPATH, ".//tr")
            if row.is_displayed()
        ]
        observed = parse_availability_rows(rows, self.config.consulate)
        self.check_payment_page()
        return observed

    def close(self):
        driver, profile = self.driver, self.profile
        self.driver = None
        self.profile = None
        self.group_identity = None
        self.payment_url = None
        try:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
        finally:
            if profile is not None:
                shutil.rmtree(profile, ignore_errors=True)


def format_alert(observed, config):
    return (
        f"{config.consulate} {observed.isoformat()}\n"
        "Call +1 (778) 807-9660 to reschedule."
    )


def log_message(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}")


def recovery_reason(error):
    """Only fixed messages; even operational exceptions may contain secrets."""
    for error_type, message in (
        (BrowserBlocked, "Website denied access or requires manual verification"),
        (SessionExpired, "Visa session expired"),
        (FreshBrowserRequired, "Browser navigation requires a fresh session"),
        (SetupError, "Login or payment-page setup temporarily failed"),
        (ExtractionError, "Appointment summary could not be read"),
        (WebDriverException, "Chrome operation failed"),
    ):
        if isinstance(error, error_type):
            return message
    return "Unexpected browser operation failed"


def requires_fresh_browser(error):
    return isinstance(error, (SessionExpired, FreshBrowserRequired)) or (
        isinstance(error, WebDriverException)
        and not isinstance(error, (TimeoutException, StaleElementReferenceException))
    ) or not isinstance(
        error, (SetupError, ExtractionError, TimeoutException, StaleElementReferenceException),
    )


def log_browser_diagnostic(error, log, recovery):
    """Log one allowlisted diagnostic per failure, never arbitrary metadata."""
    diagnostic = getattr(error, "browser_diagnostic", None)
    if not isinstance(diagnostic, tuple) or len(diagnostic) != 2:
        return
    stage, category = diagnostic
    if (
        not isinstance(stage, str) or stage not in DIAGNOSTIC_STAGES
        or not isinstance(category, str) or category not in ROUTE_CATEGORIES
        or recovery not in ("terminal", "cooldown", "fresh-browser", "same-browser")
    ):
        return
    error_category = "unexpected-operation"
    for error_type, label in (
        (TrackerConfigurationError, "configuration"),
        (BrowserBlocked, "access-block"),
        (SessionExpired, "expired"),
        (FreshBrowserRequired, "navigation"),
        (SetupError, "setup"),
        (ExtractionError, "summary"),
        (TimeoutException, "timeout"),
        (StaleElementReferenceException, "stale-element"),
        (WebDriverException, "chrome-operation"),
    ):
        if isinstance(error, error_type):
            error_category = label
            break
    log(
        f"Browser failure: stage={stage}; route={category}; "
        f"error={error_category}; recovery={recovery}"
    )


def run_tracker(
    config, *, browser_factory=PaymentBrowser, sender=send_telegram_message,
    sleep=time.sleep, clock=time.monotonic, log=log_message, max_checks=None,
):
    """Supervise setup, polling, renewal, and cooldown without calling booking."""
    state = None
    browser = None
    started = 0
    refresh = False
    checks = 0
    session_number = 0
    setup_attempts = 0
    setup_failure_streak = 0
    ready = False
    healthy_session = False
    budget = None
    delivery_failures = 0
    retry_at = 0

    def close_browser():
        nonlocal browser
        old_browser, browser = browser, None
        if old_browser is not None:
            try:
                old_browser.close()
            except Exception:
                log("Chrome cleanup failed; continuing with a fresh session")

    def renew(reason, delay):
        nonlocal ready, healthy_session, setup_attempts, budget
        close_browser()
        ready = False
        healthy_session = False
        setup_attempts = 0
        budget = None
        log(f"{reason}; reopening after {delay:g} seconds")
        sleep(delay)

    try:
        while max_checks is None or checks < max_checks:
            if browser is not None and clock() - started >= config.session_age:
                renew("Maximum browser session age reached", config.new_session_delay)
                continue
            if ready and not budget.should_retry():
                renew("Polling failure-time budget exhausted", config.new_session_delay)
                continue
            failure = None
            try:
                if browser is None:
                    session_number += 1
                    log(f"Starting browser session #{session_number}")
                    browser = browser_factory(config)
                    started = clock()
                if not ready:
                    setup_attempts += 1
                    log(
                        f"Session #{session_number}: setup attempt "
                        f"{setup_attempts}/{config.setup_max_attempts}"
                    )
                    browser.open()
                    ready = True
                    budget = RequestTracker(
                        config.poll_max_retries, config.poll_max_time, clock=clock, log=log,
                    )
                    refresh = False
                iteration_start = clock()
                budget.retry()
                observed = browser.read_date(refresh=refresh)
            except TrackerConfigurationError as exc:
                log_browser_diagnostic(exc, log, "terminal")
                raise
            except TrackerError as exc:
                if not isinstance(exc, (
                    SessionExpired, BrowserBlocked, ExtractionError, SetupError,
                    FreshBrowserRequired,
                )):
                    raise
                failure = exc
            except Exception as exc:
                # Restricted to browser operations, never state writes or delivery.
                failure = exc
            if failure is not None:
                if not healthy_session:
                    setup_failure_streak += 1
                reason = recovery_reason(failure)
                if (
                    isinstance(failure, BrowserBlocked)
                    or setup_failure_streak >= SETUP_FAILURE_SOFT_BAN_THRESHOLD
                ):
                    log_browser_diagnostic(failure, log, "cooldown")
                    log(
                        f"Cooldown: access block or {setup_failure_streak} consecutive "
                        "setup/re-establishment failures"
                    )
                    setup_failure_streak = 0
                    renew(reason, config.cooldown)
                elif requires_fresh_browser(failure) or browser is None:
                    log_browser_diagnostic(failure, log, "fresh-browser")
                    renew(reason, config.new_session_delay)
                elif not ready and setup_attempts >= config.setup_max_attempts:
                    log_browser_diagnostic(failure, log, "fresh-browser")
                    renew("Same-driver setup budget exhausted", config.new_session_delay)
                elif ready and (
                    budget.retries >= config.poll_max_retries or not budget.should_retry()
                ):
                    log_browser_diagnostic(failure, log, "fresh-browser")
                    renew("Same-driver polling budget exhausted", config.new_session_delay)
                else:
                    log_browser_diagnostic(failure, log, "same-browser")
                    refresh = True
                    attempt = budget.retries if ready else setup_attempts
                    phase = "poll failure" if ready else "setup failure"
                    log(
                        f"Session #{session_number}: {phase} #{attempt}: {reason}; "
                        f"retrying on the same browser after {config.fail_delay:g} seconds"
                    )
                    sleep(config.fail_delay)
                continue
            refresh = True
            healthy_session = True
            setup_failure_streak = 0
            # State failures remain terminal and cannot be swallowed by recovery.
            if (
                state is None
                or state.fingerprint != config.fingerprint(browser.group_identity)
            ):
                state = AlertState(config, browser.group_identity)
            checks += 1
            log(f"{config.consulate}: {observed.isoformat() if observed else 'No Appointments Available'}")
            changed = state.observe(observed, config)
            if changed and observed is not None and not config.qualifies(observed):
                log("Telegram alert skipped: displayed date is outside the range or excluded")
            if state.should_notify(observed, config) and clock() >= retry_at:
                result = sender(
                    format_alert(observed, config),
                    bot_token=config.bot_token, chat_id=config.chat_id,
                )
                if result.delivered:
                    state.mark_delivered(observed)
                    delivery_failures = 0
                    retry_at = 0
                    log("Telegram alert delivered; continuing to watch for date changes")
                elif result.status == DeliveryStatus.PERMANENT_FAILURE:
                    raise TrackerConfigurationError(result.reason)
                else:
                    delivery_failures += 1
                    delay = min(
                        config.cooldown,
                        config.fail_delay * (2 ** min(delivery_failures - 1, 10)),
                    )
                    if delivery_failures >= config.max_failures:
                        delay = config.cooldown
                        delivery_failures = 0
                    delay = max(delay, result.retry_after or 0)
                    retry_at = clock() + delay
                    log(f"Telegram delivery not confirmed ({result.reason}); retry deferred")
            if max_checks is None or checks < max_checks:
                sleep(config.poll_delay + random.uniform(0, config.jitter))
            # Refund the entire healthy cycle, including delivery and idle time.
            budget.forgive_last_retry(clock() - iteration_start)
    finally:
        close_browser()


def notify_monitoring_stopped(config):
    """One best-effort shutdown send; never change alert state or exit outcome."""
    try:
        result = send_telegram_message(
            "Monitoring Stopped", bot_token=config.bot_token, chat_id=config.chat_id,
            timeout=STOP_NOTIFICATION_TIMEOUT,
        )
        delivered = result.delivered
    except (Exception, KeyboardInterrupt):
        delivered = False
    try:
        log_message(
            "Telegram stop notification delivered" if delivered else
            "Telegram stop notification not confirmed; exiting without retry"
        )
    except (Exception, KeyboardInterrupt):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Watch the unpaid IVR payment page and send Telegram alerts. "
        "Never books; TEST_MODE does not suppress notifications."
    )
    parser.add_argument(
        "--reset-baseline",
        action="store_true",
        help="Clear the saved Telegram baseline before watching; "
        "the next date allowed by the alert policy will alert again.",
    )
    args = parser.parse_args(argv)
    monitoring_started = False
    try:
        import settings
        config = TrackerConfig.from_settings(settings)
        if args.reset_baseline:
            try:
                config.state_path.unlink()
            except FileNotFoundError:
                log_message("Baseline reset requested; no saved alert state to clear")
            except OSError:
                raise TrackerError(
                    "Cannot clear alert state; check the local state file"
                ) from None
            else:
                log_message("Baseline reset requested; cleared saved alert state")
        log_message(f"Starting payment-page tracker for {config.consulate}")
        log_message(
            f"Alert filtering: {config.earliest.isoformat()} to {config.latest.isoformat()}, "
            "excluding configured ranges"
            if config.alert_in_range_only else
            "Alert filtering: disabled; watching all displayed dates"
        )
        monitoring_started = True
        run_tracker(config)
    except KeyboardInterrupt:
        log_message("Stopped by user")
        return 0
    except TrackerError as exc:
        log_message(str(exc))
        return 1
    except (WebDriverException, OSError, ValueError):
        # No raw exception details: browser/network errors can contain personal data.
        log_message("Tracker could not start; check configuration, Chrome, and local state")
        return 1
    finally:
        if monitoring_started:
            notify_monitoring_stopped(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
