import os
import runpy
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By

# Import application settings without reading private .env or shell credentials.
with patch.dict(os.environ, {"USER_CONSULATE": "Calgary"}, clear=True), patch("dotenv.load_dotenv"):
    import reschedule

# All IVR numbers and schedule IDs below are synthetic test fixtures.


class ImmediateWait:
    def __init__(self, scope, timeout):
        self.scope = scope

    def until(self, condition):
        for _ in range(3):
            result = condition(self.scope)
            if result:
                return result
        raise TimeoutException("condition did not become true")


class FakeAction:
    def __init__(self, callback, kind="link", enabled=True):
        self.callback = callback
        self.kind = kind
        self.enabled = enabled
        self.clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.clicks += 1
        self.callback()


class FakeLabel:
    def __init__(self, group, number, visible=True):
        self.group = group
        self.text = f"IVR Account Number: {number}"
        self.visible = visible

    def is_displayed(self):
        return self.visible

    def find_elements(self, by, value):
        if (by, value) == (By.XPATH, reschedule._GROUP_ANCESTOR_XPATH):
            return [self.group] if self.group is not None else []
        raise AssertionError(f"unexpected label locator: {by}, {value}")


class FakeGroup:
    def __init__(self, driver, number, schedule_id, appointment=None, kind="link"):
        self.driver = driver
        self.schedule_id = schedule_id
        self.text = f"Current Status\nIVR Account Number: {number}"
        self.appointments = (
            [SimpleNamespace(text=f"Consular Appointment: {appointment}")] if appointment else []
        )
        self.action = FakeAction(self.select, kind)
        self.label = FakeLabel(self, number)

    def select(self):
        self.driver.selected_id = self.schedule_id
        self.driver.current_url = (
            f"https://ais.usvisa-info.com/en-ca/niv/schedule/{self.schedule_id}/continue_actions"
        )

    def find_elements(self, by, value):
        if (by, value) == (By.CSS_SELECTOR, "p.consular-appt"):
            return self.appointments
        if (by, value) == (By.LINK_TEXT, "Continue"):
            return [self.action] if self.action.kind == "link" else []
        if (by, value) == (By.XPATH, ".//button[normalize-space()='Continue']"):
            return [self.action] if self.action.kind == "button" else []
        if (by, value) == (By.XPATH, ".//input[@value='Continue']"):
            return [self.action] if self.action.kind == "input" else []
        raise AssertionError(f"unexpected group locator: {by}, {value}")


class FakeDriver:
    def __init__(self):
        self.current_url = "https://ais.usvisa-info.com/en-ca/niv/groups"
        self.selected_id = None
        self.groups = []
        self.labels = []
        self.visited_urls = []
        self.quits = 0
        self.schedule_action = FakeAction(self.schedule)

    def add_group(self, number, schedule_id, appointment=None, kind="link"):
        group = FakeGroup(self, number, schedule_id, appointment, kind)
        self.groups.append(group)
        self.labels.append(group.label)
        return group

    def schedule(self):
        schedule_id = self.selected_id or "999"
        self.current_url = (
            f"https://ais.usvisa-info.com/en-ca/niv/schedule/{schedule_id}/appointment"
        )

    def find_elements(self, by, value):
        if (by, value) == (By.XPATH, reschedule._IVR_LABEL_XPATH):
            return self.labels
        if (by, value) == (By.CSS_SELECTOR, "p.consular-appt"):
            return [element for group in self.groups for element in group.appointments]
        if (by, value) == (By.LINK_TEXT, "Continue"):
            return [group.action for group in self.groups]
        if (by, value) == (By.LINK_TEXT, "Schedule Appointment"):
            return [self.schedule_action] if self.selected_id else []
        if by == By.XPATH and value.startswith(".//"):
            return []
        raise AssertionError(f"unexpected driver locator: {by}, {value}")

    def get(self, url):
        self.current_url = url
        self.visited_urls.append(url)

    def quit(self):
        self.quits += 1


class IvrSettingsTests(unittest.TestCase):
    def load_settings(self, value):
        env = {"USER_CONSULATE": "Calgary"}
        if value is not None:
            env["PAID_IVR_ACCOUNT_NUMBER"] = value
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
            return runpy.run_path(str(Path(reschedule.__file__).with_name("settings.py")))

    def test_optional_missing_and_blank_values(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(self.load_settings(value)["PAID_IVR_ACCOUNT_NUMBER"], "")

    def test_trims_whitespace_and_preserves_leading_zeros(self):
        self.assertEqual(self.load_settings(" 00000001 ")["PAID_IVR_ACCOUNT_NUMBER"], "00000001")

    def test_rejects_non_ascii_digits(self):
        for value in ("123abc", "123-456", "１２３４"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError, "PAID_IVR_ACCOUNT_NUMBER must contain only digits"
                ),
            ):
                self.load_settings(value)


class IvrGroupSelectionTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("PAID_IVR_ACCOUNT_NUMBER", "22222222"),
            ("WebDriverWait", ImmediateWait),
            ("sleep", lambda _: None),
            ("log_message", lambda _: None),
        ):
            patcher = patch.object(reschedule, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.driver = FakeDriver()
        self.first = self.driver.add_group("11111111", "101", "5 October, 2026")
        self.target = self.driver.add_group("22222222", "202", "6 November, 2026")

    def test_clicks_second_groups_continue(self):
        reschedule.get_appointment_page(self.driver)
        self.assertEqual(self.first.action.clicks, 0)
        self.assertEqual(self.target.action.clicks, 1)
        self.assertEqual(
            self.driver.visited_urls,
            ["https://ais.usvisa-info.com/en-ca/niv/schedule/202/appointment"],
        )

    def test_reads_only_target_groups_appointment(self):
        self.assertEqual(reschedule.get_current_appointment_date(self.driver), date(2026, 11, 6))

    def test_label_and_number_can_be_in_separate_elements(self):
        self.target.label.text = "IVR Account Number:"
        self.assertIs(reschedule._find_dashboard_group(self.driver), self.target)

    def test_target_without_appointment_does_not_read_another_groups_date(self):
        self.target.appointments = []
        self.assertIsNone(reschedule.get_current_appointment_date(self.driver))

    def test_empty_setting_preserves_first_continue_and_date(self):
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", ""):
            self.assertEqual(
                reschedule.get_current_appointment_date(self.driver), date(2026, 10, 5)
            )
            reschedule.get_appointment_page(self.driver)
        self.assertEqual(self.first.action.clicks, 1)
        self.assertEqual(self.target.action.clicks, 0)
        self.assertIn("/schedule/101/appointment", self.driver.current_url)

    def test_empty_setting_preserves_direct_schedule_flow(self):
        self.driver.selected_id = "303"
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", ""):
            reschedule.get_appointment_page(self.driver)
        self.assertIn("/schedule/303/appointment", self.driver.current_url)
        self.assertEqual(self.first.action.clicks + self.target.action.clicks, 0)

    def test_configured_ivr_does_not_click_unrelated_global_schedule_action(self):
        self.driver.selected_id = "999"
        reschedule.get_appointment_page(self.driver)
        self.assertEqual(self.target.action.clicks, 1)
        self.assertIn("/schedule/202/appointment", self.driver.current_url)

    def test_matches_whole_number_not_substring(self):
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", "2222222"):
            self.assertFalse(reschedule._find_dashboard_group(self.driver))

    def test_leading_zeros_match_exactly(self):
        group = self.driver.add_group("00000001", "303")
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", "00000001"):
            self.assertIs(reschedule._get_dashboard_scope(self.driver), group)

    def test_missing_match_never_falls_back_to_another_group(self):
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", "33333333"):
            for operation in (
                reschedule.get_appointment_page,
                reschedule.get_current_appointment_date,
            ):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(
                        TimeoutException, "Could not find a unique dashboard group"
                    ),
                ):
                    operation(self.driver)
        self.assertEqual(self.first.action.clicks + self.target.action.clicks, 0)
        self.assertEqual(self.driver.visited_urls, [])

    def test_duplicate_match_is_rejected(self):
        self.driver.add_group("22222222", "303")
        for operation in (
            reschedule.get_appointment_page,
            reschedule.get_current_appointment_date,
        ):
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaisesRegex(RuntimeError, "Multiple dashboard groups"),
            ):
                operation(self.driver)
        self.assertEqual(sum(group.action.clicks for group in self.driver.groups), 0)

    def test_hidden_matching_label_is_not_selected(self):
        self.target.label.visible = False
        self.assertFalse(reschedule._find_dashboard_group(self.driver))

    def test_stale_label_is_retried(self):
        with patch.object(
            self.target.label,
            "is_displayed",
            side_effect=[StaleElementReferenceException(), True],
        ):
            self.assertIs(reschedule._get_dashboard_scope(self.driver), self.target)

    def test_duplicate_label_reference_does_not_make_group_ambiguous(self):
        self.driver.labels.append(self.target.label)
        self.assertIs(reschedule._find_dashboard_group(self.driver), self.target)

    def test_container_spanning_multiple_groups_is_not_selected(self):
        self.target.text += "\nIVR Account Number: 11111111"
        self.assertFalse(reschedule._find_dashboard_group(self.driver))

    def test_missing_group_ancestor_is_not_selected(self):
        self.target.label.group = None
        self.assertFalse(reschedule._find_dashboard_group(self.driver))

    def test_scoped_continue_supports_buttons_and_inputs(self):
        for kind in ("button", "input"):
            with self.subTest(kind=kind):
                self.target.action.kind = kind
                reschedule.get_appointment_page(self.driver)
        self.assertEqual(self.first.action.clicks, 0)
        self.assertEqual(self.target.action.clicks, 2)

    def test_disabled_continue_does_not_fall_back(self):
        self.target.action.enabled = False
        with self.assertRaisesRegex(TimeoutException, "Could not find Continue"):
            reschedule.get_appointment_page(self.driver)
        self.assertEqual(self.first.action.clicks + self.target.action.clicks, 0)
        self.assertEqual(self.driver.schedule_action.clicks, 0)

    def verify(self, target_date):
        with (
            patch.object(reschedule, "get_chrome_driver", return_value=(self.driver, "unused")),
            patch.object(reschedule, "login"),
            patch.object(reschedule, "VERIFY_MAX_READS", 1),
            patch.object(reschedule.shutil, "rmtree"),
        ):
            return reschedule.verify_booking_after_unverified(date(2026, 10, 1), target_date)

    def test_verification_uses_target_group(self):
        self.assertEqual(self.verify(date(2026, 11, 6)), "success")
        self.assertEqual(self.driver.quits, 1)
        self.assertEqual(self.first.action.clicks + self.target.action.clicks, 0)

    def test_verification_does_not_accept_another_groups_date(self):
        self.assertEqual(self.verify(date(2026, 10, 5)), "unknown")

    def test_verification_missing_group_is_unknown(self):
        with patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", "33333333"):
            self.assertEqual(self.verify(date(2026, 10, 5)), "unknown")

    def test_setup_failure_never_reaches_polling(self):
        with (
            patch.object(reschedule, "PAID_IVR_ACCOUNT_NUMBER", "33333333"),
            patch.object(reschedule, "get_chrome_driver", return_value=(self.driver, "unused")),
            patch.object(reschedule, "login"),
            patch.object(reschedule, "reschedule") as poll,
            patch.object(reschedule, "_consecutive_setup_failures", 0),
            patch.object(reschedule, "NEW_SESSION_AFTER_FAILURES", 1),
            patch.object(reschedule.shutil, "rmtree"),
        ):
            self.assertEqual(reschedule.reschedule_with_new_session(), (False, False))
            poll.assert_not_called()
        self.assertEqual(self.driver.quits, 1)


if __name__ == "__main__":
    unittest.main()
