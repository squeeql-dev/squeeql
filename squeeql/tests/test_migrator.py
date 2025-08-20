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
import sqlite3
import unittest

import pytest

from parameterized import parameterized

from squeeql.migrator import _INTERNAL_TABLES, dumb_migrate_db
from squeeql.utils import normalize_sql


_TEST_SCHEMAS = [
    # 0
    "",
    # 1
    """\
    CREATE TABLE Node(
        node_oid INTEGER PRIMARY KEY NOT NULL,
        node_id INTEGER NOT NULL);
    CREATE UNIQUE INDEX Node_node_id on Node(node_id);
    """,
    # 2
    # Added Node.active
    # Changed node_id type from INTEGER to TEXT
    # New table Job
    """\
    PRAGMA foreign_keys = 1;

    CREATE TABLE Node(
        node_oid INTEGER PRIMARY KEY NOT NULL,
        node_id TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT(1),
        something_else TEXT);
    CREATE UNIQUE INDEX Node_node_id on Node(node_id);

    CREATE TABLE Job(
        node_oid INTEGER NOT NULL,
        id INTEGER NOT NULL,
        FOREIGN KEY(node_oid) REFERENCES Node(node_oid));
    CREATE UNIQUE INDEX Job_node_oid on Job(node_oid, id);
    """,
    # 3
    # Remove field something_else.  Note: this is significant because
    # Job.node_oid references table Node which must be recreated.
    """\
    PRAGMA foreign_keys = 1;

    CREATE TABLE Node(
        node_oid INTEGER PRIMARY KEY NOT NULL,
        node_id TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT(1));
    CREATE UNIQUE INDEX Node_node_id on Node(node_id);

    CREATE TABLE Job(
        node_oid INTEGER NOT NULL,
        id INTEGER NOT NULL,
        FOREIGN KEY(node_oid) REFERENCES Node(node_oid));
    CREATE UNIQUE INDEX Job_node_oid on Job(node_oid, id);
    """,
    # 4
    # Change index Node_node_id field
    # Delete index Job_node_id
    # Set user_version = 6
    """\
    PRAGMA foreign_keys = 1;

    CREATE TABLE Node(
        node_oid INTEGER PRIMARY KEY NOT NULL,
        node_id TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT(1));
    CREATE UNIQUE INDEX Node_node_id on Node(node_oid);

    CREATE TABLE Job(
        node_oid INTEGER NOT NULL,
        id INTEGER NOT NULL,
        FOREIGN KEY(node_oid) REFERENCES Node(node_oid));
    CREATE UNIQUE INDEX Job_node_oid on Job(node_oid, id);

    PRAGMA user_version = 6;
    """,
    # 5
    # (vs. schema[1]) - Change Node.active default from 1 to 2
    """\
    CREATE TABLE Node(
        node_oid INTEGER PRIMARY KEY NOT NULL,
        node_id TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT(2));
    CREATE UNIQUE INDEX Node_node_id on Node(node_id);
    """,
    # 6
    # Create a table containing a column of the same name
    """\
    CREATE TABLE Name(
        id INTEGER PRIMARY KEY NOT NULL,
        Name TEXT NOT NULL
    );
    CREATE VIEW Names AS SELECT Name from Name;
    """,
]


class MigratorTestCase(unittest.TestCase):

    def dump_sqlite_master(self, db: sqlite3.Connection) -> list:
        out = []
        for type_, name, tbl_name, sql in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ):
            if tbl_name in _INTERNAL_TABLES:
                continue
            out.append(
                {
                    "type": type_,
                    "name": name,
                    "tbl_name": tbl_name,
                    "sql": normalize_sql(sql),
                }
            )
        return sorted(out, key=lambda x: x["name"])

    def assert_schema_equal(self, db: sqlite3.Connection, schema: str) -> None:
        pristine = sqlite3.connect(":memory:")
        pristine.executescript(schema)
        assert self.dump_sqlite_master(pristine) == self.dump_sqlite_master(db)

        pristine_sql = "\n".join(
            sorted(normalize_sql(x) for x in pristine.iterdump()),
        )
        db_sql = "\n".join(sorted(normalize_sql(x) for x in db.iterdump()))
        assert pristine_sql == db_sql

        for pragma in ["user_version", "foreign_keys"]:
            assert (
                pristine.execute(f"PRAGMA {pragma}").fetchone()[0]
                == db.execute(f"PRAGMA {pragma}").fetchone()[0]
            ), f"Value for PRAGMA {pragma} does not match"

    @parameterized.expand(
        [
            (0, 0, False),
            (0, 1, False),
            (0, 2, False),
            (0, 3, False),
            (0, 4, False),
            (1, 0, True),
            (1, 1, False),
            (1, 2, False),
            (1, 3, False),
            (1, 4, False),
            (2, 0, True),
            (2, 1, True),
            (2, 2, False),
            (2, 3, True),
            (2, 4, True),
            (3, 0, True),
            (3, 1, True),
            (3, 2, False),
            (3, 3, False),
            (3, 4, False),
            (0, 6, False),
            (6, 6, False),
            (6, 0, True),
            (6, 5, True),
            (5, 6, True),
        ]
    )
    def test_dumb_schema_migration(
        self, start: int, end: int, need_allow_deletions: bool
    ) -> None:
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.assert_schema_equal(db, _TEST_SCHEMAS[0])

        logging.info("Testing from %s to %s", start, end)
        db.executescript(_TEST_SCHEMAS[start])
        if need_allow_deletions:
            with pytest.raises(RuntimeError):
                dumb_migrate_db(db, _TEST_SCHEMAS[end])
            # The transaction should make the RuntimeError above revert any
            # work in progress
            self.assert_schema_equal(db, _TEST_SCHEMAS[start])
        changed = dumb_migrate_db(
            db, _TEST_SCHEMAS[end], allow_deletions=need_allow_deletions
        )
        assert changed == (start != end)
        self.assert_schema_equal(db, _TEST_SCHEMAS[end])

        assert not dumb_migrate_db(db, _TEST_SCHEMAS[end])

    def test_dumb_data_migration(self) -> None:
        # Check that data is preserved during the migration:
        db = sqlite3.connect(":memory:", isolation_level=None)
        db.executescript(_TEST_SCHEMAS[1])
        db.executemany(
            """\
            INSERT INTO Node(node_oid, node_id)
            VALUES (?, ?)""",
            [
                (0, 0),
                (1, 100),
            ],
        )
        assert db.execute("SELECT node_oid, node_id FROM Node").fetchall() == [
            (0, 0),
            (1, 100),
        ]

        dumb_migrate_db(db, _TEST_SCHEMAS[2])
        assert db.execute("SELECT node_oid, node_id, active FROM Node").fetchall() == [
            (0, "0", 1),
            (1, "100", 1),
        ]

        db.execute('UPDATE Node SET active = 0, node_id = "abc" WHERE node_oid == 0')

        # Insert Job data.  It has a FOREIGN KEY back into Node.  We want
        # to be sure that this FOREIGN KEY isn't confused by the migration
        db.executemany(
            """\
            INSERT INTO Job(node_oid, id)
            VALUES (?, ?)""",
            [
                (0, 1234),
                (0, 5432),
                (1, 1234),
                (1, 9876),
            ],
        )
        assert (
            db.execute(
                """\
                SELECT node_id, id
                FROM Job
                INNER JOIN Node ON Node.node_oid == Job.node_oid"""
            ).fetchall()
            == [
                ("abc", 1234),
                ("abc", 5432),
                ("100", 1234),
                ("100", 9876),
            ]
        )

        dumb_migrate_db(db, _TEST_SCHEMAS[3], allow_deletions=True)

        assert (
            db.execute(
                """\
                SELECT node_id, id
                FROM Job
                INNER JOIN Node ON Node.node_oid == Job.node_oid"""
            ).fetchall()
            == [
                ("abc", 1234),
                ("abc", 5432),
                ("100", 1234),
                ("100", 9876),
            ]
        )

        # The new default for active should not affect existing rows
        # with default values:
        dumb_migrate_db(db, _TEST_SCHEMAS[4])
        assert db.execute("SELECT node_oid, node_id, active FROM Node").fetchall() == [
            (0, "abc", 0),
            (1, "100", 1),
        ]

        db.execute('UPDATE Node SET active = 0, node_id = "0" WHERE node_oid == 0')

        # And delete the active column again removing the data:
        dumb_migrate_db(db, _TEST_SCHEMAS[1], allow_deletions=True)
        assert db.execute("SELECT node_oid, node_id FROM Node").fetchall() == [
            (0, 0),
            (1, 100),
        ]
