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
import re
import sqlite3

from os import PathLike
from types import TracebackType


class dbopen:
    def __init__(self, filename: PathLike, *args, **kwargs) -> None:
        self._filename = filename
        self._args = args
        self._kwargs = kwargs

    def __enter__(self) -> sqlite3.Connection:
        self._db = sqlite3.connect(self._filename, *self._args, **self._kwargs)
        return self._db

    def __exit__(
        self,
        exc_type: BaseException | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._db.commit()
        self._db.close()


def normalize_sql(sql: str) -> str:
    # Remove comments:
    sql = re.sub(r"--[^\n]*\n", "", sql)
    # Normalise whitespace:
    sql = re.sub(r"\s+", " ", sql)
    sql = re.sub(r" *([(),]) *", r"\1", sql)
    # Remove unnecessary quotes
    sql = re.sub(r'"(\w+)"', r"\1", sql)
    return sql.strip()
