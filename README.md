# US Visa Rescheduler for Canada

Python tools for booking paid US visa interview appointments in Canada and
tracking the earliest dates shown before payment.

## Choose the entry point

- `python3 reschedule.py`: paid appointment booking/rescheduling; existing
  Gmail notifications and booking `TEST_MODE` behavior are unchanged.
- `python3 payment_tracker.py`: unpaid payment-page summary checks and Telegram
  alerts only. It never submits payments, books, or reschedules.

Both commands load this repository's `.env`, sharing login credentials,
consulate, date window, exclusions, and polling settings. Shell environment
variables take precedence over `.env`.

## Update

- The core functionality is still working (as of **March 2026**) according to users' report
- Gmail-related code might error out, but it doesn't block rescheduling
- Adopt this repo: this project is looking for a new maintainer, open an issue if you'd like to adopt it.

## Features

- Automatically checks for available visa interview slots at your selected consulate
- Supports multiple consulate locations across Canada
- Configurable date ranges for appointment scheduling
- Email notifications when appointments are found or rescheduled
- Support for excluding specific date ranges
- Headless operation mode for unattended running
- Test mode for safe testing without actual rescheduling
- Automatic retry mechanism with configurable delays
- Support for multiple applicants in a single appointment

## Prerequisites

- Python 3.10 or newer installed
- Google Chrome installed (the script drives Chrome via Selenium; it will fail without a Chrome binary — Safari/Firefox/Edge are not supported)
- For rescheduling: a paid US visa application on https://ais.usvisa-info.com/en-ca/
- For payment tracking: an unpaid group that already exposes the payment page's
  **First Available Appointments** summary
- Gmail for paid-flow notifications (optional); a Telegram bot and chat for the tracker

## Installation

1. Clone this repository
2. Install dependencies:

```sh
pip install -r requirements.txt
```

Supported Consulate locations:

```python
CONSULATES = {
    "Calgary": 89,
    "Halifax": 90,
    "Montreal": 91,
    "Ottawa": 92,
    "Quebec": 93,
    "Toronto": 94,
    "Vancouver": 95
} # Only Toronto and Vancouver consulates are verified
```

Add a new `.env` file to the root of the project, this file will be used to configure parameters for the script. You can use the following parameters:

```
USER_EMAIL=""   # The email address for your https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
USER_PASSWORD=""    # The password for your  https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
PAID_IVR_ACCOUNT_NUMBER=""    # Optional: the group for paid booking/rescheduling
UNPAID_IVR_ACCOUNT_NUMBER=""    # Optional only for a single dashboard group; otherwise set its exact IVR
TELEGRAM_BOT_TOKEN=""    # Required for payment_tracker.py; keep private
TELEGRAM_CHAT_ID=""    # Destination for payment-tracker alerts
FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY="NO"    # Tracker only: YES filters alerts by the date window/exclusions
EARLIEST_ACCEPTABLE_DATE="" # The earliest interview date you are looking for
LATEST_ACCEPTABLE_DATE=""   # The latest acceptable interview date
USER_CONSULATE="" # Use one of the cosulate names from above
NUM_PARTICIPANTS="1"  # Number of applicants on the appointment (default 1). All are kept selected on reschedule.
GMAIL_SENDER_NAME=""    # Name of sender on email
GMAIL_EMAIL=""  # Sender email account
GMAIL_APPLICATION_PWD=""    # Use the app password you generated for application -- check https://support.google.com/mail/answer/185833?hl=en
RECEIVER_NAME=""    # Recipient name
RECEIVER_EMAIL=""   # Recipient email
EXCLUSION_START_DATE_1=""   # Start date for first excluded date range
EXCLUSION_END_DATE_1=""     # End date for first excluded date range
EXCLUSION_START_DATE_2=""   # Start date for second excluded date range
EXCLUSION_END_DATE_2=""     # End date for second excluded date range
DATE_REQUEST_DELAY="180"    # Seconds to wait between availability checks (default 180)
DATE_REQUEST_JITTER="30"    # Extra random 0-N seconds added to each wait so polling looks less bot-like (default 30)
FAIL_RETRY_DELAY="180"    # Seconds to wait between failed appointment-page setup retries (default 180)
SOFT_BAN_COOLDOWN="3600"    # Seconds to cool down when an empty date list signals a soft-ban (default 3600 = 1 hour)
TEST_MODE="True"    # "True" = dry run (never clicks confirm), "False" = live booking
ONLY_EARLIER_THAN_CURRENT_APPOINTMENT="True"    # In .env (project root): "True" (default) = only book strictly earlier than your current appointment; "False" = allow any in-window date (e.g. later date at a different consulate)
```

You can add upto 9 exclusion date ranges. Each date range to be excluded using the syntax `EXCLUSION_START_DATE_{i}` and `EXCLUSION_END_DATE_{i}` where `i` can be replaced by numbers between 1 to 9.

If your account has multiple groups, set `PAID_IVR_ACCOUNT_NUMBER` to the digits
shown under **IVR Account Number** in the group you want to reschedule.
The script clicks that group's Continue and uses only that group's current appointment
date, including during post-booking verification. If the configured number
cannot be uniquely matched, it does not select another group or attempt a
booking. Omit this setting or leave it blank to retain first-Continue behavior.

### Find a slot and book it automatically

```sh
python3 reschedule.py
```

See the script in action. Once you're satisfied with its functionality, set `TEST_MODE` to `False` in `settings.py`. For a headless operation, you can also set `SHOW_GUI` to `False` and allow the script to run unattended.

### Track dates before payment

Set `UNPAID_IVR_ACCOUNT_NUMBER` to the exact digits for your unpaid group if
your dashboard has multiple groups. Leading zeros are preserved; a configured
selector must match exactly one card, with no fallback to another group.

Omit it or leave it blank to automatically select the sole visible,
unambiguously identified dashboard group. Multiple groups (even if only one
is unpaid), duplicate cards, or ambiguous group identities require an explicit
selector. The tracker never uses `PAID_IVR_ACCOUNT_NUMBER` as a fallback and
refuses to select a group matching that paid selector. The selected group must
still expose its payment route and **First Available Appointments** summary.
An absent booked appointment does not prove that a group is unpaid.

Use your existing `USER_CONSULATE` and valid date preferences from `settings.py`.
The tracker reads only the selected consulate from the **First Available
Appointments** table, not the full calendar.

`FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY` controls all unpaid-tracker date alerts:
- `NO` (default, including when omitted/blank): report the earliest displayed
  date regardless of the booking window or exclusions.
- `YES`: report only dates inside the inclusive `EARLIEST_ACCEPTABLE_DATE` /
  `LATEST_ACCEPTABLE_DATE` window and outside configured exclusion ranges.

"First appointment" means the earliest date on the payment page, not only the
startup alert. This setting never changes paid booking criteria.

Telegram setup:

1. Create a bot using Telegram's official `@BotFather`; save its token privately
   as `TELEGRAM_BOT_TOKEN` in `.env`.
2. Open the bot's chat and send `/start`, or add the bot to the target group
   and send a message there. Ensure the bot can send to that destination.
3. Obtain the destination's `chat.id` from the bot's `getUpdates` response using
   a trusted local client that keeps the token private. Group IDs are often
   negative; use the chat ID, not a message ID or your username.
4. Save it as `TELEGRAM_CHAT_ID`. Do not paste tokens into issue reports,
   terminal output, shared URLs, or screenshots.

Run:

```sh
python3 payment_tracker.py
python3 payment_tracker.py --reset-baseline
```

**This sends real Telegram alerts even when `TEST_MODE=True`.** That setting
belongs to the booking flow and is deliberately ignored by the read-only tracker.
Missing tracker/Telegram settings do not prevent running `reschedule.py`.

Messages contain the city and date followed by a rescheduling phone line:

```text
Vancouver 2027-01-12
Call +1 (778) 807-9660 to reschedule.
```

The tracker sends the first date allowed by the alert policy, then alerts for
every observed date change, whether earlier or later. Direction never depends
on `FIRST_APPOINTMENT_ALERT_IN_RANGE_ONLY`; only date eligibility does.
Repeated unchanged dates are suppressed. If an unavailable or filtered date
intervenes, a previously reported date returning alerts again, even across
restarts. Explicit unavailability is recorded but sends no city/date message;
missing/malformed summaries and read failures never count as a date change.

Polling continues using `DATE_REQUEST_DELAY` plus `DATE_REQUEST_JITTER`, with
no special pause after an alert. Changes occurring entirely between polls
cannot be detected.

Successful delivery state is saved atomically in `.payment_tracker_state.json`
with private file permissions. This ignored file contains only an opaque
configuration fingerprint, last observed and last delivered dates, and a
pending-alert flag. Only confirmed delivery advances the last delivered date.
The fingerprint uses the actual selected IVR and schedule ID, rechecked when
Chrome sessions renew. Changing account, group/schedule, consulate, date
preferences, exclusions, alert policy, bot identity, or destination starts a
fresh baseline; switching between explicit and automatic
selection of the same group, or rotating the same bot's token, does not.
Existing version-one files remain readable; the new alert-policy fingerprint
starts fresh tracking once after this update. Run one tracker process per
state file.
To intentionally reset the baseline, stop the tracker and run once with
`--reset-baseline`, or remove its state file.

Failed notifications do not advance the baseline. Transient failures use
bounded backoff/cooldowns; a delayed resend first rechecks the current date.
Pending alerts survive restart, but obsolete intermediate dates are not queued.
Telegram rate-limit delays are honored. Invalid token/chat or TLS configuration
errors stop the tracker for correction. A network timeout can leave delivery
uncertain, so a retry may produce a duplicate.

Chrome sessions are reused and renewed after expiry or the configured maximum
age. The tracker refreshes the payment page before each subsequent check; it
does not rely on the website automatically updating already-rendered text.
Manual navigation within the same group, or back to the dashboard, returns to
the previously validated payment URL. Navigation to another group or website
discards that browser and re-establishes the unpaid group with a fresh login;
no date is read from the unexpected page.

Recovery uses the same settings and layered model as the paid program:
- Transient login/dashboard/payment setup failures retry on the same usable
  driver, up to `NEW_SESSION_AFTER_FAILURES` attempts (5 in `settings.py`),
  waiting `FAIL_RETRY_DELAY` between attempts. Known blank/new-tab/browser-error
  pages and an empty current URL are transient throughout setup, not successful
  login/payment validation or proof of invalid configuration.
- Unexpected or malformed routes during login/dashboard setup close Chrome
  and start a fresh session from the trusted login URL after `NEW_SESSION_DELAY`.
  The unexpected destination is never accepted as successful setup. Once group
  selection/Continue/payment validation begins, unsafe destinations and wrong
  schedule identities remain terminal.
- Transient summary reads, stale elements, and page-load timeouts retry on
  the same driver. `DATE_REQUEST_MAX_RETRY` (5 failures) and
  `DATE_REQUEST_MAX_TIME` (15 minutes of unrefunded failure time) bound polling
  per driver. Healthy reads and their waits do not consume this budget,
  including unchanged, filtered, and `No Appointments Available` observations.
- Expired/dead sessions, exhausted budgets, unsuccessful page restoration, and
  `MAX_HEALTHY_SESSION_AGE` cause Chrome/profile cleanup and a fresh login after
  `NEW_SESSION_DELAY` (60 seconds in `settings.py`). Browser creation failures
  and unexpected browser-operation errors also participate in recovery.
- Access-denied, rate-limit, and challenge pages immediately close Chrome and
  wait `SOFT_BAN_COOLDOWN`, without bypass attempts. Fifteen consecutive
  setup/re-establishment failures across drivers also trigger this cooldown
  as a suspected soft ban or service outage. Only a healthy summary read or
  cooldown resets that streak; merely opening Chrome or reaching the payment
  route does not. The cooldown replaces, rather than stacks with, the normal
  new-session delay.

The paid calendar's four-empty-list soft-ban rule is **not** applied to the
payment summary. Website recovery is separate from Telegram backoff/rate
limits and preserves the alert baseline and pending delivery.
Invalid settings, explicit credential rejection, unsafe/ambiguous group
selection, corrupt/unwritable alert state, and permanent Telegram errors still
stop for correction. A login timeout alone is retryable, not proof of bad
credentials. Existing retry-count/time and new-session constants are configured
in `settings.py`; no new unpaid-specific `.env` variables are required.

Browser setup failures and navigation trust failures include one sanitized
diagnostic per failed attempt: a fixed stage (`login`, `dashboard`, `continue`,
`payment-navigation`, etc.), route category, error category, and recovery action.
Logs appear in the terminal only; no log file is created. They do not expose
arbitrary URLs/hosts, query strings, page titles, account details, credentials,
tokens, or raw browser exceptions. The shared login helper is unchanged.

Ctrl-C, including during retry/cooldown waits, closes Chrome and its temporary
profile and exits without relaunch. Recovery replaces browsers inside the
running Python process; it does not restart a killed Python process or survive
an OS shutdown. The tracker never navigates to the calendar itself or submits
payment forms.

After monitoring starts with valid configuration, the unpaid CLI makes one
best-effort Telegram send containing exactly **Monitoring Stopped** when it
actually exits, including a terminal failure, Ctrl-C, or an unhandled Python
exception that unwinds through cleanup. This is sent after Chrome cleanup, not
on browser renewals, retries, or cooldowns. It uses the existing bot/chat, with
5-second connect/read timeouts and no retry or change to appointment-alert state.
Delivery failure does not change the original exit outcome. Invalid startup
configuration does not trigger a send. `TEST_MODE` does not suppress this message.
Force-killing Python, default SIGTERM termination, power loss, or unavailable
Telegram/network cannot guarantee a stop notification; there is no external
watchdog or signal-handler change.

Availability is not reserved and may change before/after payment. If the
displayed earliest date is before your window, the summary cannot tell you
whether a later in-window slot exists. Current table selectors still need an
explicitly initiated read-only check against the live page.

`legacy/detect_and_notify.py` is unmaintained and superseded by
`payment_tracker.py`; do not use it as the supported payment tracker.

### Reuse Telegram delivery

`telegram_notifier.send_telegram_message()` is independent of visa settings,
Selenium, test mode, and Gmail. Pass text, `bot_token`, and `chat_id` explicitly;
it makes one verified HTTPS attempt and returns `TelegramDeliveryResult` with
`status`, `reason`, `retry_after`, and a `delivered` property.

Statuses distinguish confirmed delivery, retryable failure, permanent failure,
and uncertain delivery. The function does not sleep, retry, or retain state.
Callers own message formatting, duplicate suppression, and retry/test-mode policy.
Future paid-flow integration can reuse it without changing booking behavior;
Telegram is not currently enabled in the paid flow.

### Offline regression tests

```sh
USER_CONSULATE=Calgary python3 -m unittest discover -s tests -v
```

New tracker/Telegram tests use fake browsers, HTTP clients, temporary state, and
controlled clocks; they neither log in to real visa accounts nor send messages.
Existing local booking tests expect Calgary for their fake facility options.

## Caution

It may not always be feasible to reschedule an appointment multiple times. Therefore, it's crucial to use `TEST_MODE = True` for testing purposes and ensure the `LATEST_ACCEPTABLE_DATE` is genuinely acceptable to you.

Consulates other than Toronto and Vancouver are not tested.

## Contribution

Please feel free to report issues. PRs are welcomed and greatly appreciated!

One improvement I'm interested in is rewriting `legacy_rescheduler` using `requests`.

## Special thanks

Huge thanks to [@jywyq](https://github.com/jywyq) for adding the Gmail notification feature.

Huge thanks to [@bsingh-kpt](https://github.com/bsingh-kpt) for (finally!) fixing the `legacy_rescheduler` in Mar 2025.

Thanks to [@trungnguyen21](https://github.com/trungnguyen21) and [@saroopskesav](https://github.com/saroopskesav) for helping with the consulate numbers in other cities.

## Disclaimer

This script is provided as-is for the purpose of assisting individuals in rescheduling appointments. While it has been developed with care and with the intention of being helpful, it comes with no guarantees or warranties of any kind, either expressed or implied. By using this script, you acknowledge and agree that you are doing so at your own risk. The author(s) or contributor(s) of this script shall not be held liable for any direct or indirect damages that arise from its use. Please ensure that you understand the actions performed by this script before running it, and consider the ethical and legal implications of its use in your context.
