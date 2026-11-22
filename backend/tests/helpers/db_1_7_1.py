# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Build the database of a 1.7.1 installation in a temp file (Task 8b, #335).

The file is generated per test, never committed. Its schema comes from Alembic at the 1.7.1
head (``20261118a2``); its content from the 1.7.x seeder (``run_sheet_seed``, which 1.8.0 no
longer runs at start) and the type and sheet services, run on a staging database and copied
column by column into the 1.7.1 schema. A column the 1.7.1 schema does not have is not copied,
so the result holds exactly what a 1.7.1 hub could have written.

``build_db_1_7_1`` gives the usual installation: the shipped seed, one seeded type with an
edited description, a custom type and one sheet. ``seeds`` replays older seed files first (an
installation that started on an older release), and ``prepare`` runs before the seed (a user's
own type of a key a later seed adopts).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.db.engine import _apply_pragmas
from app.models.sheet import SheetType
from app.seed import sheet_seed
from app.sheets import sheet_service as ss
from app.sheets import type_service as ts
from app.sheets.geometry import Geometry
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
HEAD_1_7_1 = "20261118a2"

EDITED_KEY = "avery-7160"
EDITED_DESCRIPTION = "Address labels for the workshop drawers"
CUSTOM_KEY = "workshop-bins"
SHEET_TYPE_KEY = "avery-5160"  # an unedited seeded type that has a sheet

Factory = async_sessionmaker[AsyncSession]
Prepare = Callable[[Factory], Awaitable[None]]

CUSTOM_GEOMETRY = Geometry(
    page_size="A4",
    page_w=21000,
    page_h=29700,
    label_w=6000,
    label_h=3000,
    cols=3,
    rows=8,
    pitch_x=6500,
    pitch_y=3300,
    margin_top=1500,
    margin_left=1000,
    corner_radius=None,
    shape="rectangle",
)


def alembic_config(db: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    cfg.attributes["configure_logger"] = False
    return cfg


async def migrate(db: Path, revision: str) -> None:
    """``alembic upgrade`` (or ``downgrade`` with a leading ``-``) outside the running loop."""
    if revision.startswith("-"):
        await asyncio.to_thread(command.downgrade, alembic_config(db), revision[1:])
    else:
        await asyncio.to_thread(command.upgrade, alembic_config(db), revision)


def factory_for(db: Path) -> tuple[Factory, Callable[[], Awaitable[None]]]:
    """A session factory on ``db`` with the hub's pragmas, and the call that disposes it."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db}", connect_args={"check_same_thread": False}
    )
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return async_sessionmaker(engine, expire_on_commit=False), engine.dispose


async def seed_with(factory: Factory, path: Path | None) -> None:
    """One run of the 1.7.x seeder, with the seed file ``path`` (``None``: the shipped one)."""
    if path is None:
        await sheet_seed.run_sheet_seed(factory)
        return
    real = sheet_seed.load_seed
    try:
        sheet_seed.load_seed = lambda p=path: real(p)  # type: ignore[assignment]
        await sheet_seed.run_sheet_seed(factory)
    finally:
        sheet_seed.load_seed = real


async def _populate(factory: Factory) -> None:
    async with factory() as s:
        await s.execute(
            update(SheetType)
            .where(col(SheetType.key) == EDITED_KEY)
            .values(description=EDITED_DESCRIPTION)
        )
        await s.commit()
    fields = ts.TypeFields(
        key=CUSTOM_KEY,
        prefix="WB",
        vendor=None,
        product_codes=[],
        description="Bins on the shelf",
        allow_qr=None,
    )
    async with factory() as s:
        await ts.create_type(s, fields, CUSTOM_GEOMETRY, actor="user:admin", origin="custom")
    async with factory() as s:
        await ss.create_sheet(
            s, type_key=SHEET_TYPE_KEY, version=None, used_cells=[], note=None, actor="user:admin"
        )


def _copy_into(dest: Path, staging: Path) -> None:
    """Copy every row of the staging database into the tables and columns ``dest`` has.

    A row the migrations already wrote (the sheet write lock row) is kept as it is.
    """
    con = sqlite3.connect(dest)
    try:
        con.execute("ATTACH DATABASE ? AS staging", (str(staging),))
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        staged = {
            r[0] for r in con.execute("SELECT name FROM staging.sqlite_master WHERE type='table'")
        }
        for table in tables:
            if table not in staged:
                continue
            ours = [r[1] for r in con.execute(f'PRAGMA main.table_info("{table}")')]
            theirs = {r[1] for r in con.execute(f'PRAGMA staging.table_info("{table}")')}
            cols = ", ".join(f'"{c}"' for c in ours if c in theirs)
            con.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({cols}) '
                f'SELECT {cols} FROM staging."{table}"'
            )
        con.commit()
    finally:
        con.close()


async def build_db_1_7_1(
    dest: Path,
    *,
    seeds: Sequence[Path | None] = (None,),
    prepare: Prepare | None = None,
    populate: bool = True,
) -> Path:
    """A database file at ``20261118a2`` holding a 1.7.1 installation's rows."""
    import app.models  # noqa: F401 — registers every table with SQLModel.metadata

    staging = dest.with_name(dest.stem + ".staging.db")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{staging}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if prepare is not None:
            await prepare(factory)
        for seed in seeds:
            await seed_with(factory, seed)
        if populate:
            await _populate(factory)
    finally:
        await engine.dispose()
    await migrate(dest, HEAD_1_7_1)
    await asyncio.to_thread(_copy_into, dest, staging)
    staging.unlink()
    return dest
