# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
   - `employees`: unique `emp_code` (enforces customer code uniqueness, prevents race conditions); compound `(department, joined_on)` and `joined_on` (serves department filtering and headcount date scans).
   - `attendance_logs`: unique compound `(emp_code, date)` (enforces one record per employee-day, prevents concurrent punch-in races); `(emp_code, punch_in)` (serves punch-out lookup for latest punch-in); compound `[("date", -1), ("emp_code", 1)]` (enables index-backed sort for `attendance_list` without memory sort, and date scan for trend); `(date, late_minutes)` (serves leaderboard match).
   - Rejected: an index on `status` alone, as low cardinality (5 values) yields poor selectivity compared to compound ESR indexes.

2. **Punch-in race.**
   Both concurrent requests validate and attempt `insert_one()`. MongoDB's unique compound index on `(emp_code, date)` atomically accepts the first write and rejects the second with `DuplicateKeyError`. The application catches this error and returns `HTTP 409 Conflict`, guaranteeing exactly one 201 and no duplicate logs.

3. **Ties.**
   Both tied employees receive the identical rank using standard competition ranking (`$rank` in MongoDB's `$setWindowFields`, skipping subsequent ranks, e.g. 1, 2, 2, 4). Filtering is applied after ranking as `rank <= limit`. Because both share an eligible rank, both are returned (ordered by `emp_code` ascending), so the response can contain more than `limit` rows.

4. **Headcount.**
   The aggregation pipeline starts from the `employees` collection (matching `joined_on <= month_end`), grouping by department to compute `headcount: {$sum: 1}`. It left-joins `attendance_logs` via `$lookup`. Employees with zero logs yield an empty logs array, contributing 1 to headcount and 0 to attendance metrics.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily attendance summaries into a dedicated rollup collection upon punch-out and regularization. This replaces heavy multi-collection `$lookup` and window operations on tens of millions of raw logs with instant O(1) range queries.
