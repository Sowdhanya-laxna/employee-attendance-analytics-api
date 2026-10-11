# DECISIONS.md

1. **Indexes.** `employees.emp_code` (unique): one employee per code, and the join key. `employees (department, joined_on)` and `joined_on`: headcount and department filters. `attendance_logs (emp_code, date)` (unique): one record per day, and per-employee month scans. `(date desc, emp_code)`: the list order and month range scans. `(emp_code, punch_in desc)`: finding the open record at punch-out. `(status, date desc, emp_code)`: status filter. I rejected an index on `status` alone: only five values, so it narrows almost nothing.

2. **Punch-in race.** Both requests find the employee and compute the same date, then both call `insert_one`. The unique `(emp_code, date)` index lets the database accept one; the other gets `DuplicateKeyError`, which I turn into 409. I never check first.

3. **Ties.** I rank with `$rank`, so ties share a rank (1, 2, 2, 4). I apply `limit` afterwards as `rank <= limit`, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs. No logs just means an empty array, and the person still counts once.

5. **100x data.** The trend pipeline pushes every emp_code of a department into one document, which will hit the 16 MB limit. I would keep a per-department, per-day rollup collection updated on each write.
