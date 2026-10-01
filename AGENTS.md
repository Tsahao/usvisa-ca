# AGENTS.md — usvisa-ca

## Run
- Entry: `python3 reschedule.py`. Requires Chrome + `.env` (see `README.md` vars). `SHOW_GUI=True` hardcoded in `settings.py`.
- Dry run: `TEST_MODE="True"` (default) **never clicks confirm** — `legacy_rescheduler.py` raises `UnverifiedReschedule` instead to exercise verification read-only. `False` = live booking.
- No `pytest` installed; use `python3 -m unittest discover -s tests -v`.
- Tests are env-leaky: browser tests read `USER_CONSULATE` from `.env`. Run with `USER_CONSULATE=Calgary` (fakes only offer Toronto 94 / Calgary 89); `ConsulateSettingsTests` pass regardless.

## Architecture
- `reschedule.py`: session lifecycle, polling, verification. `legacy_rescheduler.py`: Selenium calendar/booking only (no `requests`). `settings.py`: `.env` via `python-dotenv`. `request_tracker.py`: retry/time budgets.
- Flow: login → dashboard `p.consular-appt` date → appointment page → poll `.../appointment/days/{FACILITY_ID}.json` → `legacy_reschedule()` → success / `False` (retry) / `UnverifiedReschedule`.
- Unverified verify (date-only): fresh login re-reads dashboard (`VERIFY_MAX_READS=3`, `VERIFY_READ_DELAY=10`): `== target` → success+quit; `== previous` repeatedly → silent continue, next session skips one `NEW_SESSION_DELAY`; else manual email+quit. Toggle: `VERIFY_UNVERIFIED_BOOKING="False"` restores old quit-always.
- `reschedule_with_new_session()` returns `(done, skip_delay)`; `__main__` skips the delay once after verified-failure. All Gmail goes through `_send_gmail_notification()` (log-only in `TEST_MODE`).

## Gotchas
- Dates strictly `YYYY-MM-DD`; `USER_CONSULATE` validated case-insensitively against Calgary/Halifax/Montreal/Ottawa/Quebec/Toronto/Vancouver.
- Soft-ban: 4× empty date list → `SoftBanDetected` → `SOFT_BAN_COOLDOWN` (1h); 15× setup failures → same. `SessionExpired` (sign_in redirect, 401/403, HTML instead of JSON) must start a new session immediately, never retry on the dead driver.
- Healthy-but-unusable polls (`forgive_last_retry`) don't consume budget; `MAX_HEALTHY_SESSION_AGE` (90min) forces re-login.
- Never book outside window/exclusions/`ONLY_EARLIER_THAN_CURRENT_APPOINTMENT` guard — the calendar click is untrusted, the post-pick guard in `legacy_reschedule()` is authoritative.
- Never add a same-driver dashboard check that navigates the polling driver away; verification owns its driver and quits it.
