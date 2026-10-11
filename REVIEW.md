# REVIEW.md

Defects found in the starter `app/main.py`, in the order they appear in the file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Not `tz_aware`, so reads return naive datetimes. Mixing them with the aware `IST` values raises `TypeError`, and "naive" is silently assumed to be local time. | Punch-out on a stored record -> `can't subtract offset-naive and offset-aware datetimes` (500). | `tz_aware=True`; all instants are aware UTC. `to_ms()` also tolerates naive values. |
| 2 | startup (missing) | No indexes at all; nothing enforces uniqueness. | `GET /attendance` on 100k docs is a COLLSCAN; the races below are possible. | `ensure_indexes()` in the lifespan hook, idempotent, one `create_index` per index so a failure cannot hide the others. |
| 3 | `/health` | Always returns ok, even if MongoDB is down. | Stop mongod, call `/health` -> still 200 (contract: 503). | Pings Mongo; 503 on `PyMongoError`. |
| 4 | `compute_late_minutes` | `punch_in.replace(hour, minute)` uses whatever time zone the datetime has (naive local time), not IST. Shift start is therefore compared on the wrong clock. | Server in UTC: a 09:35 IST punch-in looks 5 h early or 5.5 h late. | Shift start is built as an aware IST datetime for the *attendance date* (`shift_bounds`). |
| 5 | `compute_late_minutes` | Grace boundary wrong: `int(seconds/60)` floors to minutes *before* comparing `> 10`, so 09:40:01 gives 10 -> not > 10 -> 0 (R2 says 10). | 09:40:01 must return 10. | Compare in whole seconds (`> 600`), then floor to minutes. |
| 6 | `compute_late_minutes` / `punch_in` | Seconds/milliseconds not truncated (R1). `int()` truncates toward zero, not floor. Overnight shifts ignored: a 03:00 punch-in for a 22:00-06:00 shift is compared with *that day's* 22:00 and reads as early (0 late) instead of 300. | Overnight employee punching in at 03:00. | `from_ms` truncates to seconds; late uses the attendance date (R1) and integer floor division. |
| 7 | `compute_work_hours` | `round()` is banker's rounding on a binary float (R4 wants half-up); no second truncation. | 18 s = 0.005 h -> returns 0.0 (should be 0.01). | `Decimal` division and `ROUND_HALF_UP`. |
| 8 | `compute_overtime` | No 30-minute minimum (R3): 5 minutes past shift end is returned as 5. Also subtracts an aware value from a naive stored punch-out (TypeError), and ignores overnight shifts (end is on the next day). | Sample 1 (out 18:35) must give 0. | `shift_bounds` adds a day for overnight shifts; `>= 30` rule; all datetimes aware. |
| 9 | helpers (missing) | There is no half-day logic (R5) at all and nothing builds the full derived set. | Punch-out for 3.5 h must set `half_day: true`. | `compute_derived()` returns all four derived fields; half-day uses the *rounded* hours. |
| 10 | `EmployeeIn` | No validation: any `emp_code`, `joined_on: "banana"`, `shift_start == shift_end`, `25:99` are accepted. | POST bad bodies -> 201 instead of 422. | Pydantic patterns, a real-date validator and a model validator for equal shifts. |
| 11 | `create_employee` | Check-then-insert race: two parallel creates both pass `find_one`. | Fire 10 identical POSTs at once -> several 201. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 12 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as an ISO string, not epoch ms. | Response `created_at` is a string. | `utc_now()` stored; serializer returns epoch ms. |
| 13 | `list_employees` | `skip = page * page_size` skips the first page (page 1 returns items 21-40). | `?page=1` misses the first employees. | `(page - 1) * page_size`. |
| 14 | `list_employees` | `total` counts the whole collection, ignoring `department` (R10). | `?department=X` -> total is every employee. | `count_documents(filter)` with the same filter. |
| 15 | `list_employees` | No sort (contract: `emp_code` asc, so pages can overlap or skip). `page=0`/`page_size=100000` unchecked: negative skip raises a driver error (500). | `?page=0`. | `.sort("emp_code")`; `Query(ge=1)`, `page_size` 1-100 (422 otherwise). |
| 16 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError` -> 500 (contract: 404). | Punch in for `EMP9999`. | `get_employee_or_404`. |
| 17 | `punch_in` | Check-then-insert race: two parallel punch-ins can both insert (duplicate day). | Two simultaneous identical requests -> two 201. | Unique `(emp_code, date)` index; insert and catch `DuplicateKeyError` -> 409. |
| 18 | `punch_in` | `fromtimestamp()` uses the server's local zone, so `date` is not the IST date (R1); no overnight rule; `if body.punched_at` treats 0 as "missing". | Server in UTC: a punch-in at 01:00 IST gets yesterday's date. | `attendance_date()` implements R1 in IST including overnight shifts; `is not None` checks. |
| 19 | `PunchInIn` | `punched_at: Optional[int]` accepts seconds-looking values; `status: str` accepts `ABSENT`, `LEAVE` or garbage. | `punched_at: 1783312500` (seconds), `status: "FOO"`. | `StrictInt` with `100000000000 <= x <= 4102444800000` (rejects floats, strings, bools); `Literal` for the three presence statuses. |
| 20 | `punch_in` response | Leaks `id` (the ObjectId) and returns datetimes instead of epoch ms; derived fields are hand-built so they can drift from the other endpoints. | Response has `id`; `punch_in` is a string. | One `serialize_record()` used by every endpoint; no `_id`/`id`. |
| 21 | `list_attendance` | `list(db.attendance_logs.find(q))` loads every match into memory, sorts in Python, slices in Python. `total = len(docs)`. | 100k docs: slow and memory-heavy; explain shows COLLSCAN. | `find().sort().skip().limit()` backed by `(date desc, emp_code)` / `(emp_code, date)` indexes, and `count_documents`. |
| 22 | `list_attendance` | Sorted by `date` only (contract: date desc, then `emp_code` asc), so ties are in arbitrary order. | Many records on one date. | Sort `[("date", -1), ("emp_code", 1)]`. |
| 23 | `list_attendance` | No validation: bad dates, `date_from > date_to`, unknown `status` accepted; `page=0` makes a negative slice; `page_size` unbounded; legacy documents without `history`/`half_day` are returned as-is; leaks `id`. | `?date_from=2026-08-01&date_to=2026-07-01`. | `DateStr` validator, range check -> 422, `Literal` status, bounded paging, serializer that defaults missing fields. |

**Examined and judged fine**

- `load_dotenv()` does not override variables already in the environment, so real environment variables win as required.
- `doc.pop("_id")` after `insert_one`: `insert_one` mutates the dict, so removing `_id` was correct (I serialize explicitly instead).
- The `IST` constant (`UTC+05:30`, no DST) is correct.
- Pydantic ignores unknown fields, so a client sending `work_hours` etc. is ignored as the contract requires (kept deliberately for the PATCH body).
- Synchronous `def` endpoints with PyMongo are fine: FastAPI runs them in a thread pool and `MongoClient` is thread-safe. Concurrency safety comes from the database (unique indexes, atomic updates), not from Python.
