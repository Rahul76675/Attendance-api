# REVIEW.md

Defects found in the starter `app/main.py`. "Fix" describes what the submitted file does now.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Client is not `tz_aware`: PyMongo returns naive datetimes, so every comparison/conversion silently depends on the server's local zone. No `serverSelectionTimeoutMS`, so an outage hangs requests for 30 s. | `punch_in` read back has `tzinfo=None`; `.timestamp()` shifts by the host offset. | `tz_aware=True, tzinfo=UTC`; 3 s selection timeout; all conversions go through `to_ms` / `from_ms` / `as_utc`. |
| 2 | whole file | No indexes and no unique constraints anywhere. | `explain()` shows COLLSCAN; duplicates appear under parallel requests. | `ensure_indexes()` at startup (and retried from `/health`), idempotent. |
| 3 | `health()` | Always returns ok, never talks to MongoDB. | Stop MongoDB, `/health` still 200 (spec: 503). | Pings MongoDB, 503 on failure. |
| 4 | `compute_late_minutes` | Uses the punch-in's own (naive/UTC) clock, not IST, and puts `shift_start` on the punch-in's calendar day instead of the attendance date, so overnight shifts are wrong. | Overnight shift 22:00, punch-in 00:05 IST: should be 125 min late, gives a negative / 0. | `shift_instant(attendance_date, shift_start)` in IST. |
| 5 | `compute_late_minutes` | Floors to whole minutes *before* comparing with the grace period. | 09:40:01 on a 09:30 shift: floor = 10, `10 > 10` is false -> 0. Spec says 10. Sub-second values are also not truncated. | Compare seconds (`> 600`), then floor to minutes; instants truncated to seconds on input. |
| 6 | `compute_work_hours` | Built-in `round()` is banker's rounding on a binary float, not half-up. | 16182 s = 4.495 h must be 4.50. Float/banker's can give 4.49. | `Decimal` with `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | Shift end placed on the record's date even for overnight shifts; no 30-minute minimum; `replace()` keeps stray seconds. | Overnight 22:00-06:00 out 06:40 next day: negative then clamped to 0 instead of 40; 5 min over gives 5 instead of 0. | End moved to next day when overnight; counted only if >= 30. |
| 8 | `create_employee` | Check-then-insert race, no unique index. | 20 parallel POSTs with the same `emp_code` -> several 201. | Unique index; `DuplicateKeyError` -> 409. |
| 9 | `EmployeeIn` / `create_employee` | No validation: `emp_code` format, e-mail, `HH:MM`, lengths, `joined_on` real date, `shift_start != shift_end`. | `joined_on: "2026-13-45"` or `shift_start: "25:99"` accepted. | Pydantic constraints + validators -> 422. |
| 10 | `create_employee` | `created_at = datetime.now()` is local and naive, and is returned as an ISO string, not epoch ms. | Response `created_at` is a string. | UTC datetime stored, epoch-ms integer returned. |
| 11 | `list_employees` | `skip = page * page_size` skips the whole first page. | `page=1` never returns the first rows. | `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` counts the whole collection, ignoring the filter; no sort so paging is unstable; `page=0` gives a negative skip (500); `page_size` unbounded. | `?department=X` returns total = all employees. | `count_documents(q)`, `sort emp_code`, `Query(ge=1, le=100)`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` -> `TypeError` -> 500. | POST an unknown `emp_code`. | 404. |
| 14 | `punch_in` | `fromtimestamp(ms/1000)` uses local time and keeps microseconds; `if body.punched_at` treats `0` as missing; seconds-looking, float and string values are not rejected. | `punched_at: 1783312500` (seconds) accepted. | `StrictInt` bounded 1e11..4.1e12 -> 422; truncated to whole seconds; `is not None` check. |
| 15 | `punch_in` | `date` taken from the naive local date, not IST, and no overnight rule (R1). | Punch-in 23:30 UTC (05:00 IST next day) filed under the wrong day. | `attendance_date_for()`. |
| 16 | `punch_in` | Check-then-insert race. | Two parallel identical punch-ins -> two 201 (or one 201 + one 500 once an index exists). | Unique `(emp_code, date)` index; `DuplicateKeyError` -> 409. |
| 17 | `punch_in` / `PunchInIn.status` | Any string accepted, including `ABSENT`, `LEAVE`, garbage. | `status: "banana"` -> 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 18 | `punch_in` | Leaks the ObjectId as `id`, returns `punch_in` as a datetime. | Response has `id`. | `attendance_out()` builds the contract shape only. |
| 19 | `list_attendance` | Loads every matching document into memory, sorts and slices in Python; no index. | 100k records: seconds and hundreds of MB. | `find().sort().skip().limit()` backed by indexes; `count_documents` for the total. |
| 20 | `list_attendance` | Sort is `date` only (no `emp_code` tie-break) so pages are unstable; `sort(reverse=True)` on strings only. | Same-date rows swap between pages. | Sort `(date desc, emp_code asc)`. |
| 21 | `list_attendance` | No validation of dates, `status`, `page`, `page_size`; `date_from > date_to` not rejected; legacy docs without `history`/`half_day` returned as-is; `_id` leaked; datetimes unconverted. | `date_from=abc`, `page=-1`. | Validators, 422, normalising `attendance_out()`. |
| 22 | all endpoints | A MongoDB error becomes an unhandled 500. | Stop MongoDB while calling an endpoint. | Global `PyMongoError` handler -> 503. |

## Looked at, judged fine

* `load_dotenv()` does **not** override variables already in the environment, so real env vars win as required.
* Endpoints are plain `def`, so FastAPI runs them in a thread pool. That is correct for blocking PyMongo calls (an `async def` would block the event loop).
* `insert_one` mutating the dict with `_id`, then `doc.pop("_id")`: works. I avoid the mutation by inserting a copy.
* Status codes 201 on the two POST creations are right.
* The default `mongodb://localhost:27017` fallback is fine for local development; env wins.
