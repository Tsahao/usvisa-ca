from dotenv import load_dotenv
import os
from datetime import datetime
from pathlib import Path
# Load environment variables
load_dotenv(Path(__file__).with_name(".env"))

# Account Info
USER_EMAIL = os.getenv("USER_EMAIL")
USER_PASSWORD = os.getenv("USER_PASSWORD")
# Optional paid booking selector; preserve leading zeros.
PAID_IVR_ACCOUNT_NUMBER = os.getenv("PAID_IVR_ACCOUNT_NUMBER", "").strip()
if PAID_IVR_ACCOUNT_NUMBER and (
    not PAID_IVR_ACCOUNT_NUMBER.isascii() or not PAID_IVR_ACCOUNT_NUMBER.isdecimal()
):
    raise ValueError("PAID_IVR_ACCOUNT_NUMBER must contain only digits, or be left blank")
# Validate tracker-only settings at tracker startup, not when booking imports us.
UNPAID_IVR_ACCOUNT_NUMBER = os.getenv("UNPAID_IVR_ACCOUNT_NUMBER", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Tracker-only policy; validate at tracker startup so paid booking is unaffected.
FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY = os.getenv(
    "FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY", "NO"
).strip()
try:
    NUM_PARTICIPANTS = int(os.getenv("NUM_PARTICIPANTS", "1"))
except (TypeError, ValueError):
    NUM_PARTICIPANTS = 1

# Say you want an appointment no later than Mar 14, 2024
# Please strictly follow the YYYY-MM-DD format for all dates

EARLIEST_ACCEPTABLE_DATE = os.getenv("EARLIEST_ACCEPTABLE_DATE")
LATEST_ACCEPTABLE_DATE = os.getenv("LATEST_ACCEPTABLE_DATE")

# Date exclusion ranges
EXCLUSION_DATE_RANGES = []
if EARLIEST_ACCEPTABLE_DATE and LATEST_ACCEPTABLE_DATE:
    try:
        earliest_acceptable_date = datetime.strptime(EARLIEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        latest_acceptable_date = datetime.strptime(LATEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        
        for i in range(1, 10):  # Support up to 9 exclusion ranges
            start = os.getenv(f"EXCLUSION_START_DATE_{i}")
            end = os.getenv(f"EXCLUSION_END_DATE_{i}")
            if start and end:
                try:
                    exclusion_start_date = datetime.strptime(start, "%Y-%m-%d").date()
                    exclusion_end_date = datetime.strptime(end, "%Y-%m-%d").date()
                    if (exclusion_start_date < exclusion_end_date and 
                        exclusion_start_date > earliest_acceptable_date and 
                        exclusion_end_date < latest_acceptable_date):
                        EXCLUSION_DATE_RANGES.append((start, end))
                except ValueError:
                    print(f"Invalid date format in exclusion range {start} to {end}")
    except ValueError:
        print("Invalid date format in EARLIEST_ACCEPTABLE_DATE or LATEST_ACCEPTABLE_DATE")

# Your consulate's city
CONSULATES = {
    "Calgary": 89,
    "Halifax": 90,
    "Montreal": 91,
    "Ottawa": 92,
    "Quebec": 93,
    "Toronto": 94,
    "Vancouver": 95
} # Only Toronto and Vancouver consulates are verified


def resolve_consulate(value):
    """Validate a configured consulate and return its canonical name and ID."""
    name = (value or "").strip()
    if not name:
        raise ValueError(
            "USER_CONSULATE is missing. Set it in .env to one of: "
            + ", ".join(CONSULATES)
        )

    canonical_names = {city.casefold(): city for city in CONSULATES}
    canonical_name = canonical_names.get(name.casefold())
    if canonical_name is None:
        raise ValueError(
            f"Unsupported USER_CONSULATE={name!r}. Choose one of: "
            + ", ".join(CONSULATES)
        )
    return canonical_name, CONSULATES[canonical_name]


# Use one validated facility for both API requests and the browser form.
USER_CONSULATE, USER_CONSULATE_ID = resolve_consulate(
    os.getenv("USER_CONSULATE")
)

# The following is only required for the Gmail notification feature
# Gmail login info
GMAIL_SENDER_NAME = os.getenv("GMAIL_SENDER_NAME")
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_APPLICATION_PWD = os.getenv("GMAIL_APPLICATION_PWD")

# Email notification receiver info
RECEIVER_NAME = os.getenv("RECEIVER_NAME")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")

# Override with local, for developers
# from local import *

# See the automation in action
SHOW_GUI = True  # toggle to false if you don't want to see the browser

# If you just want to see the program run WITHOUT clicking the confirm reschedule button
# For testing, also set a date really far away so the app actually tries to reschedule
TEST_MODE = os.getenv("TEST_MODE", "True").strip().lower() in ("1", "true", "yes", "on")

# When True (default), only book a slot strictly earlier than your currently
# booked appointment. Set to False to allow booking any in-window date --
# e.g. moving to a later date at a different consulate.
# Set in .env (project root) as: ONLY_EARLIER_THAN_CURRENT_APPOINTMENT="True"/"False"
ONLY_EARLIER_THAN_CURRENT_APPOINTMENT = os.getenv(
    "ONLY_EARLIER_THAN_CURRENT_APPOINTMENT",
    os.getenv("REQUIRE_EARLIER_DATE", "True"),
).strip().lower() in ("1", "true", "yes", "on")
# Legacy alias (old .env name) -- do not use in new code.
REQUIRE_EARLIER_DATE = ONLY_EARLIER_THAN_CURRENT_APPOINTMENT

# When True (default), an UnverifiedReschedule (confirm clicked but no
# success signal) is re-checked with a fresh login reading the dashboard's
# booked date: == target => success/quit, == previous => silent continue
# with a new polling session, anything else => manual-check email + quit.
# Set in .env as: VERIFY_UNVERIFIED_BOOKING="True"/"False"
VERIFY_UNVERIFIED_BOOKING = os.getenv(
    "VERIFY_UNVERIFIED_BOOKING", "True"
).strip().lower() in ("1", "true", "yes", "on")
try:
    VERIFY_MAX_READS = int(os.getenv("VERIFY_MAX_READS", "3"))
except (TypeError, ValueError):
    VERIFY_MAX_READS = 3
try:
    VERIFY_READ_DELAY = int(os.getenv("VERIFY_READ_DELAY", "10"))
except (TypeError, ValueError):
    VERIFY_READ_DELAY = 10

# Don't change the following unless you know what you are doing
DETACH = False
NEW_SESSION_AFTER_FAILURES = 5
NEW_SESSION_DELAY = 60
TIMEOUT = 10
try:
    FAIL_RETRY_DELAY = int(os.getenv("FAIL_RETRY_DELAY", "180"))
except (TypeError, ValueError):
    FAIL_RETRY_DELAY = 180
try:
    DATE_REQUEST_DELAY = int(os.getenv("DATE_REQUEST_DELAY", "180"))
except (TypeError, ValueError):
    DATE_REQUEST_DELAY = 180
try:
    DATE_REQUEST_JITTER = int(os.getenv("DATE_REQUEST_JITTER", "30"))
except (TypeError, ValueError):
    DATE_REQUEST_JITTER = 30
DATE_REQUEST_MAX_RETRY = 5
DATE_REQUEST_MAX_TIME = 15 * 60
# Max wall-clock age of a healthy polling session, even if every poll is
# forgiven (healthy-but-unusable). Forgiven polls refund RequestTracker time,
# so without this a session could live for hours on one login. Observed
# logouts ~60min, so 90min forces a proactive fresh login as a backstop.
try:
    MAX_HEALTHY_SESSION_AGE = int(os.getenv("MAX_HEALTHY_SESSION_AGE", str(90 * 60)))
except (TypeError, ValueError):
    MAX_HEALTHY_SESSION_AGE = 90 * 60
try:
    SOFT_BAN_COOLDOWN = int(os.getenv("SOFT_BAN_COOLDOWN", "3600"))
except (TypeError, ValueError):
    SOFT_BAN_COOLDOWN = 60 * 60
LOGIN_URL = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"
AVAILABLE_DATE_REQUEST_SUFFIX = f"/days/{USER_CONSULATE_ID}.json?appointments[expedite]=false"
APPOINTMENT_PAGE_URL = "https://ais.usvisa-info.com/en-ca/niv/schedule/{id}/appointment"
PAYMENT_PAGE_URL = "https://ais.usvisa-info.com/en-ca/niv/schedule/{id}/payment"
REQUEST_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
