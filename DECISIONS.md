# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` makes one record per day and serves PATCH, the monthly summary and the `$lookup`s. `(date desc, emp_code)` matches the list sort and month scans. `(status, date, emp_code)` serves the status filter, `(emp_code, punch_in desc)` finds the punch-out target. Employees: unique `emp_code`, `(department, emp_code)`, `joined_on`. I rejected a `work_hours` index: analytics always scan a month anyway.

2. **Punch-in race.** Both requests compute the same date, then call `insert_one`. The unique index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. No prior read decides anything.

3. **Ties.** Ranking uses `$rank`, so tied employees share a rank and the next is skipped. The limit filters on rank after ranking, so a tie at the cutoff returns every tied person. Rows sort by minutes, then `emp_code`.

4. **Headcount.** Summary starts from `employees` filtered by `joined_on`, then `$lookup`s logs, so employees with no logs still count once.

5. **100x data.** Store `department` and a month key on each log, and pre-aggregate monthly totals.
