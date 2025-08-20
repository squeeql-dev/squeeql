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
import sqlite3

from time import sleep

from squeeql import utils


def test_dbopen() -> None:
    dbpath = "file:database?mode=memory&cache=shared"
    # For memory backed database testing, of multilpe connections,
    # we must hold a connection open, but test via the with statement
    _con = sqlite3.connect(dbpath)
    with utils.dbopen(dbpath) as db:
        db.execute("CREATE TABLE Node(id INTEGER PRIMARY KEY NOT NULL)")
        db.executemany("INSERT INTO Node VALUES (?)", [(0,), (1,), (2,)])
    with utils.dbopen(dbpath) as db:
        assert db.execute("SELECT id FROM Node").fetchall() == [
            (0,),
            (1,),
            (2,),
        ]
    _con.close()


def test_normalize_sql() -> None:
    assert (
        utils.normalize_sql(
            """\
            CREATE TABLE "Node"( -- This is my table
            -- There are many like it but this one is mine
            A b, C D, "E F G", h)"""
        )
        == 'CREATE TABLE Node(A b,C D,"E F G",h)'
    )
