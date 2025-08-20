#
# Copyright © 2025 Rodney Dawes
# Copyright © 2019-2022 Stb-tester.com Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import logging
import re
import sqlite3

from types import TracebackType
from typing import Any

from squeeql.utils import normalize_sql

_INTERNAL_TABLES = [
    "sqlite_stat1",
    "sqlite_stat2",
    "sqlite_stat3",
    "sqlite_stat4",
]


def dumb_migrate_db(
    db: sqlite3.Connection,
    schema: str,
    allow_deletions: bool = False,
) -> bool:
    """
    Migrates a database to the new schema given by the SQL text `schema`
    preserving the data.  We create any table that exists in schema, delete any
    old table that is no longer used and add/remove columns and indices as
    necessary.

    Under this scheme there are a set of changes that we can make to the schema
    and this script will handle it fine:

    1. Adding a new table
    2. Adding, deleting or modifying an index
    3. Adding a column to an existing table as long as the new column can be
       NULL or has a DEFAULT value specified.
    4. Changing a column to remove NULL or DEFAULT as long as all values in the
       database are not NULL
    5. Changing the type of a column
    6. Changing the user_version

    In addition this function is capable of:

    1. Deleting tables
    2. Deleting columns from tables

    But only if allow_deletions=True. If the new schema requires a column/table
    to be deleted and allow_deletions=False this function will raise
    `RuntimeError`.

    Note: When this function is called a transaction must not be held open on
    db.  A transaction will be used internally.  If you wish to perform
    additional migration steps as part of a migration use DBMigrator directly.

    Any internally generated rowid columns by SQLite may change values by this
    migration.
    """
    with DBMigrator(db, schema, allow_deletions) as migrator:
        migrator.migrate()
    return bool(migrator.n_changes)


class DBMigrator:
    def __init__(
        self,
        db: sqlite3.Connection,
        schema: str,
        allow_deletions: bool = False,
    ) -> None:
        self.db = db
        self.schema = schema
        self.allow_deletions = allow_deletions

        self.pristine = sqlite3.connect(":memory:")
        self.pristine.executescript(schema)
        self.n_changes = 0

        self.orig_foreign_keys = None

    def log_execute(self, msg: str, sql: str) -> None:
        # It's important to log any changes we're making to the database for
        # forensics later
        logging.info(msg)
        self.db.execute(sql)
        self.n_changes += 1

    def __enter__(self) -> None:
        self.orig_foreign_keys = self.db.execute(
            "PRAGMA foreign_keys",
        ).fetchone()[0]
        if self.orig_foreign_keys:
            self.log_execute(
                "Disable foreign keys temporarily for migration",
                "PRAGMA foreign_keys = OFF",
            )
            # This doesn't count as a change because we'll undo it at the end
            self.n_changes = 0

        self.db.__enter__()
        self.db.execute("BEGIN")
        return self

    def __exit__(
        self,
        exc_type: BaseException | None,
        exc_value: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.db.__exit__(exc_type, exc_value, exc_tb)
        if exc_value is None:
            # The SQLite docs say:
            #
            # > This pragma is a no-op within a transaction; foreign key
            # > constraint enforcement may only be enabled or disabled when
            # > there is no pending BEGIN or SAVEPOINT.
            old_changes = self.n_changes
            new_val = self._migrate_pragma("foreign_keys")
            if new_val == self.orig_foreign_keys:
                self.n_changes = old_changes

            # SQLite docs say:
            #
            # > A VACUUM will fail if there is an open transaction on the
            # > database connection that is attempting to run the VACUUM.
            if self.n_changes:
                self.db.execute("VACUUM")
        else:
            if self.orig_foreign_keys:
                self.log_execute(
                    "Re-enable foreign keys after migration",
                    "PRAGMA foreign_keys = ON",
                )

    def migrate(self) -> None:
        # In CI the database schema may be changing all the time.  This checks
        # the current db and if it doesn't match database.sql we will
        # modify it so it does match where possible.
        pristine_tables = dict(
            self.pristine.execute(
                """\
            SELECT name, sql FROM sqlite_master
            WHERE type = \"table\" AND name != \"sqlite_sequence\""""
            ).fetchall()
        )
        pristine_indices = dict(
            self.pristine.execute(
                """\
            SELECT name, sql FROM sqlite_master
            WHERE type = \"index\""""
            ).fetchall()
        )
        pristine_views = dict(
            self.pristine.execute(
                """\
                SELECT name, sql FROM sqlite_master
                WHERE type = \"view\"
                """
            ).fetchall()
        )

        views = dict(
            self.db.execute(
                """\
                SELECT name, sql FROM sqlite_master
                WHERE type = \"view\"
                """
            ).fetchall()
        )

        # Existing views must be dropped before tables migration can succeed
        for view_name in views.keys():
            self.log_execute(f"Drop view {view_name}", f"DROP VIEW {view_name}")
            self.n_changes -= 1

        tables = dict(
            self.db.execute(
                """\
            SELECT name, sql FROM sqlite_master
            WHERE type = \"table\" AND name != \"sqlite_sequence\""""
            ).fetchall()
        )

        new_tables = (
            set(pristine_tables.keys()) - set(tables.keys()) - set(_INTERNAL_TABLES)
        )
        removed_tables = (
            set(tables.keys()) - set(pristine_tables.keys()) - set(_INTERNAL_TABLES)
        )
        if removed_tables and not self.allow_deletions:
            raise RuntimeError(
                f"Database migration: Refusing to delete tables {removed_tables!r}"
            )

        modified_tables = set(
            name
            for name, sql in pristine_tables.items()
            if normalize_sql(tables.get(name, "")) != normalize_sql(sql)
        ) - set(_INTERNAL_TABLES)

        # This PRAGMA is automatically disabled when the db is committed
        self.db.execute("PRAGMA defer_foreign_keys = TRUE")

        # New and removed tables are easy:
        for tbl_name in new_tables:
            self.log_execute(f"Create table {tbl_name}", pristine_tables[tbl_name])
        for tbl_name in removed_tables:
            self.log_execute(f"Drop table {tbl_name}", f"DROP TABLE {tbl_name}")

        for tbl_name in modified_tables:
            # The SQLite documentation insists that we create the new table and
            # rename it over the old rather than moving the old out of the way
            # and then creating the new
            create_table_sql = pristine_tables[tbl_name]
            create_table_sql = re.sub(
                r"\b%s\b" % re.escape(tbl_name),
                f"{tbl_name}_migration_new",
                create_table_sql,
                count=1,
            )
            self.log_execute(
                f"Columns change: Create table {tbl_name} with updated schema",
                create_table_sql,
            )

            cols = set(
                [x[1] for x in self.db.execute(f"PRAGMA table_info({tbl_name})")]
            )
            pristine_cols = set(
                [
                    x[1]
                    for x in self.pristine.execute(
                        f"PRAGMA table_info({tbl_name})",
                    )
                ]
            )

            removed_columns = cols - pristine_cols
            if not self.allow_deletions and removed_columns:
                logging.warning(
                    "Database migration: Refusing to remove columns %r from "
                    "table %s.  Current cols are %r attempting migration to %r",
                    removed_columns,
                    tbl_name,
                    cols,
                    pristine_cols,
                )
                raise RuntimeError(
                    "Database migration: Refusing to remove columns %r from "
                    "table %s" % (removed_columns, tbl_name)
                )

            logging.info("cols: %s, pristine_cols: %s", cols, pristine_cols)
            common = ", ".join(cols.intersection(pristine_cols))
            self.log_execute(
                f"Migrate data for table {tbl_name}",
                f"""\
                INSERT INTO {tbl_name}_migration_new ({common})
                SELECT {common} FROM {tbl_name}""",
            )

            # Don't need the old table any more
            self.log_execute(
                f"Drop old table {tbl_name} now data has been migrated",
                f"DROP TABLE {tbl_name}",
            )

            self.log_execute(
                f"Columns change: Move new table {tbl_name} over old",
                f"ALTER TABLE {tbl_name}_migration_new RENAME TO {tbl_name}",
            )

        # Migrate the indices
        indices = dict(
            self.db.execute(
                """\
            SELECT name, sql FROM sqlite_master
            WHERE type = \"index\""""
            ).fetchall()
        )
        for name in set(indices.keys()) - set(pristine_indices.keys()):
            self.log_execute(
                f"Dropping obsolete index {name}",
                f"DROP INDEX {name}",
            )
        for name, sql in pristine_indices.items():
            if name not in indices:
                self.log_execute(f"Creating new index {name}", sql)
            elif sql != indices[name]:
                self.log_execute(
                    f"Index {name} changed: Dropping old version",
                    f"DROP INDEX {name}",
                )
                self.log_execute(
                    f"Index {name} changed: Creating updated version in its place",
                    sql,
                )

        # Now that tables and indices have migrated, we can recreate the views
        for name, sql in pristine_views.items():
            self.log_execute(f"Create view {name}", sql)
            self.n_changes -= 1

        self._migrate_pragma("user_version")

        if self.pristine.execute("PRAGMA foreign_keys").fetchone()[0]:
            if self.db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(
                    "Database migration: Would fail foreign_key_check",
                )

    def _migrate_pragma(self, pragma: str) -> Any:
        pristine_val = self.pristine.execute(f"PRAGMA {pragma}").fetchone()[0]
        val = self.db.execute(f"PRAGMA {pragma}").fetchone()[0]

        if val != pristine_val:
            self.log_execute(
                f"Set {pragma} to {pristine_val} from {val}",
                f"PRAGMA {pragma} = {pristine_val}",
            )

        return pristine_val
