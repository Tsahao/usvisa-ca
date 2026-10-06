import json
import os
import runpy
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

import payment_tracker as tracker
from telegram_notifier import DeliveryStatus, TelegramDeliveryResult

# A standalone run of this suite must never load real .env credentials.
with patch.dict(os.environ, {"USER_CONSULATE": "Calgary"}, clear=True), patch("dotenv.load_dotenv"):
    import reschedule as booking

# Credentials, IVRs, and schedule IDs below are synthetic test fixtures.
TEST_GROUP = tracker.GroupIdentity("002222", "202")
RESCHEDULE_LINE = "\nCall +1 (778) 807-9660 to reschedule."


def make_config(directory):
    return tracker.TrackerConfig(
        email="test@example.invalid",
        password="synthetic-password",
        unpaid_ivr="002222",
        bot_token="123:synthetic_test_token",
        chat_id="456",
        consulate="Calgary",
        earliest=date(2026, 10, 1),
        latest=date(2026, 11, 30),
        exclusions=(),
        poll_delay=10,
        jitter=0,
        fail_delay=10,
        cooldown=100,
        new_session_delay=10,
        state_path=Path(directory) / "state.json",
    )


class ImmediateWait:
    def __init__(self, scope, timeout):
        self.scope = scope

    def until(self, predicate):
        for _ in range(3):
            value = predicate(self.scope)
            if value:
                return value
        raise TimeoutException("Condition did not become true")


class Element:
    def __init__(self, text="", children=None, visible=True):
        self.text = text
        self.children = children or {}
        self.visible = visible

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return True

    def find_elements(self, by, value):
        if (by, value) not in self.children:
            raise AssertionError(f"Unexpected locator: {by}, {value}")
        return self.children[(by, value)]


class ContinueAction(Element):
    def __init__(self, driver, schedule):
        super().__init__()
        self.driver = driver
        self.schedule = schedule
        self.clicks = 0

    def click(self):
        self.clicks += 1
        self.driver.current_url = (
            tracker.SITE_ORIGIN + f"/en-ca/niv/schedule/{self.schedule}/continue_actions"
        )


class FakeDriver:
    def __init__(self):
        self.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"
        self.title = "US Visa"
        self.labels = []
        self.headings = []
        self.challenges = []
        self.urls = []
        self.refreshes = 0
        self.quits = 0
        self.redirect = None
        self.errors = []
        self.page_load_timeout = None

    def set_page_load_timeout(self, timeout):
        self.page_load_timeout = timeout

    def add_group(self, ivr, schedule):
        action = ContinueAction(self, schedule)
        group = Element(
            f"IVR Account Number: {ivr}",
            {
                (By.LINK_TEXT, "Continue"): [action],
            },
        )
        label = Element(
            f"IVR Account Number: {ivr}",
            {
                (By.XPATH, booking._GROUP_ANCESTOR_XPATH): [group],
            },
        )
        self.labels.append(label)
        return action

    def add_summary(self, rows):
        table = Element(
            children={
                (By.XPATH, ".//tr"): [
                    Element(children={(By.XPATH, "./td"): [Element(cell) for cell in row]})
                    for row in rows
                ]
            }
        )
        heading = Element(
            "First Available Appointments",
            {
                (By.XPATH, tracker.SUMMARY_TABLE_XPATH): [table],
            },
        )
        self.headings.append(heading)
        return heading

    def find_elements(self, by, value):
        if (by, value) == (By.XPATH, booking._IVR_LABEL_XPATH):
            return self.labels
        if (by, value) == (By.XPATH, tracker.SUMMARY_HEADING_XPATH):
            return self.headings
        if by == By.CSS_SELECTOR and value.startswith("iframe[src*='captcha']"):
            return self.challenges
        if (by, value) == (
            By.CSS_SELECTOR,
            ".alert.alert-error, .alert.alert-danger, #error_explanation",
        ):
            return self.errors
        raise AssertionError(f"Forbidden/unexpected page lookup: {by}, {value}")

    def get(self, url):
        if not url.endswith("/payment"):
            raise AssertionError("Must never visit the calendar or submit a payment")
        self.urls.append(url)
        self.current_url = self.redirect if self.redirect is not None else url

    def refresh(self):
        self.refreshes += 1

    def quit(self):
        self.quits += 1


class ConfigAndParsingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = make_config(self.directory.name)

    def settings(self, **overrides):
        values = dict(
            USER_EMAIL="test@example.invalid",
            USER_PASSWORD="synthetic-password",
            UNPAID_IVR_ACCOUNT_NUMBER="002222",
            PAID_IVR_ACCOUNT_NUMBER="1111",
            TELEGRAM_BOT_TOKEN="123:synthetic_test_token",
            TELEGRAM_CHAT_ID="456",
            USER_CONSULATE="Calgary",
            EARLIEST_ACCEPTABLE_DATE="2026-10-01",
            LATEST_ACCEPTABLE_DATE="2026-11-30",
            EXCLUSION_DATE_RANGES=[],
            DATE_REQUEST_DELAY=10,
            DATE_REQUEST_JITTER=0,
            FAIL_RETRY_DELAY=10,
            SOFT_BAN_COOLDOWN=100,
            TIMEOUT=10,
            MAX_HEALTHY_SESSION_AGE=5400,
            NEW_SESSION_AFTER_FAILURES=5,
            NEW_SESSION_DELAY=10,
            DATE_REQUEST_MAX_RETRY=5,
            DATE_REQUEST_MAX_TIME=900,
            TEST_MODE=True,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_shared_settings_and_leading_zeros(self):
        config = tracker.TrackerConfig.from_settings(self.settings())
        self.assertEqual(config.unpaid_ivr, "002222")
        self.assertEqual(config.consulate, "Calgary")
        self.assertEqual(config.earliest, date(2026, 10, 1))
        self.assertFalse(hasattr(config, "test_mode"))
        self.assertNotIn("synthetic-password", repr(config))
        self.assertNotIn("synthetic_test_token", repr(config))

    def test_invalid_tracker_configuration_fails_before_browser_start(self):
        for overrides in (
            {"UNPAID_IVR_ACCOUNT_NUMBER": "letters"},
            {"UNPAID_IVR_ACCOUNT_NUMBER": 123},
            {"UNPAID_IVR_ACCOUNT_NUMBER": False},
            {"UNPAID_IVR_ACCOUNT_NUMBER": "１２"},
            {"UNPAID_IVR_ACCOUNT_NUMBER": "1111"},
            {"TELEGRAM_BOT_TOKEN": ""},
            {"TELEGRAM_CHAT_ID": ""},
            {"USER_EMAIL": ""},
            {"USER_PASSWORD": ""},
            {"EARLIEST_ACCEPTABLE_DATE": "2026-1-01"},
            {"EARLIEST_ACCEPTABLE_DATE": "2026-12-01"},
            {"LATEST_ACCEPTABLE_DATE": "2026-02-30"},
            {"EXCLUSION_DATE_RANGES": [("2026-11-01", "2026-10-01")]},
            {"DATE_REQUEST_DELAY": 0},
            {"DATE_REQUEST_JITTER": -1},
            {"TIMEOUT": float("nan")},
            {"NEW_SESSION_AFTER_FAILURES": 0},
            {"NEW_SESSION_DELAY": -1},
            {"NEW_SESSION_DELAY": True},
            {"DATE_REQUEST_MAX_RETRY": 0},
            {"DATE_REQUEST_MAX_RETRY": True},
            {"DATE_REQUEST_MAX_TIME": 0},
            {"DATE_REQUEST_MAX_TIME": float("inf")},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(tracker.TrackerConfigurationError):
                    tracker.TrackerConfig.from_settings(self.settings(**overrides))

    def test_unpaid_selector_is_optional_and_never_uses_paid_selector(self):
        for value in ("", "   ", None):
            config = tracker.TrackerConfig.from_settings(
                self.settings(UNPAID_IVR_ACCOUNT_NUMBER=value)
            )
            self.assertEqual(config.unpaid_ivr, "")
            self.assertEqual(config.paid_ivr, "1111")
        settings = self.settings()
        del settings.UNPAID_IVR_ACCOUNT_NUMBER
        self.assertEqual(tracker.TrackerConfig.from_settings(settings).unpaid_ivr, "")
        config = tracker.TrackerConfig.from_settings(
            self.settings(
                UNPAID_IVR_ACCOUNT_NUMBER="",
                PAID_IVR_ACCOUNT_NUMBER="",
            )
        )
        self.assertEqual(config.unpaid_ivr, "")

    def test_group_identity_requires_actual_digit_strings_and_hides_details(self):
        for ivr, schedule in (("", "202"), ("002222", ""), ("letters", "202"), ("１２", "202")):
            with self.subTest(ivr=ivr, schedule=schedule):
                with self.assertRaises(tracker.TrackerConfigurationError):
                    tracker.GroupIdentity(ivr, schedule)
        self.assertNotIn("002222", repr(TEST_GROUP))
        self.assertNotIn("202", repr(TEST_GROUP))

    def test_paid_selector_and_optional_tracker_settings_for_booking(self):
        for paid, expected in ((None, ""), ("", ""), (" 0022 ", "0022")):
            env = {"USER_CONSULATE": "Calgary", "UNPAID_IVR_ACCOUNT_NUMBER": "invalid"}
            if paid is not None:
                env["PAID_IVR_ACCOUNT_NUMBER"] = paid
            with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv") as load:
                settings = runpy.run_path(str(Path(booking.__file__).with_name("settings.py")))
            self.assertEqual(settings["PAID_IVR_ACCOUNT_NUMBER"], expected)
            self.assertEqual(settings["TELEGRAM_BOT_TOKEN"], "")
            load.assert_called_once_with(Path(booking.__file__).with_name(".env"))

    def test_only_paid_and_unpaid_ivr_selectors_are_exported(self):
        with patch.dict(os.environ, {"USER_CONSULATE": "Calgary"}, clear=True):
            with patch("dotenv.load_dotenv"):
                settings = runpy.run_path(str(Path(booking.__file__).with_name("settings.py")))
        self.assertEqual(
            {name for name in settings if name.endswith("_ACCOUNT_NUMBER")},
            {"PAID_IVR_ACCOUNT_NUMBER", "UNPAID_IVR_ACCOUNT_NUMBER"},
        )

    def test_screenshot_rows_and_wrapped_unavailability(self):
        rows = [
            ["Vancouver", "12 January, 2027"],
            ["Ottawa", "26 October, 2026"],
            ["Calgary", "6 November, 2026"],
            ["Halifax", "3 November, 2026"],
            ["Toronto", "16 November, 2026"],
            ["Montréal", "No Appointments\nAvailable"],
            ["Québec\nCity", "No Appointments Available"],
        ]
        self.assertEqual(tracker.parse_availability_rows(rows, " calGARY "), date(2026, 11, 6))
        self.assertIsNone(tracker.parse_availability_rows(rows, "Montreal"))
        self.assertIsNone(tracker.parse_availability_rows(rows, "Quebec"))

    def test_english_date_parsing_does_not_depend_on_locale(self):
        self.assertEqual(
            tracker.parse_displayed_date(" 6\nNovember,\u00a0 2026 "), date(2026, 11, 6)
        )
        self.assertEqual(tracker.parse_displayed_date("6 November 2026"), date(2026, 11, 6))

    def test_missing_duplicate_and_malformed_rows_are_errors(self):
        for rows in (
            [],
            [["Toronto", "6 November, 2026"]],
            [["Calgary", "6 November, 2026"], ["Calgary", "7 November, 2026"]],
            [["Calgary"]],
            [["Calgary", "6 November, 2026", "extra"]],
            [["Calgary", "31 November, 2026"]],
            [["Calgary", "unknown"]],
        ):
            with self.subTest(rows=rows), self.assertRaises(tracker.ExtractionError):
                tracker.parse_availability_rows(rows, "Calgary")

    def test_bounds_and_exclusions_are_inclusive(self):
        config = replace(self.config, alert_in_range_only=True)
        self.assertTrue(config.qualifies(config.earliest))
        self.assertTrue(config.qualifies(config.latest))
        self.assertFalse(config.qualifies(date(2026, 9, 30)))
        self.assertFalse(config.qualifies(date(2026, 12, 1)))
        config = replace(config, exclusions=((date(2026, 10, 10), date(2026, 10, 20)),))
        for observed in (date(2026, 10, 10), date(2026, 10, 15), date(2026, 10, 20)):
            self.assertFalse(config.qualifies(observed))
        self.assertFalse(config.qualifies(None))

    def test_range_policy_defaults_to_no_and_accepts_yes_no_variants(self):
        config = tracker.TrackerConfig.from_settings(self.settings())
        self.assertFalse(config.alert_in_range_only)
        for value in ("", "NO", " no ", "False", "0", "off"):
            with self.subTest(value=value):
                config = tracker.TrackerConfig.from_settings(
                    self.settings(FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY=value)
                )
                self.assertFalse(config.alert_in_range_only)
        for value in ("YES", " yes ", "True", "1", "on"):
            with self.subTest(value=value):
                config = tracker.TrackerConfig.from_settings(
                    self.settings(FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY=value)
                )
                self.assertTrue(config.alert_in_range_only)

    def test_invalid_range_policy_is_rejected_only_by_tracker(self):
        for value in ("maybe", None, True, 1):
            with self.subTest(value=value):
                with self.assertRaises(tracker.TrackerConfigurationError):
                    tracker.TrackerConfig.from_settings(
                        self.settings(FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY=value)
                    )
        with (
            patch.dict(
                os.environ,
                {
                    "USER_CONSULATE": "Calgary",
                    "FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY": "invalid",
                },
                clear=True,
            ),
            patch("dotenv.load_dotenv"),
        ):
            settings = runpy.run_path(str(Path(booking.__file__).with_name("settings.py")))
        self.assertEqual(settings["FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY"], "invalid")

    def test_shared_settings_range_policy_defaults_to_no(self):
        with patch.dict(os.environ, {"USER_CONSULATE": "Calgary"}, clear=True):
            with patch("dotenv.load_dotenv"):
                settings = runpy.run_path(str(Path(booking.__file__).with_name("settings.py")))
        self.assertEqual(settings["FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY"], "NO")

    def test_no_policy_accepts_out_of_window_and_excluded_dates(self):
        config = replace(self.config, exclusions=((date(2026, 10, 10), date(2026, 10, 20)),))
        for observed in (date(2026, 9, 30), date(2027, 1, 12), date(2026, 10, 15)):
            self.assertTrue(config.qualifies(observed))
        self.assertFalse(config.qualifies(None))


class PaymentBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = make_config(self.directory.name)
        self.driver = FakeDriver()
        self.paid_action = self.driver.add_group("1111", "101")
        self.unpaid_action = self.driver.add_group("002222", "202")
        self.heading = self.driver.add_summary(
            [
                ["Toronto", "1 October, 2026"],
                ["Calgary", "6 November, 2026"],
            ]
        )
        self.profile = Path(self.directory.name) / "chrome-profile"
        self.profile.mkdir()
        self.helpers = SimpleNamespace(
            get_chrome_driver=lambda: (self.driver, self.profile),
            login=Mock(),
            _find_dashboard_group=booking._find_dashboard_group,
            _IVR_LABEL_XPATH=booking._IVR_LABEL_XPATH,
            _IVR_ACCOUNT_PATTERN=booking._IVR_ACCOUNT_PATTERN,
            _find_visible_action=booking._find_visible_action,
        )
        self.browser = tracker.PaymentBrowser(self.config, self.helpers)
        self.addCleanup(self.browser.close)
        patcher = patch.object(tracker, "WebDriverWait", ImmediateWait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_targets_unpaid_group_and_never_touches_paid_calendar(self):
        self.browser.open()
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.unpaid_action.clicks, 1)
        self.assertEqual(self.browser.group_identity, TEST_GROUP)
        self.assertEqual(
            self.driver.urls, [tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/payment"]
        )
        self.assertEqual(self.browser.read_date(), date(2026, 11, 6))
        self.assertEqual(self.browser.read_date(refresh=True), date(2026, 11, 6))
        self.assertEqual(self.driver.refreshes, 1)
        self.browser.close()
        self.assertEqual(self.driver.quits, 1)
        self.assertFalse(self.profile.exists())

    def test_chrome_initialization_failure_cleans_its_temporary_profile(self):
        with patch.object(booking.webdriver, "Chrome", side_effect=WebDriverException()):
            with patch.object(booking.shutil, "rmtree") as cleanup:
                with self.assertRaises(WebDriverException):
                    booking.get_chrome_driver()
        cleanup.assert_called_once()
        self.assertTrue(cleanup.call_args.args[0].startswith("/tmp/chrome-"))
        self.assertTrue(cleanup.call_args.kwargs["ignore_errors"])

    def test_missing_unpaid_group_never_falls_back(self):
        self.driver.labels.pop()
        with self.assertRaises(tracker.TrackerConfigurationError):
            self.browser.open()
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])
        self.assertEqual(self.driver.quits, 1)

    def test_duplicate_unpaid_group_is_rejected(self):
        self.driver.add_group("002222", "303")
        with self.assertRaises(tracker.TrackerConfigurationError):
            self.browser.open()
        self.assertEqual(self.unpaid_action.clicks, 0)

    def use_automatic_selection(self, paid_ivr=""):
        self.browser.config = replace(self.config, unpaid_ivr="", paid_ivr=paid_ivr)

    def test_blank_selector_uses_only_group_and_preserves_actual_identity(self):
        self.driver.labels.pop(0)
        self.use_automatic_selection()
        with patch.object(booking, "PAID_IVR_ACCOUNT_NUMBER", "unrelated"):
            self.browser.open()
            self.assertEqual(booking.PAID_IVR_ACCOUNT_NUMBER, "unrelated")
        self.assertEqual(self.unpaid_action.clicks, 1)
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.browser.group_identity, TEST_GROUP)
        self.assertEqual(self.browser.read_date(), date(2026, 11, 6))

    def test_blank_selector_rejects_multiple_groups_without_payment_inference(self):
        self.use_automatic_selection()
        with self.assertRaisesRegex(tracker.TrackerConfigurationError, "UNPAID_IVR_ACCOUNT_NUMBER"):
            self.browser.open()
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.unpaid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])

    def test_blank_selector_retries_an_unloaded_dashboard_without_selecting(self):
        self.driver.labels = []
        self.use_automatic_selection()
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertEqual(self.driver.urls, [])

    def test_blank_selector_rejects_duplicate_cards_with_same_ivr(self):
        self.driver.labels.pop(0)
        duplicate = self.driver.add_group("002222", "303")
        self.use_automatic_selection()
        with self.assertRaises(tracker.TrackerConfigurationError):
            self.browser.open()
        self.assertEqual(duplicate.clicks, 0)
        self.assertEqual(self.unpaid_action.clicks, 0)

    def test_multiple_label_nodes_in_one_card_do_not_count_as_multiple_groups(self):
        self.driver.labels.pop(0)
        self.driver.labels.append(self.driver.labels[0])
        self.use_automatic_selection()
        self.browser.open()
        self.assertEqual(self.browser.group_identity, TEST_GROUP)

    def test_hidden_label_does_not_count_as_a_visible_group(self):
        self.driver.labels[0].visible = False
        self.use_automatic_selection()
        self.browser.open()
        self.assertEqual(self.unpaid_action.clicks, 1)
        self.assertEqual(self.paid_action.clicks, 0)

    def test_automatic_selection_rejects_malformed_identity_or_spanning_container(self):
        group = self.driver.labels[0].children[(By.XPATH, booking._GROUP_ANCESTOR_XPATH)][0]
        self.driver.labels.pop()
        self.use_automatic_selection()
        for text in (
            "IVR Account Number: unknown",
            "IVR Account Number: 1111\nIVR Account Number: 002222",
        ):
            group.text = text
            with self.subTest(text=text), self.assertRaises(tracker.TrackerConfigurationError):
                self.browser.open()
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])

    def test_unidentifiable_second_label_prevents_automatic_selection(self):
        self.driver.labels[0].children[(By.XPATH, booking._GROUP_ANCESTOR_XPATH)] = []
        self.use_automatic_selection()
        with self.assertRaises(tracker.TrackerConfigurationError):
            self.browser.open()
        self.assertEqual(self.unpaid_action.clicks, 0)

    def test_stale_second_label_never_looks_like_a_single_group(self):
        self.driver.labels[0].is_displayed = Mock(side_effect=WebDriverException())
        self.use_automatic_selection()
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertEqual(self.unpaid_action.clicks, 0)

    def test_automatic_selection_refuses_group_marked_as_paid(self):
        self.driver.labels.pop()
        self.use_automatic_selection(paid_ivr="1111")
        with self.assertRaisesRegex(tracker.TrackerConfigurationError, "paid IVR"):
            self.browser.open()
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])

    def test_automatic_selection_still_requires_payment_route_and_summary(self):
        self.driver.labels.pop(0)
        self.use_automatic_selection()
        self.driver.redirect = tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/appointment"
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertIsNone(self.browser.group_identity)
        self.driver.redirect = None
        self.browser.open()
        self.driver.headings = []
        with self.assertRaises(tracker.ExtractionError):
            self.browser.read_date()

    def test_only_selected_summary_table_is_read(self):
        # No global table lookup exists on the fake driver: fee-table reads fail.
        self.browser.open()
        self.assertEqual(self.browser.read_date(), date(2026, 11, 6))
        self.heading.children[(By.XPATH, tracker.SUMMARY_TABLE_XPATH)].append(Element())
        with self.assertRaises(tracker.ExtractionError):
            self.browser.read_date()

    def test_missing_or_duplicate_summary_heading_is_rejected(self):
        self.browser.open()
        self.driver.headings = []
        with self.assertRaises(tracker.ExtractionError):
            self.browser.read_date()
        self.driver.headings = [self.heading, self.heading]
        with self.assertRaises(tracker.ExtractionError):
            self.browser.read_date()

    def test_payment_page_redirect_is_not_treated_as_unavailability(self):
        self.driver.redirect = tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/appointment"
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertIs(self.browser.driver, self.driver)
        self.assertIsNone(self.browser.group_identity)
        self.browser.close()
        self.assertEqual(self.driver.quits, 1)

    def test_expired_session_and_challenges_are_distinct(self):
        self.browser.open()
        self.driver.current_url = tracker.LOGIN_URL
        with self.assertRaises(tracker.SessionExpired):
            self.browser.read_date()
        self.driver.title = "Just a moment..."
        with self.assertRaises(tracker.BrowserBlocked):
            self.browser.read_date()

    def test_incomplete_login_keeps_driver_for_retry(self):
        self.driver.current_url = tracker.LOGIN_URL
        self.driver.labels = []
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertEqual(self.driver.quits, 0)
        self.assertTrue(self.profile.exists())
        self.browser.close()
        self.assertEqual(self.driver.quits, 1)
        self.assertFalse(self.profile.exists())

    def test_untrusted_navigation_is_rejected(self):
        for url in (
            "http://ais.usvisa-info.com/en-ca/niv/schedule/202/payment",
            "https://evil.invalid/en-ca/niv/schedule/202/payment",
            "https://ais.usvisa-info.com@evil.invalid/en-ca/niv/schedule/202/payment",
        ):
            with self.subTest(url=url), self.assertRaises(tracker.TrackerConfigurationError):
                tracker.trusted_schedule_id(url)
        self.assertIsNone(
            tracker.trusted_schedule_id(tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/appointment")
        )

    def test_browser_crash_during_setup_cleans_up(self):
        self.helpers.login.side_effect = WebDriverException("synthetic failure")
        with self.assertRaises(WebDriverException):
            self.browser.open()
        self.assertEqual(self.driver.quits, 1)
        self.assertFalse(self.profile.exists())

    def test_login_timeout_retries_on_one_driver_and_bounds_page_load(self):
        create = Mock(return_value=(self.driver, self.profile))
        self.helpers.get_chrome_driver = create
        self.helpers.login.side_effect = [TimeoutException("private"), None]
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.browser.open()
        create.assert_called_once()
        self.assertEqual(self.helpers.login.call_count, 2)
        self.assertEqual(self.driver.page_load_timeout, self.config.timeout)
        self.assertEqual(self.driver.quits, 0)
        self.assertEqual(self.browser.group_identity, TEST_GROUP)

    def test_login_timeout_on_blank_browser_is_not_invalid_configuration(self):
        for destination in (
            "about:blank",
            "data:,",
            "chrome-error://chromewebdata/",
            "chrome://newtab/",
            "chrome://new-tab-page/",
            "",
        ):
            self.driver.current_url = destination
            self.helpers.login.side_effect = TimeoutException("private")
            with self.subTest(destination=destination):
                with patch.object(self.driver, "find_elements") as lookup:
                    with self.assertRaises(tracker.SetupError):
                        self.browser.open()
                    lookup.assert_not_called()
            self.assertIs(self.browser.driver, self.driver)
        self.assertEqual(self.driver.quits, 0)

    def test_unexpected_login_and_dashboard_routes_require_fresh_browser(self):
        for timeout in (True, False):
            for destination, category in (
                ("https://private-host.invalid/path?private-token", "unexpected-authority"),
                ("http://ais.usvisa-info.com/en-ca/niv/users/sign_in", "unexpected-scheme"),
                ("https://[malformed", "malformed"),
                (None, "malformed"),
                ("chrome://settings/", "unexpected-scheme"),
                ("data:text/html,private-content", "unexpected-scheme"),
            ):
                self.driver.current_url = destination
                self.helpers.login.side_effect = TimeoutException("private") if timeout else None
                previous_quits = self.driver.quits
                with self.subTest(timeout=timeout, category=category):
                    with patch.object(self.driver, "find_elements") as lookup:
                        with self.assertRaises(tracker.FreshBrowserRequired) as raised:
                            self.browser.open()
                        lookup.assert_not_called()
                    self.assertEqual(
                        raised.exception.browser_diagnostic,
                        ("login" if timeout else "dashboard", category),
                    )
                    self.assertEqual(self.driver.quits, previous_quits + 1)
                    self.assertIsNone(self.browser.driver)
                    self.assertIsNone(self.browser.group_identity)
                    self.assertEqual(self.unpaid_action.clicks, 0)

    def test_navigation_timeout_does_not_hide_unsafe_or_neutral_routes(self):
        for stage in ("continue", "payment-navigation"):
            for destination, expected_error in (
                (
                    "https://private-host.invalid/private-path?private-token",
                    tracker.TrackerConfigurationError,
                ),
                ("https://[malformed", tracker.TrackerConfigurationError),
                ("about:blank", tracker.SetupError),
                ("chrome-error://chromewebdata/", tracker.SetupError),
                (tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/payment", TimeoutException),
            ):
                driver = FakeDriver()
                action = driver.add_group("002222", "202")
                helpers = SimpleNamespace(**vars(self.helpers))
                helpers.login = Mock()
                helpers.get_chrome_driver = lambda driver=driver: (driver, None)
                browser = tracker.PaymentBrowser(self.config, helpers)

                def timeout(*args, driver=driver, destination=destination):
                    driver.current_url = destination
                    raise TimeoutException("private-timeout")

                if stage == "continue":
                    action.click = timeout
                else:
                    driver.get = timeout
                with self.subTest(stage=stage, destination=destination):
                    with self.assertRaises(expected_error) as raised:
                        browser.open()
                    self.assertEqual(raised.exception.browser_diagnostic[0], stage)
                    self.assertIsNone(browser.group_identity)
                    if expected_error is tracker.TrackerConfigurationError:
                        self.assertEqual(driver.quits, 1)
                        self.assertIsNone(browser.driver)
                    else:
                        self.assertEqual(driver.quits, 0)
                        self.assertIs(browser.driver, driver)
                    browser.close()

    def test_payment_navigation_timeout_cannot_hide_wrong_schedule(self):
        def timeout(url):
            self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/schedule/101/payment"
            raise TimeoutException("private-timeout")

        self.driver.get = timeout
        with self.assertRaises(tracker.TrackerConfigurationError) as raised:
            self.browser.open()
        self.assertEqual(
            raised.exception.browser_diagnostic,
            ("payment-navigation", "trusted-https"),
        )
        self.assertEqual(self.driver.quits, 1)
        self.assertIsNone(self.browser.group_identity)

    def test_neutral_pages_after_continue_and_payment_are_retryable(self):
        original_click = self.unpaid_action.click
        for stage in ("continue", "payment-navigation"):
            for destination in (
                "about:blank",
                "data:,",
                "chrome-error://chromewebdata/",
                "chrome://newtab/",
                "chrome://new-tab-page/",
                "",
            ):
                self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"
                self.driver.redirect = destination if stage == "payment-navigation" else None
                if stage == "continue":
                    self.unpaid_action.click = lambda destination=destination: setattr(
                        self.driver, "current_url", destination
                    )
                else:
                    self.unpaid_action.click = original_click
                with self.subTest(stage=stage, destination=destination):
                    with self.assertRaises(tracker.SetupError) as raised:
                        self.browser.open()
                    self.assertEqual(raised.exception.browser_diagnostic[0], stage)
                    self.assertIs(self.browser.driver, self.driver)
                    self.assertIsNone(self.browser.group_identity)
                    self.assertEqual(self.driver.quits, 0)

    def test_unsafe_continue_destination_remains_terminal(self):
        for destination in (
            "https://private-host.invalid/payment?private-token",
            "https://[malformed",
            "data:text/html,private-content",
        ):
            self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"
            self.unpaid_action.click = lambda destination=destination: setattr(
                self.driver, "current_url", destination
            )
            with self.subTest(destination=destination):
                with self.assertRaises(tracker.TrackerConfigurationError) as raised:
                    self.browser.open()
                self.assertEqual(raised.exception.browser_diagnostic[0], "continue")
                self.assertIsNone(self.browser.driver)
                self.assertIsNone(self.browser.group_identity)

    def test_login_blank_page_recovers_on_same_driver_without_duplicate_alert(self):
        observed = date(2026, 11, 6)
        state = tracker.AlertState(self.config, TEST_GROUP)
        state.mark_delivered(observed)
        saved = self.config.state_path.read_bytes()
        create = Mock(return_value=(self.driver, self.profile))
        self.helpers.get_chrome_driver = create

        def login(driver):
            if self.helpers.login.call_count == 1:
                driver.current_url = "chrome://newtab/"
                raise TimeoutException("private")
            driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"

        self.helpers.login.side_effect = login
        clock, logs, sender = FakeClock(), [], Mock()
        tracker.run_tracker(
            self.config,
            browser_factory=lambda _: self.browser,
            sender=sender,
            sleep=clock.sleep,
            clock=clock,
            log=logs.append,
            max_checks=1,
        )
        create.assert_called_once()
        self.assertEqual(self.helpers.login.call_count, 2)
        self.assertEqual(clock.delays, [self.config.fail_delay])
        self.assertEqual(self.driver.quits, 1)
        self.assertEqual(self.config.state_path.read_bytes(), saved)
        sender.assert_not_called()
        self.assertEqual(
            [line for line in logs if line.startswith("Browser failure:")],
            [
                "Browser failure: stage=login; route=new-tab; error=setup; recovery=same-browser",
            ],
        )
        self.assertNotIn("private", "\n".join(logs))

    def test_unexpected_login_route_recovers_with_fresh_driver_and_safe_logs(self):
        observed = date(2026, 11, 6)
        state = tracker.AlertState(self.config, TEST_GROUP)
        state.mark_delivered(observed)
        saved = self.config.state_path.read_bytes()
        self.driver.current_url = (
            "https://private-user:private-password@private-host.invalid/"
            "private-path?private-token#private-fragment"
        )
        self.helpers.login.side_effect = TimeoutException("private-exception")
        fresh = PollBrowser([observed])
        factory = Mock(side_effect=[self.browser, fresh])
        clock, logs, sender = FakeClock(), [], Mock()
        tracker.run_tracker(
            self.config,
            browser_factory=factory,
            sender=sender,
            sleep=clock.sleep,
            clock=clock,
            log=logs.append,
            max_checks=1,
        )
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(clock.delays, [self.config.new_session_delay])
        self.assertEqual(self.driver.quits, 1)
        self.assertFalse(self.profile.exists())
        self.assertEqual(fresh.opens, 1)
        self.assertEqual(fresh.closes, 1)
        self.assertEqual(self.config.state_path.read_bytes(), saved)
        sender.assert_not_called()
        self.assertEqual(
            [line for line in logs if line.startswith("Browser failure:")],
            [
                "Browser failure: stage=login; route=unexpected-authority; "
                "error=navigation; recovery=fresh-browser",
            ],
        )
        self.assertNotIn("private", "\n".join(logs))

    def test_terminal_payment_failure_has_one_sanitized_diagnostic(self):
        self.driver.redirect = "https://private-host.invalid/private-path?private-token"
        logs = []
        with self.assertRaises(tracker.TrackerConfigurationError):
            tracker.run_tracker(
                self.config,
                browser_factory=lambda _: self.browser,
                sender=Mock(),
                sleep=Mock(),
                clock=lambda: 0,
                log=logs.append,
                max_checks=1,
            )
        self.assertEqual(
            [line for line in logs if line.startswith("Browser failure:")],
            [
                "Browser failure: stage=payment-navigation; route=unexpected-authority; "
                "error=configuration; recovery=terminal",
            ],
        )
        self.assertNotIn("private", "\n".join(logs))

    def test_diagnostics_reject_untrusted_metadata(self):
        for diagnostic in (
            ("private-stage", "trusted-https"),
            ("login", "private-route"),
            ([], "trusted-https"),
            ("login", []),
            "private",
            ("login",),
            None,
        ):
            error = tracker.SetupError("private-exception")
            error.browser_diagnostic = diagnostic
            log = Mock()
            tracker.log_browser_diagnostic(error, log, "same-browser")
            log.assert_not_called()

    def test_matching_ivr_label_without_loaded_continue_is_a_transient_failure(self):
        self.driver.labels[1].children[(By.XPATH, booking._GROUP_ANCESTOR_XPATH)] = []
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertEqual(self.driver.quits, 0)
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(self.unpaid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])

    def test_rejected_credentials_are_terminal_but_unknown_login_alerts_are_not(self):
        self.driver.current_url = tracker.LOGIN_URL
        self.driver.errors = [Element("Invalid email or password.")]
        with self.assertRaisesRegex(tracker.TrackerConfigurationError, "rejected"):
            self.browser.open()
        self.assertEqual(self.driver.quits, 1)
        self.driver.errors = [Element("Temporarily unavailable")]
        with self.assertRaises(tracker.SetupError):
            self.browser.open()
        self.assertEqual(self.driver.quits, 1)

    def test_setup_challenge_is_detected_before_group_selection(self):
        self.driver.challenges = [Element()]
        with self.assertRaises(tracker.BrowserBlocked):
            self.browser.open()
        self.assertEqual(self.unpaid_action.clicks, 0)
        self.assertEqual(self.driver.urls, [])

    def test_setup_payment_redirect_to_another_group_or_site_is_terminal(self):
        for destination in (
            tracker.SITE_ORIGIN + "/en-ca/niv/schedule/101/payment",
            "https://evil.invalid/payment",
        ):
            self.driver.redirect = destination
            with self.subTest(destination=destination):
                with self.assertRaises(tracker.TrackerConfigurationError):
                    self.browser.open()
                self.assertIsNone(self.browser.group_identity)

    def test_same_group_or_dashboard_navigation_restores_only_payment_url(self):
        self.browser.open()
        payment_url = self.browser.payment_url
        for path in (
            "/en-ca/niv/schedule/202/continue_actions",
            "/en-ca/niv/schedule/202/appointment",
            "/en-ca/niv/groups",
        ):
            self.driver.current_url = tracker.SITE_ORIGIN + path
            self.assertEqual(self.browser.read_date(refresh=True), date(2026, 11, 6))
            self.assertEqual(self.driver.current_url, payment_url)
        self.assertEqual(self.driver.urls, [payment_url] * 4)
        self.assertEqual(self.driver.refreshes, 0)
        self.assertEqual(self.paid_action.clicks, 0)

    def test_wrong_group_and_off_site_navigation_are_not_inspected_or_refreshed(self):
        self.browser.open()
        for destination in (
            tracker.SITE_ORIGIN + "/en-ca/niv/schedule/101/payment",
            "https://evil.invalid/en-ca/niv/schedule/202/payment",
            "http://ais.usvisa-info.com/en-ca/niv/schedule/202/payment",
            "https://ais.usvisa-info.com@evil.invalid/payment",
            "https://[malformed",
        ):
            self.driver.current_url = destination
            with self.subTest(destination=destination):
                with patch.object(self.driver, "find_elements") as lookup:
                    with self.assertRaises(tracker.FreshBrowserRequired):
                        self.browser.read_date(refresh=True)
                    lookup.assert_not_called()
        self.assertEqual(self.driver.refreshes, 0)
        self.assertEqual(len(self.driver.urls), 1)

    def test_failed_restoration_requires_new_driver_without_reading_summary(self):
        self.browser.open()
        self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"
        self.driver.redirect = tracker.SITE_ORIGIN + "/en-ca/niv/schedule/202/appointment"
        with patch.object(tracker, "parse_availability_rows") as parse:
            with self.assertRaises(tracker.FreshBrowserRequired):
                self.browser.read_date(refresh=True)
            parse.assert_not_called()
        self.assertEqual(self.driver.refreshes, 0)

    def test_expiry_is_checked_before_any_refresh(self):
        self.browser.open()
        self.driver.current_url = tracker.LOGIN_URL
        with self.assertRaises(tracker.SessionExpired):
            self.browser.read_date(refresh=True)
        self.assertEqual(self.driver.refreshes, 0)

    def test_route_change_during_refresh_is_rejected(self):
        self.browser.open()

        def redirected_refresh():
            self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/schedule/101/payment"

        self.driver.refresh = redirected_refresh
        with patch.object(tracker, "parse_availability_rows") as parse:
            with self.assertRaises(tracker.FreshBrowserRequired):
                self.browser.read_date(refresh=True)
            parse.assert_not_called()

    def test_route_change_during_extraction_cannot_return_an_observation(self):
        self.browser.open()
        original = tracker.parse_availability_rows

        def changed_route(rows, consulate):
            self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"
            return original(rows, consulate)

        with patch.object(tracker, "parse_availability_rows", side_effect=changed_route):
            with self.assertRaises(tracker.FreshBrowserRequired):
                self.browser.read_date()

    def test_cleanup_removes_profile_even_if_quit_is_interrupted(self):
        self.browser.open()
        self.driver.quit = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.browser.close()
        self.assertFalse(self.profile.exists())
        self.assertIsNone(self.browser.driver)
        self.browser.close()
        self.driver.quit.assert_called_once()

    def test_real_browser_loop_recovers_manual_navigation_without_duplicate_alert(self):
        sender = Mock(return_value=TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"))

        def navigate_away(delay):
            self.driver.current_url = tracker.SITE_ORIGIN + "/en-ca/niv/groups"

        tracker.run_tracker(
            self.config,
            browser_factory=lambda _: self.browser,
            sender=sender,
            sleep=navigate_away,
            clock=lambda: 0,
            log=lambda _: None,
            max_checks=2,
        )
        sender.assert_called_once()
        self.assertEqual(self.driver.quits, 1)
        self.assertEqual(self.paid_action.clicks, 0)
        self.assertEqual(len(self.driver.urls), 2)


class FakeClock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def sleep(self, delay):
        if len(self.delays) >= 1000:
            raise AssertionError("Offline recovery test exceeded its iteration limit")
        self.delays.append(delay)
        self.now += delay

    def __call__(self):
        return self.now


class PollBrowser:
    def __init__(self, observations, open_error=None, group_identity=TEST_GROUP):
        self.observations = iter(observations)
        self.open_error = open_error
        self.group_identity = group_identity
        self.opens = 0
        self.closes = 0
        self.refreshes = []

    def open(self):
        self.opens += 1
        if self.open_error:
            raise self.open_error

    def read_date(self, refresh=False):
        self.refreshes.append(refresh)
        observed = next(self.observations)
        if isinstance(observed, BaseException):
            raise observed
        return observed

    def close(self):
        self.closes += 1


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = make_config(self.directory.name)
        self.clock = FakeClock()
        self.sender = Mock(return_value=TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"))
        self.logs = []
        shutdown = patch.object(
            tracker,
            "send_telegram_message",
            return_value=TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        )
        self.shutdown_sender = shutdown.start()
        self.addCleanup(shutdown.stop)

    def run_checks(self, observations, config=None, factory=None):
        browser = PollBrowser(observations)
        tracker.run_tracker(
            config or self.config,
            browser_factory=factory or (lambda _: browser),
            sender=self.sender,
            clock=self.clock,
            sleep=self.clock.sleep,
            log=self.logs.append,
            max_checks=len(observations),
        )
        return browser

    def test_alert_contains_city_date_and_reschedule_phone_line(self):
        for city, observed, expected in (
            ("Vancouver", date(2027, 1, 12), "Vancouver 2027-01-12"),
            ("Calgary", date(2026, 11, 6), "Calgary 2026-11-06"),
        ):
            with self.subTest(city=city):
                config = replace(self.config, consulate=city)
                self.assertEqual(tracker.format_alert(observed, config), expected + RESCHEDULE_LINE)

    def test_continues_after_match_and_alerts_every_date_change_in_both_directions(self):
        first, earlier = date(2026, 11, 6), date(2026, 10, 26)
        browser = self.run_checks(
            [
                first,
                first,
                date(2026, 11, 16),
                None,
                earlier,
                earlier,
                date(2026, 9, 30),
                date(2027, 1, 12),
            ]
        )
        self.assertEqual(self.sender.call_count, 5)
        self.assertEqual(
            self.sender.call_args_list[0].args[0], "Calgary 2026-11-06" + RESCHEDULE_LINE
        )
        self.assertEqual(
            self.sender.call_args_list[1].args[0], "Calgary 2026-11-16" + RESCHEDULE_LINE
        )
        self.assertEqual(
            self.sender.call_args_list[2].args[0], "Calgary 2026-10-26" + RESCHEDULE_LINE
        )
        self.assertEqual(
            self.sender.call_args_list[3].args[0], "Calgary 2026-09-30" + RESCHEDULE_LINE
        )
        self.assertEqual(
            self.sender.call_args_list[4].args[0], "Calgary 2027-01-12" + RESCHEDULE_LINE
        )
        self.assertEqual(browser.refreshes, [False] + [True] * 7)
        self.assertEqual(browser.closes, 1)
        self.assertEqual(self.clock.delays, [10] * 7)

    def test_later_in_range_date_alerts_immediately(self):
        self.run_checks([date(2026, 11, 6), date(2026, 11, 16)])
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(
            tracker.AlertState(self.config, TEST_GROUP).last_notified,
            date(2026, 11, 16),
        )

    def test_unavailability_is_recorded_and_next_date_alerts(self):
        self.run_checks([date(2026, 11, 6), None, date(2026, 11, 16)])
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(
            self.sender.call_args_list[1].args[0], "Calgary 2026-11-16" + RESCHEDULE_LINE
        )

    def test_unavailability_persists_across_restart_and_same_date_reappears(self):
        self.run_checks([date(2026, 11, 6), None])
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertEqual(state.last_notified, date(2026, 11, 6))
        self.assertIsNone(state.last_observed)
        self.assertFalse(state.pending_notification)
        self.run_checks([date(2026, 11, 6)])
        self.assertEqual(self.sender.call_count, 2)

    def test_out_of_window_date_and_returning_date_both_alert_with_no_policy(self):
        self.run_checks([date(2026, 11, 6), date(2026, 9, 30), date(2026, 11, 6)])
        self.assertEqual(self.sender.call_count, 3)

    def test_explicit_and_automatic_selection_share_history_for_same_actual_group(self):
        observed = date(2026, 11, 6)
        self.run_checks([observed])
        self.run_checks([observed], config=replace(self.config, unpaid_ivr=""))
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(
            self.config.fingerprint(TEST_GROUP),
            replace(self.config, unpaid_ivr="").fingerprint(TEST_GROUP),
        )

    def test_group_or_schedule_change_starts_a_fresh_baseline_on_restart(self):
        observed = date(2026, 11, 6)
        config = replace(self.config, unpaid_ivr="")
        self.run_checks([observed], config=config)
        for identity in (
            tracker.GroupIdentity("003333", "202"),
            tracker.GroupIdentity("003333", "303"),
        ):
            browser = PollBrowser([observed], group_identity=identity)
            self.run_checks([observed], config=config, factory=lambda _, browser=browser: browser)
        self.assertEqual(self.sender.call_count, 3)

    def test_session_renewal_rebinds_state_when_actual_group_changes(self):
        observed = date(2026, 11, 6)
        first = PollBrowser([observed])
        second_identity = tracker.GroupIdentity("003333", "303")
        second = PollBrowser([date(2026, 11, 16)], group_identity=second_identity)
        config = replace(self.config, unpaid_ivr="", session_age=5)
        self.run_checks(
            [observed, date(2026, 11, 16)],
            config=config,
            factory=Mock(side_effect=[first, second]),
        )
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(
            tracker.AlertState(config, second_identity).last_notified,
            date(2026, 11, 16),
        )

    def test_session_renewal_rebinds_state_for_new_schedule_with_same_ivr(self):
        observed = date(2026, 11, 6)
        first = PollBrowser([observed])
        second = PollBrowser([observed], group_identity=tracker.GroupIdentity("002222", "303"))
        self.run_checks(
            [observed, observed],
            config=replace(self.config, session_age=5),
            factory=Mock(side_effect=[first, second]),
        )
        self.assertEqual(self.sender.call_count, 2)

    def test_group_change_keeps_telegram_retry_delay_and_rechecks_current_date(self):
        self.sender.side_effect = [
            TelegramDeliveryResult(DeliveryStatus.RETRYABLE_FAILURE, "Rate limit", 25),
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        ]
        first = PollBrowser([date(2026, 11, 6), tracker.SessionExpired("Expired")])
        second_identity = tracker.GroupIdentity("003333", "303")
        second = PollBrowser(
            [date(2026, 11, 16), date(2026, 10, 26)], group_identity=second_identity
        )
        config = replace(self.config, unpaid_ivr="")
        self.run_checks(
            [None] * 3,
            config=config,
            factory=Mock(side_effect=[first, second]),
        )
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("2026-10-26", self.sender.call_args.args[0])
        self.assertEqual(
            tracker.AlertState(config, second_identity).last_notified, date(2026, 10, 26)
        )
        self.assertEqual(self.clock.now, 30)

    def test_restart_suppression_and_state_contains_no_personal_details(self):
        observed = date(2026, 11, 6)
        self.run_checks([observed])
        self.run_checks([observed])
        self.assertEqual(self.sender.call_count, 1)
        saved = self.config.state_path.read_text()
        for sensitive in (
            "test@example.invalid",
            "synthetic-password",
            "synthetic_test_token",
            '"ivr"',
            '"schedule_id"',
            '"002222"',
            '"202"',
        ):
            self.assertNotIn(sensitive, saved)
        self.assertEqual(json.loads(saved)["last_notified_date"], "2026-11-06")
        self.assertEqual(self.config.state_path.stat().st_mode & 0o777, 0o600)

    def test_configuration_change_resets_baseline_but_token_rotation_does_not(self):
        observed = date(2026, 11, 6)
        self.run_checks([observed])
        self.run_checks([observed], config=replace(self.config, bot_token="123:rotated_test_token"))
        self.assertEqual(self.sender.call_count, 1)
        self.run_checks([observed], config=replace(self.config, consulate="Toronto"))
        self.assertEqual(self.sender.call_count, 2)

    def test_rate_limit_delays_retry_and_rechecks_current_date(self):
        self.sender.side_effect = [
            TelegramDeliveryResult(DeliveryStatus.RETRYABLE_FAILURE, "Rate limit", 25),
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        ]
        self.run_checks(
            [date(2026, 11, 6), date(2026, 11, 6), date(2026, 10, 26), date(2026, 10, 26)]
        )
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("2026-10-26", self.sender.call_args.args[0])
        self.assertEqual(
            tracker.AlertState(self.config, TEST_GROUP).last_notified, date(2026, 10, 26)
        )

    def test_failed_delivery_does_not_advance_state_and_ineligible_date_is_not_resent(self):
        self.sender.return_value = TelegramDeliveryResult(DeliveryStatus.UNCERTAIN, "Uncertain")
        self.run_checks([date(2026, 11, 6), None])
        self.assertEqual(self.sender.call_count, 1)
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertIsNone(state.last_notified)
        self.assertIsNone(state.last_observed)
        self.assertFalse(state.pending_notification)

    def test_repeated_delivery_failures_enter_cooldown(self):
        self.sender.return_value = TelegramDeliveryResult(
            DeliveryStatus.RETRYABLE_FAILURE, "Offline"
        )
        self.run_checks([date(2026, 11, 6)] * 5, config=replace(self.config, max_failures=2))
        self.assertEqual(self.sender.call_count, 2)
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertIsNone(state.last_notified)
        self.assertTrue(state.pending_notification)

    def test_permanent_notification_failure_stops_without_marking_delivery(self):
        self.sender.return_value = TelegramDeliveryResult(
            DeliveryStatus.PERMANENT_FAILURE, "Invalid chat"
        )
        browser = PollBrowser([date(2026, 11, 6)])
        with self.assertRaises(tracker.TrackerConfigurationError):
            self.run_checks([date(2026, 11, 6)], factory=lambda _: browser)
        self.assertEqual(browser.closes, 1)
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertIsNone(state.last_notified)
        self.assertTrue(state.pending_notification)

    def test_expiry_never_reuses_dead_browser(self):
        expired = PollBrowser([tracker.SessionExpired("Expired")])
        fresh = PollBrowser([date(2026, 11, 6)])
        factory = Mock(side_effect=[expired, fresh])
        self.run_checks([date(2026, 11, 6)], factory=factory)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(expired.closes, 1)
        self.assertEqual(fresh.closes, 1)

    def test_challenge_gets_cooldown_and_does_not_send(self):
        blocked = PollBrowser([], open_error=tracker.BrowserBlocked("Challenge"))
        fresh = PollBrowser([None])
        self.run_checks([None], factory=Mock(side_effect=[blocked, fresh]))
        self.assertEqual(self.clock.delays, [100])
        self.sender.assert_not_called()

    def test_incomplete_or_out_of_window_saved_state_is_rejected(self):
        config = replace(self.config, alert_in_range_only=True)
        for saved in (
            {"version": 1},
            {
                "version": 1,
                "fingerprint": config.fingerprint(TEST_GROUP),
                "last_notified_date": "2025-01-01",
            },
            {"version": 1, "fingerprint": "invalid", "last_notified_date": "2026-11-06"},
        ):
            self.config.state_path.write_text(json.dumps(saved))
            with self.subTest(saved=saved), self.assertRaises(tracker.TrackerError):
                tracker.AlertState(config, TEST_GROUP)

    def test_driver_failure_logs_do_not_expose_private_browser_details(self):
        failed = PollBrowser([WebDriverException("synthetic-sensitive-page-data")])
        fresh = PollBrowser([None])
        self.run_checks([None], factory=Mock(side_effect=[failed, fresh]))
        self.assertNotIn("synthetic-sensitive-page-data", "\n".join(self.logs))
        self.assertIn("Chrome operation failed", "\n".join(self.logs))

    def test_session_age_forces_fresh_browser_without_resetting_baseline(self):
        first = PollBrowser([date(2026, 11, 6)])
        second = PollBrowser([date(2026, 10, 26)])
        self.run_checks(
            [date(2026, 11, 6), date(2026, 10, 26)],
            config=replace(self.config, session_age=5),
            factory=Mock(side_effect=[first, second]),
        )
        self.assertEqual(first.closes, 1)
        self.assertEqual(second.closes, 1)
        self.assertEqual(self.sender.call_count, 2)

    def test_extraction_error_does_not_reset_successful_baseline(self):
        self.run_checks([date(2026, 11, 6)])
        failed = PollBrowser([tracker.ExtractionError("Missing summary")])
        fresh = PollBrowser([date(2026, 11, 6)])
        self.run_checks(
            [date(2026, 11, 6)],
            config=replace(self.config, poll_max_retries=1),
            factory=Mock(side_effect=[failed, fresh]),
        )
        self.assertEqual(self.sender.call_count, 1)

    def test_keyboard_interrupt_still_closes_browser(self):
        browser = PollBrowser([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_checks([None], factory=lambda _: browser)
        self.assertEqual(browser.closes, 1)

    def test_test_mode_does_not_suppress_tracker_delivery(self):
        with patch.dict(os.environ, {"TEST_MODE": "True"}):
            self.run_checks([date(2026, 11, 6)])
        self.sender.assert_called_once()

    def test_invalid_state_stops_instead_of_sending_duplicate_alerts(self):
        self.config.state_path.write_text("not JSON")
        with self.assertRaises(tracker.TrackerError):
            self.run_checks([date(2026, 11, 6)])
        self.sender.assert_not_called()

    def test_state_write_failure_after_delivery_stops_monitor(self):
        original_replace = tracker.os.replace
        replacements = 0

        def fail_after_observation(source, destination):
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                raise OSError("synthetic failure")
            original_replace(source, destination)

        with patch("payment_tracker.os.replace", side_effect=fail_after_observation):
            with self.assertRaises(tracker.TrackerError):
                self.run_checks([date(2026, 11, 6)])
        self.sender.assert_called_once()
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertIsNone(state.last_notified)
        self.assertTrue(state.pending_notification)
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_default_no_reports_vancouver_date_outside_booking_window(self):
        config = replace(self.config, consulate="Vancouver")
        self.run_checks([date(2027, 1, 12), date(2027, 1, 12)], config=config)
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[0], "Vancouver 2027-01-12" + RESCHEDULE_LINE)

    def test_yes_policy_filters_initial_and_later_dates_but_not_change_direction(self):
        config = replace(self.config, alert_in_range_only=True)
        self.run_checks(
            [
                date(2027, 1, 12),
                date(2026, 11, 6),
                date(2026, 11, 16),
                date(2026, 10, 26),
                date(2026, 9, 30),
                date(2027, 1, 12),
            ],
            config=config,
        )
        self.assertEqual(
            [call.args[0] for call in self.sender.call_args_list],
            [
                message + RESCHEDULE_LINE
                for message in (
                    "Calgary 2026-11-06",
                    "Calgary 2026-11-16",
                    "Calgary 2026-10-26",
                )
            ],
        )
        self.assertIn("outside the range or excluded", "\n".join(self.logs))

    def test_yes_policy_returning_same_date_alerts_after_filtered_observation(self):
        config = replace(self.config, alert_in_range_only=True)
        self.run_checks(
            [
                date(2026, 11, 6),
                date(2026, 9, 30),
                date(2026, 11, 6),
            ],
            config=config,
        )
        self.assertEqual(self.sender.call_count, 2)

    def test_yes_policy_filtered_observation_survives_restart(self):
        config = replace(self.config, alert_in_range_only=True)
        self.run_checks([date(2026, 11, 6), date(2027, 1, 12)], config=config)
        state = tracker.AlertState(config, TEST_GROUP)
        self.assertEqual(state.last_notified, date(2026, 11, 6))
        self.assertEqual(state.last_observed, date(2027, 1, 12))
        self.assertFalse(state.pending_notification)
        self.run_checks([date(2026, 11, 6), date(2026, 11, 6)], config=config)
        self.assertEqual(self.sender.call_count, 2)

    def test_exclusion_filter_only_applies_with_yes_policy(self):
        config = replace(self.config, exclusions=((date(2026, 10, 10), date(2026, 10, 20)),))
        observations = [date(2026, 11, 6), date(2026, 10, 15), date(2026, 11, 6)]
        self.run_checks(observations, config=config)
        self.assertEqual(self.sender.call_count, 3)
        self.sender.reset_mock()
        self.run_checks(observations, config=replace(config, alert_in_range_only=True))
        self.assertEqual(self.sender.call_count, 2)

    def test_initial_and_repeated_unavailability_never_send_a_date_message(self):
        self.run_checks([None, None])
        self.sender.assert_not_called()
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertTrue(state.has_observation)
        self.assertIsNone(state.last_observed)
        self.assertIsNone(state.last_notified)
        self.assertFalse(state.pending_notification)

    def test_failed_first_delivery_stays_pending_and_retries_after_restart(self):
        observed = date(2027, 1, 12)
        self.sender.return_value = TelegramDeliveryResult(DeliveryStatus.UNCERTAIN, "Uncertain")
        self.run_checks([observed])
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertIsNone(state.last_notified)
        self.assertEqual(state.last_observed, observed)
        self.assertTrue(state.pending_notification)
        self.sender.return_value = TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered")
        self.run_checks([observed, observed])
        self.assertEqual(self.sender.call_count, 2)
        self.assertFalse(tracker.AlertState(self.config, TEST_GROUP).pending_notification)

    def test_failed_later_change_preserves_confirmed_date_until_retry(self):
        self.run_checks([date(2026, 11, 6)])
        self.sender.return_value = TelegramDeliveryResult(
            DeliveryStatus.RETRYABLE_FAILURE, "Offline"
        )
        self.run_checks([date(2026, 11, 16)])
        state = tracker.AlertState(self.config, TEST_GROUP)
        self.assertEqual(state.last_notified, date(2026, 11, 6))
        self.assertEqual(state.last_observed, date(2026, 11, 16))
        self.assertTrue(state.pending_notification)
        self.sender.return_value = TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered")
        self.run_checks([date(2026, 11, 16)])
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(
            tracker.AlertState(self.config, TEST_GROUP).last_notified, date(2026, 11, 16)
        )

    def test_rate_limit_is_honored_across_filtered_and_returning_dates(self):
        config = replace(self.config, alert_in_range_only=True)
        self.sender.side_effect = [
            TelegramDeliveryResult(DeliveryStatus.RETRYABLE_FAILURE, "Rate limit", 25),
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        ]
        self.run_checks(
            [
                date(2026, 11, 6),
                date(2027, 1, 12),
                date(2026, 11, 6),
                date(2026, 11, 6),
            ],
            config=config,
        )
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.clock.now, 30)
        self.assertEqual(tracker.AlertState(config, TEST_GROUP).last_notified, date(2026, 11, 6))

    def test_return_to_last_delivered_date_is_a_change_even_after_failed_alert(self):
        self.sender.side_effect = [
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
            TelegramDeliveryResult(DeliveryStatus.RETRYABLE_FAILURE, "Offline"),
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        ]
        self.run_checks([date(2026, 11, 6), date(2026, 11, 16), date(2026, 11, 6)])
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.sender.call_args.args[0], "Calgary 2026-11-06" + RESCHEDULE_LINE)

    def test_range_policy_change_starts_fresh_tracking(self):
        observed = date(2026, 11, 6)
        self.run_checks([observed])
        self.run_checks([observed], config=replace(self.config, alert_in_range_only=True))
        self.assertEqual(self.sender.call_count, 2)

    def test_legacy_state_is_read_and_upgraded_on_next_change(self):
        self.config.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fingerprint": self.config.fingerprint(TEST_GROUP),
                    "last_notified_date": "2026-11-06",
                }
            )
        )
        self.run_checks([date(2026, 11, 6), date(2026, 11, 16)])
        self.sender.assert_called_once()
        self.assertEqual(json.loads(self.config.state_path.read_text())["version"], 2)

    def test_old_policy_fingerprint_starts_a_fresh_baseline(self):
        self.config.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fingerprint": "0" * 64,
                    "last_notified_date": "2026-11-06",
                }
            )
        )
        self.run_checks([date(2026, 11, 6)])
        self.sender.assert_called_once()

    def test_invalid_version_two_observation_and_pending_state_are_rejected(self):
        valid = {
            "version": 2,
            "fingerprint": self.config.fingerprint(TEST_GROUP),
            "last_notified_date": None,
            "last_observed_date": "2026-11-06",
            "pending_notification": True,
        }
        for overrides in (
            {"version": True},
            {"last_observed_date": "2026-02-30"},
            {"last_notified_date": "invalid"},
            {"pending_notification": "yes"},
            {"last_observed_date": None},
        ):
            self.config.state_path.write_text(json.dumps({**valid, **overrides}))
            with self.subTest(overrides=overrides), self.assertRaises(tracker.TrackerError):
                tracker.AlertState(self.config, TEST_GROUP)
        del valid["pending_notification"]
        self.config.state_path.write_text(json.dumps(valid))
        with self.assertRaises(tracker.TrackerError):
            tracker.AlertState(self.config, TEST_GROUP)

    def test_observation_write_failure_stops_before_any_message_is_sent(self):
        with patch("payment_tracker.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(tracker.TrackerError):
                self.run_checks([date(2026, 11, 6)])
        self.sender.assert_not_called()
        self.assertFalse(self.config.state_path.exists())
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_reset_baseline_flag_clears_state_and_reruns(self):
        self.run_checks([date(2026, 11, 6)])
        self.assertTrue(self.config.state_path.exists())
        with (
            patch.object(tracker.TrackerConfig, "from_settings", return_value=self.config),
            patch.object(tracker, "run_tracker") as run,
        ):
            self.assertEqual(tracker.main(["--reset-baseline"]), 0)
            run.assert_called_once()
        self.assertFalse(self.config.state_path.exists())
        self.run_checks([date(2026, 11, 6)])
        self.assertEqual(self.sender.call_count, 2)

    def test_reset_baseline_flag_without_state_still_runs(self):
        with (
            patch.object(tracker.TrackerConfig, "from_settings", return_value=self.config),
            patch.object(tracker, "run_tracker") as run,
        ):
            self.assertEqual(tracker.main(["--reset-baseline"]), 0)
            run.assert_called_once()

    def test_main_without_flag_preserves_state(self):
        self.run_checks([date(2026, 11, 6)])
        with (
            patch.object(tracker.TrackerConfig, "from_settings", return_value=self.config),
            patch.object(tracker, "run_tracker"),
        ):
            self.assertEqual(tracker.main([]), 0)
        self.assertTrue(self.config.state_path.exists())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = replace(make_config(self.directory.name), new_session_delay=7)
        self.clock = FakeClock()
        self.sender = Mock(return_value=TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"))
        self.logs = []
        for target in ("reschedule.webdriver.Chrome", "requests.sessions.Session.request"):
            guard = patch(target, side_effect=AssertionError("Live operation forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def run_monitor(self, browsers, *, config=None, checks=1):
        factory = Mock(side_effect=browsers)
        tracker.run_tracker(
            config or self.config,
            browser_factory=factory,
            sender=self.sender,
            sleep=self.clock.sleep,
            clock=self.clock,
            log=self.logs.append,
            max_checks=checks,
        )
        return factory

    def test_transient_setup_errors_reuse_one_driver(self):
        browser = PollBrowser([None])
        browser.open = Mock(
            side_effect=[
                TimeoutException("private"),
                StaleElementReferenceException("private"),
                None,
            ]
        )
        factory = self.run_monitor([browser])
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(browser.open.call_count, 3)
        self.assertEqual(browser.refreshes, [False])
        self.assertEqual(self.clock.delays, [10, 10])
        self.assertEqual(browser.closes, 1)

    def test_setup_attempt_limit_replaces_driver_without_an_extra_attempt(self):
        failed = PollBrowser([], open_error=tracker.SetupError("private"))
        fresh = PollBrowser([None])
        factory = self.run_monitor(
            [failed, fresh],
            config=replace(self.config, setup_max_attempts=3),
        )
        self.assertEqual(failed.opens, 3)
        self.assertEqual(failed.refreshes, [])
        self.assertEqual(failed.closes, 1)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(self.clock.delays, [10, 10, 7])

    def test_15_setup_failures_across_three_drivers_trigger_one_cooldown(self):
        failed = [PollBrowser([], open_error=tracker.SetupError("private")) for _ in range(3)]
        fresh = PollBrowser([None])
        self.run_monitor([*failed, fresh])
        self.assertEqual([browser.opens for browser in failed], [5, 5, 5])
        self.assertEqual([browser.closes for browser in failed], [1, 1, 1])
        self.assertEqual(self.clock.delays, [10] * 4 + [7] + [10] * 4 + [7] + [10] * 4 + [100])
        self.assertEqual(sum("Cooldown:" in message for message in self.logs), 1)

    def test_15_browser_creation_failures_are_supervised_and_cool_down(self):
        self.run_monitor([*[OSError("private")] * 15, PollBrowser([None])])
        self.assertEqual(self.clock.delays, [7] * 14 + [100])
        self.assertNotIn("private", "\n".join(self.logs))

    def test_15_early_route_failures_across_drivers_trigger_one_cooldown(self):
        failures = []
        for _ in range(15):
            error = tracker.FreshBrowserRequired("private")
            error.browser_diagnostic = ("dashboard", "unexpected-authority")
            failures.append(PollBrowser([], open_error=error))
        self.run_monitor([*failures, PollBrowser([None])])
        self.assertEqual([browser.opens for browser in failures], [1] * 15)
        self.assertEqual([browser.closes for browser in failures], [1] * 15)
        self.assertEqual(self.clock.delays, [7] * 14 + [100])
        diagnostics = [line for line in self.logs if line.startswith("Browser failure:")]
        self.assertEqual(len(diagnostics), 15)
        self.assertEqual(sum("recovery=fresh-browser" in line for line in diagnostics), 14)
        self.assertIn("recovery=cooldown", diagnostics[-1])
        self.assertEqual(sum("Cooldown:" in line for line in self.logs), 1)
        self.assertNotIn("private", "\n".join(self.logs))

    def test_first_summary_failures_do_not_reset_setup_streak(self):
        first = PollBrowser([tracker.ExtractionError("private")] * 5)
        first.open = Mock(side_effect=[tracker.SetupError("private")] * 2 + [None])
        second = PollBrowser([tracker.ExtractionError("private")] * 5)
        third = PollBrowser([tracker.ExtractionError("private")] * 3)
        self.run_monitor([first, second, third, PollBrowser([None])])
        self.assertEqual(len(third.refreshes), 3)
        self.assertEqual(self.clock.delays.count(100), 1)
        self.assertIn("15 consecutive", "\n".join(self.logs))

    def test_successful_read_resets_the_setup_failure_streak(self):
        first = PollBrowser([None, tracker.SessionExpired("private")])
        first.open = Mock(side_effect=[tracker.SetupError("private")] * 2 + [None])
        second = PollBrowser([None])
        second.open = Mock(side_effect=[tracker.SetupError("private")] * 2 + [None])
        with patch.object(tracker, "SETUP_FAILURE_SOFT_BAN_THRESHOLD", 3):
            self.run_monitor([first, second], checks=2)
        self.assertNotIn(100, self.clock.delays)
        self.assertEqual(first.open.call_count, 3)
        self.assertEqual(second.open.call_count, 3)

    def test_polling_errors_retry_the_same_browser_and_refresh(self):
        browser = PollBrowser(
            [
                TimeoutException("private"),
                StaleElementReferenceException("private"),
                tracker.ExtractionError("private"),
                None,
            ]
        )
        self.run_monitor([browser])
        self.assertEqual(browser.opens, 1)
        self.assertEqual(browser.refreshes, [False, True, True, True])
        self.assertEqual(self.clock.delays, [10, 10, 10])

    def test_polling_retry_limit_replaces_driver_after_exactly_configured_failures(self):
        failed = PollBrowser([tracker.ExtractionError("private")] * 3)
        self.run_monitor(
            [failed, PollBrowser([None])],
            config=replace(self.config, poll_max_retries=3),
        )
        self.assertEqual(len(failed.refreshes), 3)
        self.assertEqual(failed.closes, 1)
        self.assertEqual(self.clock.delays, [10, 10, 7])

    def test_polling_time_budget_replaces_driver_before_another_read(self):
        failed = PollBrowser([tracker.ExtractionError("private")])
        self.run_monitor(
            [failed, PollBrowser([None])],
            config=replace(self.config, poll_max_time=5),
        )
        self.assertEqual(failed.refreshes, [False])
        self.assertEqual(self.clock.delays, [10, 7])
        self.assertIn("failure-time budget exhausted", "\n".join(self.logs))

    def test_healthy_unavailable_unchanged_and_filtered_reads_are_exempt(self):
        for observed in (None, date(2026, 11, 6), date(2027, 1, 12)):
            browser = PollBrowser([observed] * 100)
            factory = self.run_monitor(
                [browser],
                config=replace(
                    self.config, poll_max_time=1, poll_max_retries=1, alert_in_range_only=True
                ),
                checks=100,
            )
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(len(browser.refreshes), 100)
            self.assertNotIn(100, self.clock.delays)

    def test_successful_polls_do_not_erase_previous_poll_failures(self):
        failed = PollBrowser(
            [tracker.ExtractionError("private"), None, None, tracker.ExtractionError("private")]
        )
        self.run_monitor(
            [failed, PollBrowser([None])],
            config=replace(self.config, poll_max_retries=2),
            checks=3,
        )
        self.assertEqual(len(failed.refreshes), 4)
        self.assertEqual(self.clock.delays, [10, 10, 10, 7])

    def test_dead_drivers_expiry_and_failed_restoration_never_retry_same_instance(self):
        for error in (
            InvalidSessionIdException("private"),
            NoSuchWindowException("private"),
            WebDriverException("private"),
            tracker.SessionExpired("private"),
            tracker.FreshBrowserRequired("private"),
        ):
            before = len(self.clock.delays)
            failed = PollBrowser([error])
            self.run_monitor([failed, PollBrowser([None])])
            self.assertEqual(failed.refreshes, [False])
            self.assertEqual(failed.closes, 1)
            self.assertEqual(self.clock.delays[before:], [7])
        self.assertNotIn("private", "\n".join(self.logs))

    def test_expiry_and_dead_driver_during_setup_skip_same_driver_retries(self):
        for error in (tracker.SessionExpired("private"), InvalidSessionIdException("private")):
            failed = PollBrowser([], open_error=error)
            self.run_monitor([failed, PollBrowser([None])])
            self.assertEqual(failed.opens, 1)
            self.assertEqual(failed.closes, 1)

    def test_unexpected_setup_read_and_creation_errors_relaunch_safely(self):
        for failed in (
            OSError("private"),
            PollBrowser([], open_error=ValueError("private")),
            PollBrowser([RuntimeError("private")]),
        ):
            before = len(self.clock.delays)
            self.run_monitor([failed, PollBrowser([None])])
            self.assertEqual(self.clock.delays[before:], [7])
        self.assertNotIn("private", "\n".join(self.logs))

    def test_access_block_in_polling_closes_driver_before_one_cooldown(self):
        blocked = PollBrowser([tracker.BrowserBlocked("private")])
        self.run_monitor([blocked, PollBrowser([None])])
        self.assertEqual(blocked.closes, 1)
        self.assertEqual(blocked.refreshes, [False])
        self.assertEqual(self.clock.delays, [100])
        self.sender.assert_not_called()

    def test_maximum_age_waits_new_session_delay_and_keeps_baseline(self):
        observed = date(2026, 11, 6)
        first, second = PollBrowser([observed]), PollBrowser([observed])
        self.run_monitor(
            [first, second],
            config=replace(self.config, session_age=5),
            checks=2,
        )
        self.assertEqual(self.clock.delays, [10, 7])
        self.sender.assert_called_once()

    def test_telegram_rate_limit_and_pending_date_survive_session_recovery(self):
        self.sender.side_effect = [
            TelegramDeliveryResult(DeliveryStatus.RETRYABLE_FAILURE, "Rate limit", 50),
            TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        ]
        first = PollBrowser([date(2026, 11, 6), tracker.SessionExpired("private")])
        second = PollBrowser([date(2026, 11, 16)] * 5)
        self.run_monitor([first, second], checks=6)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.sender.call_args.args[0], "Calgary 2026-11-16" + RESCHEDULE_LINE)
        self.assertEqual(self.clock.now, 57)
        self.assertFalse(tracker.AlertState(self.config, TEST_GROUP).pending_notification)

    def test_terminal_configuration_and_state_errors_are_not_retried(self):
        for error in (
            tracker.TrackerConfigurationError("Safe configuration error"),
            tracker.TrackerError("Safe state error"),
        ):
            failed = PollBrowser([], open_error=error)
            with self.assertRaises(type(error)):
                self.run_monitor([failed])
            self.assertEqual(failed.opens, 1)
            self.assertEqual(failed.closes, 1)
        self.assertEqual(self.clock.delays, [])

    def test_state_load_failure_after_a_healthy_read_is_terminal(self):
        self.config.state_path.write_text("not JSON")
        failed = PollBrowser([date(2026, 11, 6)])
        with self.assertRaises(tracker.TrackerError):
            self.run_monitor([failed])
        self.assertEqual(failed.closes, 1)
        self.assertEqual(self.clock.delays, [])
        self.sender.assert_not_called()

    def test_interrupt_during_setup_closes_driver_without_retry(self):
        failed = PollBrowser([], open_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_monitor([failed])
        self.assertEqual(failed.closes, 1)
        self.assertEqual(self.clock.delays, [])

    def test_interrupt_during_retry_renewal_cooldown_or_healthy_wait_never_relaunches(self):
        for browser in (
            PollBrowser([], open_error=tracker.SetupError("private")),
            PollBrowser([tracker.ExtractionError("private")]),
            PollBrowser([tracker.SessionExpired("private")]),
            PollBrowser([tracker.BrowserBlocked("private")]),
            PollBrowser([None, None]),
        ):
            factory = Mock(return_value=browser)
            with self.assertRaises(KeyboardInterrupt):
                tracker.run_tracker(
                    self.config,
                    browser_factory=factory,
                    sender=self.sender,
                    sleep=Mock(side_effect=KeyboardInterrupt()),
                    clock=self.clock,
                    log=self.logs.append,
                    max_checks=2,
                )
            factory.assert_called_once()
            self.assertEqual(browser.closes, 1)

    def test_cleanup_errors_do_not_prevent_relaunch_or_leak_private_details(self):
        failed = PollBrowser([tracker.SessionExpired("private")])
        failed.close = Mock(side_effect=RuntimeError("private"))
        self.run_monitor([failed, PollBrowser([None])])
        failed.close.assert_called_once()
        self.assertNotIn("private", "\n".join(self.logs))


class ShutdownNotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = make_config(self.directory.name)
        self.clock = FakeClock()
        self.logs = []
        self.actual_runner = tracker.run_tracker
        for target in ("reschedule.webdriver.Chrome", "requests.sessions.Session.request"):
            guard = patch(target, side_effect=AssertionError("Live operation forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        sender = patch.object(
            tracker,
            "send_telegram_message",
            return_value=TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered"),
        )
        self.sender = sender.start()
        self.addCleanup(sender.stop)
        logger = patch.object(tracker, "log_message", side_effect=self.logs.append)
        logger.start()
        self.addCleanup(logger.stop)

    def run_cli(self, browsers, *, checks=1, sleep=None, appointment_sender=None):
        factory = Mock(side_effect=browsers)

        def run(config):
            return self.actual_runner(
                config,
                browser_factory=factory,
                sender=appointment_sender or Mock(),
                max_checks=checks,
                sleep=sleep or self.clock.sleep,
                clock=self.clock,
                log=self.logs.append,
            )

        with patch.object(tracker.TrackerConfig, "from_settings", return_value=self.config):
            with patch.object(tracker, "run_tracker", side_effect=run):
                return tracker.main([])

    def assert_stop_sent(self):
        self.sender.assert_called_once_with(
            "Monitoring Stopped",
            bot_token=self.config.bot_token,
            chat_id=self.config.chat_id,
            timeout=tracker.STOP_NOTIFICATION_TIMEOUT,
        )

    def test_terminal_exit_sends_exact_message_after_browser_cleanup(self):
        failed = PollBrowser([], open_error=tracker.TrackerConfigurationError("Safe failure"))

        def send(*args, **kwargs):
            self.assertEqual(failed.closes, 1)
            return TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered")

        self.sender.side_effect = send
        self.assertEqual(self.run_cli([failed]), 1)
        self.assert_stop_sent()
        self.assertFalse(self.config.state_path.exists())

    def test_ctrl_c_sends_once_after_cleanup_without_retry(self):
        for error_at in ("setup", "poll", "cleanup"):
            self.sender.reset_mock()
            failed = PollBrowser(
                [KeyboardInterrupt()] if error_at == "poll" else [None],
                open_error=KeyboardInterrupt() if error_at == "setup" else None,
            )
            if error_at == "cleanup":
                original_close = failed.close

                def close(original_close=original_close):
                    original_close()
                    raise KeyboardInterrupt()

                failed.close = close

            def send(*args, failed=failed, **kwargs):
                self.assertEqual(failed.closes, 1)
                return TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered")

            self.sender.side_effect = send
            with self.subTest(error_at=error_at):
                self.assertEqual(self.run_cli([failed]), 0)
                self.assert_stop_sent()
                self.assertEqual(self.clock.delays, [])

    def test_recovery_and_cooldown_do_not_send_stop_until_program_exits(self):
        blocked = PollBrowser([tracker.BrowserBlocked("private")])
        expired = PollBrowser([tracker.SessionExpired("private")])
        healthy = PollBrowser([None])

        def sleep(delay):
            self.sender.assert_not_called()
            self.clock.sleep(delay)

        def send(*args, **kwargs):
            self.assertEqual([browser.closes for browser in (blocked, expired, healthy)], [1] * 3)
            return TelegramDeliveryResult(DeliveryStatus.SENT, "Delivered")

        self.sender.side_effect = send
        self.assertEqual(self.run_cli([blocked, expired, healthy], sleep=sleep), 0)
        self.assertEqual(self.clock.delays, [self.config.cooldown, self.config.new_session_delay])
        self.assert_stop_sent()
        self.assertNotIn("private", "\n".join(self.logs))

    def test_interrupt_during_retry_or_cooldown_sends_once(self):
        for error in (tracker.SetupError("private"), tracker.BrowserBlocked("private")):
            self.sender.reset_mock()
            failed = PollBrowser([], open_error=error)
            with self.subTest(error=type(error).__name__):
                self.assertEqual(
                    self.run_cli([failed], sleep=Mock(side_effect=KeyboardInterrupt())),
                    0,
                )
                self.assertEqual(failed.closes, 1)
                self.assert_stop_sent()

    def test_invalid_startup_configuration_does_not_send(self):
        with (
            patch.object(
                tracker.TrackerConfig,
                "from_settings",
                side_effect=tracker.TrackerConfigurationError("Invalid configuration"),
            ),
            patch.object(tracker, "run_tracker") as run,
        ):
            self.assertEqual(tracker.main([]), 1)
        run.assert_not_called()
        self.sender.assert_not_called()

    def test_reset_failure_before_monitoring_starts_does_not_send(self):
        with patch.object(tracker.TrackerConfig, "from_settings", return_value=self.config):
            with patch.object(Path, "unlink", side_effect=OSError("private")):
                self.assertEqual(tracker.main(["--reset-baseline"]), 1)
        self.sender.assert_not_called()
        self.assertNotIn("private", "\n".join(self.logs))

    def test_unhandled_exception_keeps_original_error_and_sends_after_cleanup(self):
        failed = PollBrowser([date(2026, 11, 6)])
        error = RuntimeError("private-unhandled-error")
        with self.assertRaises(RuntimeError) as raised:
            self.run_cli([failed], appointment_sender=Mock(side_effect=error))
        self.assertIs(raised.exception, error)
        self.assertEqual(failed.closes, 1)
        self.assert_stop_sent()
        self.assertTrue(tracker.AlertState(self.config, TEST_GROUP).pending_notification)
        self.assertNotIn("private", "\n".join(self.logs))

    def test_shutdown_delivery_failures_are_not_retried_or_logged_raw(self):
        for status in (
            DeliveryStatus.PERMANENT_FAILURE,
            DeliveryStatus.RETRYABLE_FAILURE,
            DeliveryStatus.UNCERTAIN,
        ):
            self.sender.reset_mock()
            self.sender.return_value = TelegramDeliveryResult(status, "private-reason", 600)
            self.config.state_path.write_text("corrupt state")
            with self.subTest(status=status):
                self.assertEqual(self.run_cli([PollBrowser([None])]), 1)
                self.assert_stop_sent()
                self.assertEqual(self.config.state_path.read_text(), "corrupt state")
                self.assertEqual(self.clock.delays, [])
        self.assertNotIn("private", "\n".join(self.logs))

    def test_shutdown_send_exceptions_do_not_change_exit_status_or_state(self):
        state = tracker.AlertState(self.config, TEST_GROUP)
        state.mark_delivered(date(2026, 11, 6))
        saved = self.config.state_path.read_bytes()
        for error in (RuntimeError("private-send-error"), KeyboardInterrupt()):
            for stop_error, expected_status in (
                (KeyboardInterrupt(), 0),
                (tracker.TrackerConfigurationError("Safe failure"), 1),
            ):
                self.sender.reset_mock()
                self.sender.side_effect = error
                with self.subTest(send_error=type(error).__name__, exit_status=expected_status):
                    self.assertEqual(
                        self.run_cli([PollBrowser([], open_error=stop_error)]),
                        expected_status,
                    )
                    self.assert_stop_sent()
                    self.assertEqual(self.config.state_path.read_bytes(), saved)
                    self.assertEqual(self.clock.delays, [])
        self.assertNotIn("private", "\n".join(self.logs))

    def test_stop_log_failure_does_not_mask_original_exit_status(self):
        def log(message):
            if message.startswith("Telegram stop notification"):
                raise OSError("private-log-error")
            self.logs.append(message)

        with patch.object(tracker, "log_message", side_effect=log):
            failed = PollBrowser([], open_error=tracker.TrackerConfigurationError("Safe failure"))
            self.assertEqual(self.run_cli([failed]), 1)
        self.assert_stop_sent()


class RequestBudgetTests(unittest.TestCase):
    def test_injected_clock_and_log_preserve_count_and_time_boundaries(self):
        clock, logs = FakeClock(), []
        budget = tracker.RequestTracker(2, 10, clock=clock, log=logs.append)
        budget.retry()
        budget.retry()
        clock.now = 10
        self.assertTrue(budget.should_retry())
        clock.now = 11
        self.assertFalse(budget.should_retry())
        self.assertIn("Max time reached", logs)
        clock.now = 0
        budget.retry()
        self.assertFalse(budget.should_retry())
        self.assertIn("Max retries reached", logs)

    def test_forgiveness_preserves_previous_failure_and_refunds_healthy_time(self):
        clock = FakeClock()
        budget = tracker.RequestTracker(2, 10, clock=clock, log=lambda _: None)
        budget.retry()
        clock.now = 5
        budget.retry()
        clock.now = 1005
        budget.forgive_last_retry(1000)
        self.assertEqual(budget.retries, 1)
        self.assertTrue(budget.should_retry())
        clock.now = 1011
        self.assertFalse(budget.should_retry())

    def test_existing_paid_constructor_uses_wall_clock_and_default_logs(self):
        with (
            patch("request_tracker.time.time", return_value=123),
            patch("builtins.print") as output,
        ):
            budget = tracker.RequestTracker(0, 1)
            self.assertEqual(budget.start_time, 123)
            budget.retry()
            self.assertFalse(budget.should_retry())
            self.assertIn("Max retries reached", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
