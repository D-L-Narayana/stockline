# Schema versioning and migrations

StockLine stores its schema version in SQLite's `PRAGMA user_version` and upgrades database
files in place, forward-only, the first time a connection is opened (`db.init_schema()` →
`db.migrate()`). This is what lets a `/data/stockline.db` volume written by v0.1.0 start under
v0.2.0 without any manual step.

## How it works (`app/db.py`)

| Symbol | Meaning |
|---|---|
| `SCHEMA_V1` | The v0.1.0 DDL, **frozen**. Every statement is `CREATE … IF NOT EXISTS`. `SCHEMA` is an alias kept for compatibility. |
| `SCHEMA_VERSION` | The version this code expects (currently `2`). |
| `MIGRATIONS` | `dict[int, Callable[[sqlite3.Connection], None]]`: target version → step function. |
| `V2_STATEMENTS` | The individual SQL statements executed by the v2 step (handy for tests and reviews). |
| `schema_version(conn)` | Reads `PRAGMA user_version` (`0` = never migrated). |
| `migrate(conn) -> int` | Upgrades the file and returns the resulting version. |
| `init_schema(conn) -> int` | Alias of `migrate`; used by the connection pool and the browser bridge. |

`migrate()` does, in order:

1. If `user_version == 0` — a brand-new file *or* a legacy v0.1.0 file (which never set the pragma) —
   run `SCHEMA_V1` with `executescript` and stamp `user_version = 1`. Because every statement is
   `IF NOT EXISTS`, existing tables, indexes and rows are untouched.
2. For every version `v` from `user_version + 1` up to `SCHEMA_VERSION`, open one
   `BEGIN IMMEDIATE` transaction (`db.transaction`), call `MIGRATIONS[v]`, then run
   `PRAGMA user_version = v` **inside the same transaction**. In SQLite `user_version` is
   transactional, so a step either lands completely or not at all.
3. Files already at or above `SCHEMA_VERSION` are left alone (a newer file opened by older code
   keeps its version; see *Rollback* below).

Rules for steps:

* Use plain `conn.execute(...)` calls, one statement each — never `executescript` inside a
  transaction (it would issue its own `COMMIT`).
* Additive and non-destructive only: new nullable columns, new tables, new indexes, data backfills.
  Never drop or rewrite tables; never edit `SCHEMA_V1` or an already-shipped step.
* Steps must be idempotent where SQLite allows it (`IF NOT EXISTS`, `WHERE … IS NULL` backfills).
  `ALTER TABLE … ADD COLUMN` is not idempotent, which is exactly why the version stamp and the
  statements share one transaction.

## Version 2 (v0.2.0)

```sql
ALTER TABLE orders ADD COLUMN request_hash TEXT;            -- idempotency fingerprint
ALTER TABLE orders ADD COLUMN updated_at TEXT;              -- set on cancel / fulfil
ALTER TABLE products ADD COLUMN updated_at TEXT;            -- set on patch / soft delete
ALTER TABLE stock_movements ADD COLUMN balance_after INTEGER;  -- running balance per (store, product)
CREATE TABLE IF NOT EXISTS transfers (...);                 -- transfers become entities
CREATE INDEX IF NOT EXISTS idx_orders_status_created   ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_movements_reason_created ON stock_movements(reason, created_at);
CREATE INDEX IF NOT EXISTS idx_movements_reference      ON stock_movements(reference);
UPDATE orders SET updated_at = created_at WHERE updated_at IS NULL;
UPDATE stock_movements SET balance_after = (running SUM(delta) up to and including this row)
 WHERE balance_after IS NULL;
```

The `stock_movements.reason` CHECK list and every v1 constraint are unchanged. All new columns are
nullable, so code that does `SELECT *` and `INSERT` with explicit column lists keeps working.

## Adding version 3

1. Write the step in `app/db.py`:

   ```python
   V3_STATEMENTS: tuple[str, ...] = (
       "ALTER TABLE stores ADD COLUMN timezone TEXT",
       "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)",
   )

   def _migrate_v3(conn: sqlite3.Connection) -> None:
       for statement in V3_STATEMENTS:
           conn.execute(statement)

   MIGRATIONS[3] = _migrate_v3          # or add it to the dict literal
   SCHEMA_VERSION = 3
   ```

2. Keep the step additive (see the rules above). If a backfill is needed, express it as an
   `UPDATE … WHERE column IS NULL` so re-running it is harmless.
3. Add tests to `tests/test_migrations.py`: a fresh database reaches version 3 with the new
   columns/indexes (`PRAGMA table_info`), a v2 file built from the real v2 steps migrates in place
   with its rows intact, running `migrate()` twice is a no-op, and a step that raises midway leaves
   `user_version` at 2 with none of the v3 DDL applied.
4. Update this document; the `schema_version` reported by `/health` and `/integrity` follows automatically.

## Legacy v0.1.0 files

A v0.1.0 database has all six tables and `user_version = 0`. On first start the migration stamps it
to 1 (no DDL runs because every table already exists) and then applies v2 in one transaction:
`updated_at` is backfilled from `created_at` for existing orders and `balance_after` is computed
for every existing movement as the running sum of `delta` per `(store_id, product_id)` in `id`
order. Row counts and `inventory` balances are not modified. The `balance_after` backfill is a
correlated sub-query, so its cost grows with the number of movements per store/product pair;
for demo-sized files it is not noticeable.

## Failure semantics

* A step that raises (SQL error or Python exception) rolls back together with its version stamp;
  the file stays at the previous version and the next start retries the same step.
* `migrate()` refuses to run inside an open transaction and raises if a version between the file's
  version and `SCHEMA_VERSION` has no registered step — both are programming errors, not runtime
  conditions.
* The connection pool runs `init_schema` once per process on the first connection; the browser
  bridge does the same for its single in-memory/virtual-FS connection.

## Rollback

v0.2.0 only adds nullable columns, a new table and indexes. Downgrading the *code* to v0.1.0
against a v2 *file* therefore works: the old code ignores `user_version`, selects `*` and inserts
with explicit column lists, so its statements still succeed. Rows it writes simply carry `NULL`
in the new columns — movements have no `balance_after`, orders have no `updated_at` /
`request_hash`. The cached balances stay correct, because both versions update
`inventory.on_hand` in the same transaction as the movement. Once v0.2.0 code runs again:

* `GET /integrity` (`ledger.ledger_integrity()`) counts such movements in `missing_balance_after`
  without failing the check (`ok` stays `true`; the chain check only looks at rows that have a value);
* `POST /integrity/rebuild` (`ledger.rebuild_balances()`) backfills every missing `balance_after`
  from the running ledger sum, reporting the number of rows in `backfilled`.

Both halves of this scenario are exercised by `tests/test_migrations.py`.
