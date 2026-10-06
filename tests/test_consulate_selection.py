import os
import unittest
from unittest.mock import patch

# Import application settings without reading private .env or shell credentials.
with patch.dict(os.environ, {"USER_CONSULATE": "Calgary"}, clear=True), patch("dotenv.load_dotenv"):
    import legacy_rescheduler
    from settings import resolve_consulate


class FakeOption:
    def __init__(self, text, value):
        self.text = text
        self.value = str(value)

    def get_attribute(self, name):
        return self.value if name == "value" else None


class FakeSelect:
    def __init__(self, options, selected_value):
        self.options = options
        self.selected_value = str(selected_value)
        self.selection_calls = []

    @property
    def first_selected_option(self):
        return next(option for option in self.options if option.value == self.selected_value)

    def select_by_value(self, value):
        self.selection_calls.append(str(value))
        if not any(option.value == str(value) for option in self.options):
            raise RuntimeError("missing option")
        self.selected_value = str(value)


class FakeDriver:
    def __init__(self, select, delayed_attempts=0):
        self.select = select
        self.delayed_attempts = delayed_attempts
        self.find_attempts = 0

    def find_element(self, by, value):
        if value == "appointments_consulate_appointment_facility_id":
            self.find_attempts += 1
            if self.find_attempts <= self.delayed_attempts:
                raise RuntimeError("not loaded")
            return self.select
        return object()


class ImmediateWait:
    def __init__(self, driver, timeout):
        self.driver = driver
        self.timeout = timeout

    def until(self, condition):
        last_result = False
        for _ in range(5):
            last_result = condition(self.driver)
            if last_result:
                return last_result
        raise TimeoutError("condition did not become true")


def fake_select(element):
    return element


class ConsulateSettingsTests(unittest.TestCase):
    def test_resolve_consulate_accepts_case_and_whitespace(self):
        self.assertEqual(resolve_consulate(" calGARY "), ("Calgary", 89))

    def test_resolve_consulate_rejects_invalid_value(self):
        with self.assertRaisesRegex(ValueError, "Unsupported USER_CONSULATE"):
            resolve_consulate("Edmonton")

    def test_resolve_consulate_rejects_missing_value(self):
        with self.assertRaisesRegex(ValueError, "USER_CONSULATE is missing"):
            resolve_consulate(None)


class ConsulateBrowserSelectionTests(unittest.TestCase):
    def setUp(self):
        self.options = [FakeOption("Toronto", 94), FakeOption("Calgary", 89)]

    def run_helper(self, selected_value, delayed_attempts=0):
        select = FakeSelect(self.options, selected_value)
        driver = FakeDriver(select, delayed_attempts)
        with (
            patch.object(legacy_rescheduler, "WebDriverWait", ImmediateWait),
            patch.object(legacy_rescheduler, "Select", fake_select),
            patch.object(
                legacy_rescheduler.EC,
                "element_to_be_clickable",
                return_value=lambda current_driver: object(),
            ),
        ):
            changed = legacy_rescheduler._select_configured_consulate(driver, timeout=1)
        return changed, select, driver

    def test_selects_configured_consulate(self):
        changed, select, _ = self.run_helper(94)
        self.assertTrue(changed)
        self.assertEqual(select.selected_value, "89")
        self.assertEqual(select.selection_calls, ["89"])

    def test_does_not_reselect_correct_consulate(self):
        changed, select, _ = self.run_helper(89)
        self.assertFalse(changed)
        self.assertEqual(select.selection_calls, [])

    def test_waits_for_delayed_dropdown(self):
        changed, select, driver = self.run_helper(94, delayed_attempts=2)
        self.assertTrue(changed)
        self.assertEqual(select.selected_value, "89")
        self.assertGreaterEqual(driver.find_attempts, 3)

    def test_fails_if_configured_facility_is_absent(self):
        select = FakeSelect([FakeOption("Toronto", 94)], 94)
        driver = FakeDriver(select)
        with (
            patch.object(legacy_rescheduler, "WebDriverWait", ImmediateWait),
            patch.object(legacy_rescheduler, "Select", fake_select),
        ):
            with self.assertRaisesRegex(RuntimeError, "Calgary facility 89 is not available"):
                legacy_rescheduler._select_configured_consulate(driver, timeout=1)


if __name__ == "__main__":
    unittest.main()
