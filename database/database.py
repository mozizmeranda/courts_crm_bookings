# database.py
import aiosqlite
import os
import json
import secrets
from typing import Optional, List, Tuple, Any
import asyncio
import logging
from datetime import datetime, timedelta
from functools import wraps
import traceback
from utils import notify_admin
from config.config import courts, BOT_LINK
from datetime import date as date_type
from zoneinfo import ZoneInfo
from calendar_api.grid_utils import _build_cancelled_pairs, _build_grid

logger = logging.getLogger(__name__)
tashkent_tz = ZoneInfo("Asia/Tashkent")

# За сколько часов до начала игры бронь ещё можно перенести
RESCHEDULE_MIN_HOURS = 6


def slot_start_tashkent(date_str: str, time_slot: str) -> datetime:
    """'2026-11-10', '14:00-15:00' -> datetime начала слота в часовом поясе Ташкента."""
    start_str = time_slot.split("-")[0]
    naive = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=tashkent_tz)


def serialized_transaction(func):
    """Декоратор, который гарантирует, что методы изменения данных

    выполняются строго по очереди (100% защита от гонок данных).
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with self._tx_lock:
            return await func(self, *args, **kwargs)
    return wrapper


class Database:

    def __init__(self, db_name: str = "users.db"):
        self.path_to_db = os.path.join(os.path.dirname(__file__), db_name)
        self.connection: aiosqlite.Connection
        self._tx_lock = asyncio.Lock()

    async def connect(self):
        """Открывает одно глобальное соединение"""
        self.connection = await aiosqlite.connect(self.path_to_db)
        await self.connection.execute("PRAGMA foreign_keys = ON;")
        await self.connection.execute("PRAGMA busy_timeout = 5000;")
        await self._run_crm_migrations()

    async def _run_crm_migrations(self):
        """Безопасные миграции таблиц и колонок для CRM."""
        create_statements = [
            """
            CREATE TABLE IF NOT EXISTS crm_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                phone TEXT NULL,
                telegram_id INTEGER UNIQUE NULL,
                invite_token TEXT UNIQUE NULL,
                status TEXT NULL,
                next_payment_date DATE NULL,
                payment_amount REAL NULL,
                trainer_id INTEGER NULL,
                calendar_id TEXT NULL,
                notes TEXT NULL,
                coach_comment TEXT NULL,
                skill_level TEXT NULL,
                skills_json TEXT NULL,
                polls_enabled INTEGER DEFAULT 1,
                last_notif_7d_date DATE NULL,
                last_notif_3d_date DATE NULL,
                created_by INTEGER NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                trainer_id INTEGER NULL,
                calendar_id TEXT NULL,
                notes TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trainer_id) REFERENCES calendar_users (telegram_id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_group_members (
                group_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, student_id),
                FOREIGN KEY (group_id) REFERENCES crm_groups (id) ON DELETE CASCADE,
                FOREIGN KEY (student_id) REFERENCES crm_students (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                single_event_id INTEGER NULL,
                recurring_event_id INTEGER NULL,
                event_date DATE NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                poll_sent INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES crm_students (id) ON DELETE CASCADE,
                FOREIGN KEY (single_event_id) REFERENCES single_events (id) ON DELETE CASCADE,
                FOREIGN KEY (recurring_event_id) REFERENCES recurring_events (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_student_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                trainer_id INTEGER NULL,
                calendar_id TEXT NULL,
                amount REAL DEFAULT 0,
                paid_at DATE NOT NULL,
                next_payment_date DATE NULL,
                comment TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES crm_students (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_trainer_rent_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendar_id TEXT NOT NULL,
                trainer_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                paid_at DATE NOT NULL,
                next_payment_date DATE NULL,
                comment TEXT NULL,
                created_by INTEGER NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (calendar_id) REFERENCES calendars (id) ON DELETE CASCADE,
                FOREIGN KEY (trainer_id) REFERENCES calendar_users (telegram_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS crm_poll_schedule_rules (
                event_hour INTEGER PRIMARY KEY,
                send_time TEXT NOT NULL,
                days_before INTEGER NOT NULL DEFAULT 0
            )
            """,
        ]
        for stmt in create_statements:
            await self.connection.execute(stmt)

        alter_statements = [
            "ALTER TABLE calendar_users ADD COLUMN phone TEXT NULL",
            "ALTER TABLE calendar_users ADD COLUMN bio TEXT NULL",
            "ALTER TABLE calendar_users ADD COLUMN photo_url TEXT NULL",
            "ALTER TABLE calendar_users ADD COLUMN is_independent INTEGER DEFAULT 0",
            "ALTER TABLE calendar_members ADD COLUMN role TEXT DEFAULT 'trainer'",
            "ALTER TABLE calendar_members ADD COLUMN can_edit_schedule INTEGER DEFAULT 1",
            "ALTER TABLE calendar_members ADD COLUMN employment_type TEXT DEFAULT 'club'",
            "ALTER TABLE single_events ADD COLUMN event_type TEXT NULL",
            "ALTER TABLE single_events ADD COLUMN student_id INTEGER NULL",
            "ALTER TABLE single_events ADD COLUMN group_id INTEGER NULL",
            "ALTER TABLE single_events ADD COLUMN trainer_id INTEGER NULL",
            "ALTER TABLE single_events ADD COLUMN send_poll INTEGER DEFAULT 0",
            "ALTER TABLE single_events ADD COLUMN poll_send_time TEXT NULL",
            "ALTER TABLE single_events ADD COLUMN poll_days_before INTEGER DEFAULT 0",
            "ALTER TABLE recurring_events ADD COLUMN event_type TEXT NULL",
            "ALTER TABLE recurring_events ADD COLUMN student_id INTEGER NULL",
            "ALTER TABLE recurring_events ADD COLUMN group_id INTEGER NULL",
            "ALTER TABLE recurring_events ADD COLUMN trainer_id INTEGER NULL",
            "ALTER TABLE recurring_events ADD COLUMN send_poll INTEGER DEFAULT 0",
            "ALTER TABLE recurring_events ADD COLUMN poll_send_time TEXT NULL",
            "ALTER TABLE recurring_events ADD COLUMN poll_days_before INTEGER DEFAULT 0",
        ]
        for stmt in alter_statements:
            try:
                await self.connection.execute(stmt)
            except Exception:
                pass  # Колонка уже существует — игнорируем

        # Заполняем дефолтные правила отправки опросов по часам (если таблица пуста)
        # Утренние тренировки (06:00 - 10:00) -> опрос в 20:00 предыдущего дня (days_before=1)
        # Дневные/вечерние (11:00 - 23:00) -> опрос за 4 часа до начала в тот же день (days_before=0)
        async with self.connection.execute("SELECT COUNT(*) FROM crm_poll_schedule_rules") as cur:
            row = await cur.fetchone()
        if not row or row[0] == 0:
            for h in range(0, 24):
                if h <= 10:
                    await self.connection.execute(
                        "INSERT OR IGNORE INTO crm_poll_schedule_rules (event_hour, send_time, days_before) VALUES (?, '20:00', 1)",
                        (h,),
                    )
                else:
                    send_h = max(8, h - 4)
                    await self.connection.execute(
                        "INSERT OR IGNORE INTO crm_poll_schedule_rules (event_hour, send_time, days_before) VALUES (?, ?, 0)",
                        (h, f"{send_h:02d}:00"),
                    )

        await self.connection.commit()

    async def close(self):
        """Закрывает соединение"""
        if self.connection:
            await self.connection.close()

    async def execute(
            self,
            sql: str,
            parameters: Optional[tuple] = None,
            fetchone: bool = False,
            fetchall: bool = False,
            commit: bool = False
    ) -> Any:
        if not parameters:
            parameters = tuple()

        if not self.connection:
            raise RuntimeError("База данных не инициализирована! Вызовите db.connect() в lifespan.")

        async with self.connection.execute(sql, parameters) as cursor:
            data = None
            if fetchone:
                data = await cursor.fetchone()
            elif fetchall:
                data = await cursor.fetchall()

            if commit:
                await self.connection.commit()

            return data

    @serialized_transaction
    async def insert_into(self, id: int, name: str, username: str, number: str, language: str):
        sql = "INSERT OR IGNORE INTO users (id, name, username, number, language, OfferOk) VALUES (?, ?, ?, ?, ?, ?)"
        params = (id, name, username, number, language, 1)
        await self.execute(sql, parameters=params, commit=True)

    async def get_user_data_by_id(self, id: int) -> Optional[Tuple[str, str]]:
        sql = "SELECT number, username FROM users WHERE id=?"
        params = (id,)
        return await self.execute(sql, parameters=params, fetchone=True)

    async def get_number_by_id(self, id: int) -> Optional[str]:
        sql = "SELECT number FROM users WHERE id=?"
        params = (id,)
        result = await self.execute(sql, parameters=params, fetchone=True)
        return result[0] if result else None

    async def get_all_users(self) -> List[Tuple[int]]:
        sql = "SELECT id FROM users"
        return await self.execute(sql, fetchall=True)

    @serialized_transaction
    async def delete_booking(self, order_id: str) -> bool:
        sql = """
        DELETE FROM bookings 
        WHERE order_id = ?
        """
        params = (order_id, )
        await self.execute(sql, parameters=params, commit=True)
        return True

    async def check_access_for_reschedule(self, telegram_id: int, booking_id: str, old_slots: List[str]) -> int:
        """
        Проверяет, можно ли переносить указанные слоты брони.

        Возвращает:
             1 — можно переносить
             0 — бронь не найдена / принадлежит другому пользователю
            -1 — внутренняя ошибка
            -2 — до начала одного из переносимых слотов меньше RESCHEDULE_MIN_HOURS часов
            -3 — среди old_slots есть слот, которого нет в брони

        Ограничение по времени проверяется только для переносимых слотов:
        остальные слоты брони остаются на месте и не влияют на результат.

        Даты в БД хранятся по Ташкенту, поэтому и "сейчас" берём по Ташкенту
        (время сервера — немецкое, на него опираться нельзя).
        """
        sql = "SELECT booking_date, time_slot FROM bookings WHERE telegram_id = ? AND booking_id = ?"
        try:
            rows = await self.execute(sql, (telegram_id, booking_id), fetchall=True)
            if not rows:
                return 0

            date_by_slot = {slot: date_str for date_str, slot in rows}
            if any(slot not in date_by_slot for slot in old_slots):
                return -3

            earliest_start = min(slot_start_tashkent(date_by_slot[slot], slot) for slot in old_slots)
            if earliest_start - datetime.now(tashkent_tz) < timedelta(hours=RESCHEDULE_MIN_HOURS):
                return -2

            return 1
        except Exception:
            logger.exception("check_access_for_reschedule failed: booking_id=%s", booking_id)
            return -1

    async def get_lang(self, tg_id: int) -> Optional[str]:
        sql = "SELECT language FROM users WHERE id=?"
        params = (tg_id,)
        result = await self.execute(sql, parameters=params, fetchone=True)
        return result[0] if result else None

    async def get_by_booking_id(self, booking_id: str) -> List[tuple]:
        sql = "SELECT * FROM pending_bookings WHERE booking_id=?"
        params = (booking_id,)
        return await self.execute(sql, parameters=params, fetchall=True)

    @serialized_transaction
    async def kill_from_pending_bookings(self, booking_id: str):
        sql = "DELETE FROM pending_bookings WHERE booking_id=?"
        params = (booking_id,)
        await self.execute(sql, parameters=params, commit=True)

    async def get_all_bookings(self, telegram_id: int) -> List[tuple]:
        # БЕЗОПАСНО: Исправили интерполяцию строк f"{}" на безопасные параметры ?
        sql = "SELECT * FROM bookings WHERE telegram_id=?"
        params = (telegram_id,)
        return await self.execute(sql, parameters=params, fetchall=True)

    @serialized_transaction
    async def update_user_profile(self, telegram_id: int, name: Optional[str] = None, number: Optional[str] = None):
        updates = []
        params = []
        if name is not None:
            updates.append("name=?")
            params.append(name)
        if number is not None:
            updates.append("number=?")
            params.append(number)
            
        if not updates:
            return
            
        sql = f"UPDATE users SET {', '.join(updates)} WHERE id=?"
        params.append(telegram_id)
        await self.execute(sql, parameters=tuple(params), commit=True)

    async def get_full_profile(self, telegram_id: int):
        profile_sql = "SELECT name, number FROM users WHERE id=?"

        bookings_sql = """
            SELECT
                location,
                booking_date,
                time_slot,
                booking_id
            FROM bookings
            WHERE telegram_id = ?
            AND datetime(
                booking_date || ' ' ||
                substr(time_slot, 7, 5)
            ) > datetime('now', 'localtime')
            ORDER BY booking_date, time_slot
        """

        profile = await self.execute(
            profile_sql,
            parameters=(telegram_id,),
            fetchone=True
        )

        if not profile:
            return None

        bookings = await self.execute(
            bookings_sql,
            parameters=(telegram_id,),
            fetchall=True
        )

        return {
            "profile": profile,
            "future_bookings": bookings
        }



    async def pendings(self, location: str, day: str, time_slot: str) -> int:
        sql = "SELECT COUNT(*) FROM pending_table WHERE location=? AND booking_date=? AND time_slot=? AND expires_at > ?"
        params = (location, day, time_slot, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        data = await self.execute(sql, parameters=params, fetchone=True)

        sql2 = "SELECT COUNT(*) FROM pending_bookings WHERE location=? AND booking_date=? AND time_slots=?"
        params2 = (location, day, time_slot)
        data2 = await self.execute(sql2, parameters=params2, fetchone=True)

        # Защита от None на случай пустых таблиц
        count1 = data[0] if data else 0
        count2 = data2[0] if data2 else 0
        return count1 + count2

    async def get_user_language(self, user_id: int) -> Optional[str]:
        sql = "SELECT language FROM users WHERE id = ?"
        result = await self.execute(sql, (user_id,), fetchone=True)
        return result[0] if result else None

    async def get_price(self, location: str, time_slot: str) -> Optional[int]:
        sql = "SELECT price FROM price WHERE location = ? AND time_slot = ?"
        result = await self.execute(sql, (location, time_slot), fetchone=True)
        return result if result else None

    async def get_prices_by_location(self, location: str):
        sql = "SELECT * FROM price WHERE location = ?"
        result = await self.execute(sql, (location,), fetchall=True)
        return result if result else None

    @serialized_transaction
    async def update_price(self, location: str, time_slot: str, new_price: int):
        sql = """
           UPDATE price
           SET price = ?
           WHERE location = ? AND time_slot = ?
           """
        await self.execute(sql, (new_price, location, time_slot), commit=True)

    @serialized_transaction
    async def update_user_language(self, user_id: int, new_language: str):
        sql = """
           UPDATE users
           SET language = ?
           WHERE id = ?
           """
        await self.execute(sql, (new_language, user_id), commit=True)

    @serialized_transaction
    async def delete_user_by_id(self, user_id: int) -> bool:
        sql = "DELETE FROM users WHERE id=?"
        params = (user_id,)
        await self.execute(sql, parameters=params, commit=True)
        return True

    async def get_prices_bulk(self, location: str, time_slots: list) -> list:
        if not time_slots:
            return []
        placeholders = ', '.join(['?'] * len(time_slots))
        sql = f"SELECT price FROM price WHERE location = ? AND time_slot IN ({placeholders})"
        params = (location, *time_slots)
        results = await self.execute(sql, params, fetchall=True)
        return [r[0] for r in results] if results else []

    @staticmethod
    def _slot_to_datetimes(date_str: str, slot_time: str) -> Optional[Tuple[str, str]]:
        """Та же логика, что в telegram_bot/admin.py: 'HH:MM-HH:MM' -> (start_dt, end_dt)."""
        parts = slot_time.split("-")
        if len(parts) != 2:
            return None
        start_str, end_str = parts
        if end_str == "00:00":
            dt_obj = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
            end_dt = f"{dt_obj.strftime('%Y-%m-%d')} 00:00:00"
        else:
            end_dt = f"{date_str} {end_str}:00"
        return f"{date_str} {start_str}:00", end_dt

    async def _slot_occupancy(self, location: str, booking_date: str, time_slot: str) -> Tuple[int, int]:
        """
        Возвращает (занято, вместимость). НЕ коммитит и НЕ берёт lock —
        вызывать только внутри открытой транзакции из @serialized_transaction метода.
        Ошибки пробрасываются наружу (чтобы вызывающий откатил транзакцию),
        а не глотаются — иначе занятость будет занижена.
        """
        calendar_row = await self.execute(
            "SELECT max_events_per_hour FROM calendars WHERE id=?", (location,), fetchone=True
        )
        max_capacity = calendar_row[0] if calendar_row else courts.get(location, 1)

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        r = await self.execute(
            """SELECT COUNT(*) FROM pending_table
               WHERE location=? AND booking_date=? AND time_slot=? AND expires_at > ?""",
            (location, booking_date, time_slot, now), fetchone=True,
        )
        n_pending_table = r[0] if r else 0

        r = await self.execute(
            "SELECT COUNT(*) FROM pending_bookings WHERE location=? AND booking_date=? AND time_slots=?",
            (location, booking_date, time_slot), fetchone=True,
        )
        n_pending_bookings = r[0] if r else 0

        dts = self._slot_to_datetimes(booking_date, time_slot)
        if not dts:
            raise ValueError(f"Bad time_slot: {time_slot}")
        start_dt, end_dt = dts

        r = await self.execute(
            """SELECT COUNT(*) FROM single_events
               WHERE calendar_id=? AND start_datetime < ? AND end_datetime > ? AND status='confirmed'""",
            (location, end_dt, start_dt), fetchone=True,
        )
        n_single = r[0] if r else 0

        n_recurring = 0
        weekday = datetime.strptime(booking_date, "%Y-%m-%d").weekday()
        recurring_rows = await self.execute(
            "SELECT id, start_time, end_time FROM recurring_events WHERE calendar_id=? AND day_of_week=?",
            (location, weekday), fetchall=True,
        )
        if recurring_rows:
            cancelled_rows = await self.execute(
                """SELECT recurring_event_id FROM single_events
                   WHERE calendar_id=? AND start_datetime < ? AND end_datetime > ?
                     AND status='cancelled' AND recurring_event_id IS NOT NULL""",
                (location, end_dt, start_dt), fetchall=True,
            )
            cancelled_ids = {c[0] for c in cancelled_rows} if cancelled_rows else set()
            next_day = (datetime.strptime(booking_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            for re_id, r_start, r_end in recurring_rows:
                if re_id in cancelled_ids:
                    continue
                r_start = r_start if len(r_start) == 8 else f"{r_start}:00"
                r_end = r_end if len(r_end) == 8 else f"{r_end}:00"
                re_start_dt = f"{booking_date} {r_start}"
                re_end_dt = f"{next_day} 00:00:00" if r_end == "00:00:00" else f"{booking_date} {r_end}"
                if re_start_dt < end_dt and re_end_dt > start_dt:
                    n_recurring += 1

        return n_pending_table + n_pending_bookings + n_single + n_recurring, max_capacity

    @serialized_transaction
    async def create_pending_multi(
        self,
        temporary_order_id: str,
        location: str,
        booking_date: str,
        time_slots: List[str],
        telegram_id: int,
        expires_at: str,
    ) -> dict:
        """
        Всё-или-ничего: либо созданы холды на ВСЕ слоты, либо ни одного.
        Возвращает {"ok": True} или {"ok": False, "reason": "occupied", "slot": ...}.
        Любая другая ошибка -> rollback + исключение.
        """
        conn = self.connection
        try:
            await conn.execute("BEGIN IMMEDIATE")
            for slot in time_slots:
                occupied, capacity = await self._slot_occupancy(location, booking_date, slot)
                if occupied >= capacity:
                    await conn.rollback()
                    return {"ok": False, "reason": "occupied", "slot": slot}
                await conn.execute(
                    """INSERT INTO pending_table
                           (temporary_order_id, location, booking_date, time_slot, telegram_id, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (temporary_order_id, location, booking_date, slot, telegram_id, expires_at),
                )
            await conn.commit()
            return {"ok": True}
        except Exception:
            await conn.rollback()
            logger.exception("create_pending_multi failed: order_id=%s", temporary_order_id)
            raise

    @serialized_transaction
    async def confirm_pending_booking(self, order_id: str, title: str) -> dict:
        """
        Только для PlayOn 
        Атомарно: pending_table -> single_events + bookings, затем удаление холдов.
        Идемпотентно: повторный вызов вернёт {"status": "already"}.
        Возвращает {"status": "created" | "already" | "not_found"}.
        """
        conn = self.connection
        try:
            await conn.execute("BEGIN IMMEDIATE")

            async with conn.execute("SELECT 1 FROM bookings WHERE booking_id=? LIMIT 1", (order_id,)) as cur:
                if await cur.fetchone():
                    await conn.rollback()
                    return {"status": "already"}

            async with conn.execute(
                "SELECT location, booking_date, time_slot FROM pending_table WHERE temporary_order_id=?",
                (order_id,),
            ) as cur:
                holds = await cur.fetchall()
            if not holds:
                await conn.rollback()
                return {"status": "not_found"}

            for rec in holds:
                location = rec[0]
                date_str = rec[1]
                slot_time = rec[2]

                dts = self._slot_to_datetimes(date_str, slot_time)
                event_id = None
                if dts:
                    cursor = await conn.execute(
                        """INSERT INTO single_events
                               (calendar_id, created_by, title, start_datetime, end_datetime, status)
                           VALUES (?, NULL, ?, ?, ?, 'confirmed')""",
                        (location, title, dts[0], dts[1]),
                    )
                    event_id = cursor.lastrowid

                async with conn.execute(
                    "SELECT price FROM price WHERE location=? AND time_slot=?", (location, slot_time)
                ) as cur:
                    prow = await cur.fetchone()
                price = prow[0] if prow else 0

                await conn.execute(
                    """INSERT INTO bookings
                           (telegram_id, location, booking_date, time_slot, screenshot_path, price, booking_id, calendar_id)
                       VALUES (?, ?, ?, ?, '', ?, ?, ?)""",
                    (0, location, date_str, slot_time, price, order_id, event_id),
                )

            await conn.execute("DELETE FROM pending_table WHERE temporary_order_id=?", (order_id,))
            await conn.commit()
            return {"status": "created"}
        except Exception:
            await conn.rollback()
            logger.exception("confirm_pending_booking failed: order_id=%s", order_id)
            raise

    @serialized_transaction
    async def create_pending(
        self,
        slot_quantity: int,
        temporary_order_id: str,
        location: str,
        booking_date: str,
        time_slot: str,
        telegram_id: int,
        expires_at: str,
    ):
        """
        Атомарно создаёт временный холд (запись в pending_table).

        Защита от race conditions:
          - asyncio.Lock (через @serialized_transaction) — только 1 запрос одновременно
          - BEGIN IMMEDIATE — write-lock на уровне SQLite

        Проверяет 3 источника занятости:
          1. pending_table    — активные холды
          2. pending_bookings — старые pending-брони
          3. single_events    — подтверждённые брони из календаря

        slot_quantity принимается от клиента для каждого конкретного слота, 
        но ограничивается сверху значением из конфига (защита от подделки).

        Возвращает True если холд создан, 0 если слот занят, 0 при ошибке.
        """

        calendar_id = location
        calendar_row = await self.execute("SELECT max_events_per_hour FROM calendars WHERE id=?", (calendar_id,), fetchone=True)
        max_capacity = calendar_row[0] if calendar_row else courts.get(location, 1)

        try:
            await self.connection.execute("BEGIN IMMEDIATE")

            # 1. Активные холды в pending_table
            res_table = await self.execute(
                """
                SELECT COUNT(*) FROM pending_table
                WHERE location = ? AND booking_date = ? AND time_slot = ? AND expires_at > ?
                """,
                (location, booking_date, time_slot, datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                fetchone=True,
            )
            n_pending_table = res_table[0] if res_table else 0

            # 2. pending_bookings (старый механизм)
            res_bookings = await self.execute(
                "SELECT COUNT(*) FROM pending_bookings WHERE location = ? AND booking_date = ? AND time_slots = ?",
                (location, booking_date, time_slot),
                fetchone=True,
            )
            n_pending_bookings = res_bookings[0] if res_bookings else 0

            # 3. Подтверждённые брони из календаря (single_events + recurring_events) — источник правды
            n_single_events = 0
            n_recurring_events = 0
            if calendar_id is not None:
                # Слот формата "HH:MM-HH:MM" → парсим start/end
                parts = time_slot.split("-")
                start_str = parts[0]
                end_str = parts[1]
                start_dt = f"{booking_date} {start_str}:00"
                if end_str == "00:00":
                    dt_obj = datetime.strptime(booking_date, "%Y-%m-%d") + timedelta(days=1)
                    end_dt = f"{dt_obj.strftime('%Y-%m-%d')} 00:00:00"
                else:
                    end_dt = f"{booking_date} {end_str}:00"

                # a) Одиночные события (single_events)
                res_single = await self.execute(
                    """
                    SELECT COUNT(*) FROM single_events
                    WHERE calendar_id = ?
                      AND start_datetime < ?
                      AND end_datetime > ?
                      AND status IN ('confirmed')
                    """,
                    (calendar_id, end_dt, start_dt),
                    fetchone=True,
                )
                n_single_events = res_single[0] if res_single else 0

                # b) Повторяющиеся события (recurring_events)
                try:
                    weekday = datetime.strptime(booking_date, "%Y-%m-%d").weekday()
                    recurring_rows = await self.execute(
                        """
                        SELECT id, start_time, end_time FROM recurring_events
                        WHERE calendar_id = ? AND day_of_week = ?
                        """,
                        (calendar_id, weekday),
                        fetchall=True,
                    )
                    if recurring_rows:
                        cancelled_rows = await self.execute(
                            """
                            SELECT recurring_event_id FROM single_events
                            WHERE calendar_id = ?
                              AND start_datetime < ?
                              AND end_datetime > ?
                              AND status = 'cancelled'
                              AND recurring_event_id IS NOT NULL
                            """,
                            (calendar_id, end_dt, start_dt),
                            fetchall=True,
                        )
                        cancelled_ids = {r[0] for r in cancelled_rows} if cancelled_rows else set()

                        for re in recurring_rows:
                            re_id, r_start, r_end = re[0], re[1], re[2]
                            if re_id in cancelled_ids:
                                continue

                            r_start_fmt = r_start if len(r_start) == 8 else f"{r_start}:00"
                            r_end_fmt = r_end if len(r_end) == 8 else f"{r_end}:00"

                            re_start_dt = f"{booking_date} {r_start_fmt}"
                            if r_end_fmt == "00:00:00":
                                next_day = (datetime.strptime(booking_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                                re_end_dt = f"{next_day} 00:00:00"
                            else:
                                re_end_dt = f"{booking_date} {r_end_fmt}"

                            if re_start_dt < end_dt and re_end_dt > start_dt:
                                n_recurring_events += 1
                except Exception as ex:
                    logger.error("Error querying recurring_events in create_pending: %s", ex)

            total_occupied = n_pending_table + n_pending_bookings + n_single_events + n_recurring_events

            if total_occupied >= max_capacity:
                await self.connection.rollback()
                return 0

            await self.connection.execute(
                """INSERT INTO pending_table
                       (temporary_order_id, location, booking_date, time_slot, telegram_id, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (temporary_order_id, location, booking_date, time_slot, telegram_id, expires_at),
            )
            await self.connection.commit()
            return True

        except aiosqlite.IntegrityError:
            await self.connection.rollback()
            return 0

        except Exception as exp:
            await self.connection.rollback()
            logger.exception(
                "Error while creating pending: telegram_id=%s, temporary_order_id=%s, exp=%s",
                telegram_id, temporary_order_id, exp,
            )
    @serialized_transaction
    async def create_pending_booking(self, booking_id: str, location: str, day: str, time_slot: str, telegram_id: int):
        sql = "INSERT INTO pending_bookings(booking_id, location, booking_date, time_slots, telegram_ID) VALUES (?, ?, ?, ?, ?)"
        params = (booking_id, location, day, time_slot, telegram_id)
        await self.execute(sql, parameters=params, commit=True)

    @serialized_transaction
    async def cancel_pending_by_order_id(self, temporary_order_id: str) -> None:
        """
        Удаляет все холды в pending_table для данного temporary_order_id.
        Используется для отката частично записанных слотов при ошибке создания брони.
        """
        await self.execute(
            "DELETE FROM pending_table WHERE temporary_order_id = ?",
            (temporary_order_id,),
            commit=True,
        )

    @serialized_transaction
    async def save_message_id(self, telegram_id: int, message_id: int, chat_id: int):
        sql = "INSERT INTO message_ids(telegram_id, message_id, chat_id) VALUES (?, ?, ?)"
        params = (telegram_id, message_id, chat_id)
        await self.execute(sql, parameters=params, commit=True)

    async def get_booked_slots(self, location: str, date_str: str) -> dict:
        """
        Возвращает список занятых time_slot для данной локации и даты.
        Учитывает как single_events, так и recurring_events (с учётом отмен).
        Разбивает события любой длительности (в т.ч. многочасовые) на 1-часовые слоты из TIME_SLOTS.
        pending_table учитывается отдельно через db.pendings().
        """
        from config.config import TIME_SLOTS

        calendar_id = location
        if not calendar_id:
            logger.warning("get_booked_slots: unknown location=%s", location)
            return {"max_quantity": 1, "slots": []}

        try:
            target_date = date_type.fromisoformat(date_str)
        except ValueError:
            logger.warning("get_booked_slots: invalid date_str=%s", date_str)
            return {"max_quantity": 1, "slots": []}

        from_str = f"{date_str} 00:00:00"
        to_str = f"{date_str} 23:59:59"

        active = await self.get_single_events_for_calendar(
            calendar_id, from_str, to_str, statuses=["confirmed", "pending_payment"]
        )
        cancelled = await self.get_single_events_for_calendar(
            calendar_id, from_str, to_str, statuses=["cancelled"]
        )
        cancelled_pairs = _build_cancelled_pairs(cancelled)
        recurring = await self.get_recurring_events_for_calendar(calendar_id)

        grid = _build_grid(active, cancelled_pairs, recurring, target_date, target_date)

        row = await self.execute("SELECT max_events_per_hour FROM calendars WHERE id=?", (calendar_id,), fetchone=True)
        max_quantity = row[0] if row else courts.get(location, 1)

        booked_slots = []
        for e in grid:
            try:
                e_start = datetime.fromisoformat(e["start_datetime"])
                e_end = datetime.fromisoformat(e["end_datetime"])
            except Exception:
                continue

            for slot in TIME_SLOTS:
                parts = slot.split("-")
                s_start_str, s_end_str = parts[0], parts[1]
                slot_start = datetime.fromisoformat(f"{date_str} {s_start_str}:00")
                if s_end_str == "00:00":
                    next_d = target_date + timedelta(days=1)
                    slot_end = datetime.fromisoformat(f"{next_d.isoformat()} 00:00:00")
                else:
                    slot_end = datetime.fromisoformat(f"{date_str} {s_end_str}:00")

                if e_start < slot_end and e_end > slot_start:
                    booked_slots.append(slot)

        return {
            "max_quantity": max_quantity,
            "slots": booked_slots
        }

    # ---------------------------------------------------------
    # Calendar DB
    # ---------------------------------------------------------

    async def check_calendar_exists(self, calendar_id: str) -> bool:
        row = await self.execute("SELECT id FROM calendars WHERE id=?", (calendar_id,), fetchone=True)
        return row is not None

    async def get_user(self, telegram_id: int) -> Optional[dict]:
        sql = "SELECT telegram_id, username, full_name, phone, bio, photo_url, is_independent, created_at FROM calendar_users WHERE telegram_id=?"
        params = (telegram_id,)
        try:
            row = await self.execute(sql, parameters=params, fetchone=True)
            if not row:
                return None
            return {
                "telegram_id": row[0],
                "username": row[1],
                "full_name": row[2],
                "phone": row[3],
                "bio": row[4],
                "photo_url": row[5],
                "is_independent": bool(row[6]),
                "created_at": row[7],
            }
        except Exception:
            logger.exception("get_user failed: telegram_id=%s", telegram_id)
            return None

    async def get_all_calendar_users(self) -> List[dict]:
        sql = "SELECT telegram_id, username, full_name, phone, bio, photo_url, is_independent, created_at FROM calendar_users"
        try:
            rows = await self.execute(sql, fetchall=True)
            if not rows:
                return []
            return [
                {
                    "telegram_id": r[0],
                    "username": r[1],
                    "full_name": r[2],
                    "phone": r[3],
                    "bio": r[4],
                    "photo_url": r[5],
                    "is_independent": bool(r[6]),
                    "created_at": r[7],
                }
                for r in rows
            ]
        except Exception:
            logger.exception("get_all_users failed")
            return []

    @serialized_transaction
    async def create_calendar_user(
            self,
            telegram_id: int,
            username: Optional[str],
            full_name: str,
            phone: Optional[str] = None,
            bio: Optional[str] = None,
            photo_url: Optional[str] = None,
            is_independent: bool = False,
    ) -> Optional[dict]:
        sql = """
            INSERT INTO calendar_users (telegram_id, username, full_name, phone, bio, photo_url, is_independent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=COALESCE(excluded.username, calendar_users.username),
                full_name=COALESCE(excluded.full_name, calendar_users.full_name),
                phone=COALESCE(excluded.phone, calendar_users.phone),
                bio=COALESCE(excluded.bio, calendar_users.bio),
                photo_url=COALESCE(excluded.photo_url, calendar_users.photo_url),
                is_independent=excluded.is_independent
        """
        params = (telegram_id, username, full_name, phone, bio, photo_url, 1 if is_independent else 0)
        try:
            await self.execute(sql, parameters=params, commit=True)
            return await self.get_user(telegram_id)
        except Exception:
            logger.exception("create_user failed: telegram_id=%s", telegram_id)
            return None

    @serialized_transaction
    async def delete_user(self, telegram_id: int) -> bool:
        sql = "DELETE FROM calendar_users WHERE telegram_id=?"
        params = (telegram_id,)
        try:
            await self.execute(sql, parameters=params, commit=True)
            return True
        except Exception:
            logger.exception("delete_user failed: telegram_id=%s", telegram_id)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    #  CALENDARS
    # ──────────────────────────────────────────────────────────────────────────

    async def get_calendar(self, calendar_id: str) -> Optional[dict]:
        sql = "SELECT id, name, max_events_per_hour, created_at FROM calendars WHERE id=?"
        params = (calendar_id,)
        try:
            row = await self.execute(sql, parameters=params, fetchone=True)
            if not row:
                return None
            return {
                "id": row[0],
                "name": row[1],
                "max_events_per_hour": row[2],
                "created_at": row[3],
            }
        except Exception:
            logger.exception("get_calendar failed: calendar_id=%s", calendar_id)
            return None

    async def get_user_calendars(self, telegram_id: int) -> List[dict]:
        sql = """
            SELECT c.id, c.name, c.max_events_per_hour, c.created_at
            FROM calendars c
            JOIN calendar_members cm ON c.id = cm.calendar_id
            WHERE cm.telegram_id=?
        """
        params = (telegram_id,)
        try:
            rows = await self.execute(sql, parameters=params, fetchall=True)
            if not rows:
                return []
            return [
                {
                    "id": r[0],
                    "name": r[1],
                    "max_events_per_hour": r[2],
                    "created_at": r[3],
                }
                for r in rows
            ]
        except Exception:
            logger.exception("get_user_calendars failed: telegram_id=%s", telegram_id)
            return []

    @serialized_transaction
    async def create_calendar(
            self,
            id: str,
            name: str,
            max_events_per_hour: int = 1,
            owner_telegram_id: Optional[int] = None,
    ) -> Optional[dict]:
        existing = await self.get_calendar(id)
        if existing:
            return None

        try:
            await self.connection.execute(
                "INSERT INTO calendars (id, name, max_events_per_hour) VALUES (?, ?, ?)",
                (id, name, max_events_per_hour),
            )
            calendar_id = id

            if owner_telegram_id is not None:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_members (calendar_id, telegram_id) VALUES (?, ?)",
                    (calendar_id, owner_telegram_id),
                )

            await self.connection.commit()
            return await self.get_calendar(calendar_id)
        except Exception:
            await self.connection.rollback()
            logger.exception("create_calendar failed: id=%s, name=%s", id, name)
            return None

    @serialized_transaction
    async def delete_calendar(self, calendar_id: str) -> bool:
        try:
            await self.connection.execute("DELETE FROM calendar_members WHERE calendar_id=?", (calendar_id,))
            await self.connection.execute("DELETE FROM single_events WHERE calendar_id=?", (calendar_id,))
            await self.connection.execute("DELETE FROM recurring_events WHERE calendar_id=?", (calendar_id,))
            await self.connection.execute("DELETE FROM calendars WHERE id=?", (calendar_id,))
            await self.connection.commit()
            return True
        except Exception:
            await self.connection.rollback()
            logger.exception("delete_calendar failed: calendar_id=%s", calendar_id)
            return False

    @serialized_transaction
    async def create_booking(
        self,
        telegram_id: int,
        location: str,
        booking_date: str,
        time_slot: str,
        screenshot_path: str = "",
        price: float = 0.0,
        booking_id: str | None = None,
        calendar_id: int = 0
    ):
        sql = """
        INSERT OR IGNORE INTO bookings (telegram_id, location, booking_date, time_slot, screenshot_path, price, booking_id, calendar_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (telegram_id, location, booking_date, time_slot, screenshot_path, price, booking_id, calendar_id)
        await self.execute(sql, parameters=params, commit=True)

    @serialized_transaction
    async def confirm_admin_booking(
        self,
        booking_id: str,
        booking_data: list,
        date_str: str,
        location: str,
        tg_id: int,
        screenshot: str,
        title: str,
    ) -> None:
        """
        Атомарно подтверждает бронь администратором:
          1. Удаляет все записи из pending_bookings (kill_from_pending_bookings).
          2. Для каждого слота создаёт single_event в календаре.
          3. Для каждого слота создаёт запись в bookings.

        Выполняется в одной транзакции (BEGIN IMMEDIATE) — либо всё,
        либо ничего. Декоратор @serialized_transaction обеспечивает
        asyncio-уровневую сериализацию поверх SQLite-локка.
        """
        conn = self.connection
        try:
            await conn.execute("BEGIN IMMEDIATE")

            # 1. Удаляем из pending_bookings
            await conn.execute(
                "DELETE FROM pending_bookings WHERE booking_id=?",
                (booking_id,),
            )

            for rec in booking_data:
                slot_time = rec[3]
                dts = self._slot_to_datetimes(date_str, slot_time)

                event_row_id = None
                if dts:
                    start_dt, end_dt = dts

                    # 2. Гарантируем запись в calendars
                    await conn.execute(
                        "INSERT OR IGNORE INTO calendars (id, name, max_events_per_hour) VALUES (?, ?, ?)",
                        (location, f"Корт {location}", courts.get(location, 1)),
                    )

                    # 3. Создаём single_event
                    await conn.execute(
                        """INSERT INTO single_events
                               (calendar_id, created_by, title, start_datetime, end_datetime, status)
                           VALUES (?, NULL, ?, ?, ?, 'confirmed')""",
                        (location, title, start_dt, end_dt),
                    )
                    async with conn.execute("SELECT last_insert_rowid()") as cur:
                        row = await cur.fetchone()
                    event_row_id = row[0] if row else None

                # 4. Получаем цену
                async with conn.execute(
                    "SELECT price FROM price WHERE location=? AND time_slot=?",
                    (location, slot_time),
                ) as cur:
                    prow = await cur.fetchone()
                price = prow[0] if prow else 0.0

                # 5. Создаём запись в bookings
                await conn.execute(
                    """INSERT OR IGNORE INTO bookings
                           (telegram_id, location, booking_date, time_slot,
                            screenshot_path, price, booking_id, calendar_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (tg_id, location, date_str, slot_time,
                     screenshot, price, booking_id, event_row_id or 0),
                )

            await conn.commit()
        except Exception:
            await conn.rollback()
            logger.exception(
                "confirm_admin_booking failed: booking_id=%s, tg_id=%s",
                booking_id, tg_id,
            )
            raise


    @serialized_transaction
    async def reschedule_booking(
        self,
        telegram_id: int,
        booking_id: str,
        new_date: str,
        moves: dict[str, str],
    ) -> dict:
        """
        Атомарно переносит часть слотов брони (или все) на новую дату (Ташкентское время).

        moves — {старый_слот: новый_слот}. Слоты брони, которых нет в moves, остаются на месте.

        В одной транзакции:
          1. Проверяем, что все старые слоты есть в брони, а новый слот не совпадает
             с остающимся слотом этой же брони.
          2. Проверяем цену: цена нового слота должна совпадать с ценой старого.
          3. Удаляем события и строки bookings только переносимых слотов
             (до проверки вместимости, чтобы слот не конфликтовал сам с собой).
          4. Для каждого нового слота проверяем вместимость (_slot_occupancy) и создаём
             новые single_events и строки bookings.

        Возвращает {"status": "ok"} или:
          {"status": "not_found"}                                  — бронь не найдена
          {"status": "unknown_old_slot", "slot": ...}              — такого слота нет в брони
          {"status": "duplicate_slot", "slot": ...}                — новый слот уже есть в этой брони
          {"status": "price_mismatch", "slot": ..., "old_price": ..., "new_price": ...}
          {"status": "occupied", "slot": ...}                      — новый слот занят
        При любой другой ошибке: rollback + исключение.
        """
        try:
            await self.execute("BEGIN IMMEDIATE")

            rows = await self.execute(
                """SELECT location, booking_date, time_slot, screenshot_path, price, calendar_id
                   FROM bookings WHERE telegram_id=? AND booking_id=?""",
                (telegram_id, booking_id),
                fetchall=True,
            )
            
            if not rows:
                await self.connection.rollback()
                return {"status": "not_found"}

            if not rows[0][5]:
                await self.connection.rollback()
                return {"status": "old_booking"}

            rows_by_slot = {row[2]: row for row in rows}
            location = rows[0][0]
            screenshot_path = rows[0][3]

            # 1. Валидация слотов
            for old_slot in moves:
                if old_slot not in rows_by_slot:
                    await self.connection.rollback()
                    return {"status": "unknown_old_slot", "slot": old_slot}

            staying = {(row[1], row[2]) for slot, row in rows_by_slot.items() if slot not in moves}
            for new_slot in moves.values():
                if (new_date, new_slot) in staying:
                    await self.connection.rollback()
                    return {"status": "duplicate_slot", "slot": new_slot}

            # 2. Цена нового слота должна совпадать с ценой старого
            for old_slot, new_slot in moves.items():
                old_price = rows_by_slot[old_slot][4]
                price_row = await self.get_price(location, new_slot)  # возвращает кортеж (price,) или None
                new_price = price_row[0] if price_row else None
                if new_price is None or new_price != old_price:
                    await self.connection.rollback()
                    return {
                        "status": "price_mismatch",
                        "slot": new_slot,
                        "old_price": old_price,
                        "new_price": new_price,
                    }

            # 3. Освобождаем переносимые слоты: удаляем события календаря и строки bookings.
            # booking_id НЕ уникален (одна бронь на несколько часов = несколько строк),
            # поэтому строку определяем по booking_id + дате + слоту.
            title = f"Booking #{telegram_id}"
            for old_slot in moves:
                _, old_date, _, _, _, event_id = rows_by_slot[old_slot]
                if event_id:
                    old_title = await self._delete_booking_event(event_id)
                    if old_title:
                        title = old_title

                await self.execute(
                    "DELETE FROM bookings WHERE telegram_id=? AND booking_id=? AND booking_date=? AND time_slot=?",
                    (telegram_id, booking_id, old_date, old_slot),
                )

            # 4. Занимаем новые слоты и создаём новые строки bookings
            for old_slot, new_slot in moves.items():
                old_price = rows_by_slot[old_slot][4]

                occupied, capacity = await self._slot_occupancy(location, new_date, new_slot)
                if occupied >= capacity:
                    await self.connection.rollback()
                    return {"status": "occupied", "slot": new_slot}

                start_dt, end_dt = self._slot_to_datetimes(new_date, new_slot)
                await self.execute(
                    """INSERT INTO single_events
                           (calendar_id, created_by, title, start_datetime, end_datetime, status)
                       VALUES (?, NULL, ?, ?, ?, 'confirmed')""",
                    (location, title, start_dt, end_dt),
                )
                new_event_id = (await self.execute("SELECT last_insert_rowid()", fetchone=True))[0]

                await self.execute(
                    """INSERT INTO bookings
                           (telegram_id, location, booking_date, time_slot,
                            screenshot_path, price, booking_id, calendar_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (telegram_id, location, new_date, new_slot,
                     screenshot_path, old_price, booking_id, new_event_id),
                )

            await self.connection.commit()
            return {"status": "ok"}
        except Exception:
            await self.connection.rollback()
            logger.exception("reschedule_booking failed: booking_id=%s", booking_id)
            raise

    async def _delete_booking_event(self, event_id: int) -> Optional[str]:
        """
        Удаляет событие календаря по bookings.calendar_id и возвращает его title
        (None, если события уже нет).
        НЕ коммитит — вызывать только внутри открытой транзакции.
        """
        row = await self.execute("SELECT title FROM single_events WHERE id=?", (event_id,), fetchone=True)
        if not row:
            return None
        await self.execute("DELETE FROM single_events WHERE id=?", (event_id,))
        return row[0]

    @serialized_transaction
    async def update_calendar_capacity(
            self, calendar_id: str, max_events_per_hour: int
    ) -> Optional[dict]:
        sql = "UPDATE calendars SET max_events_per_hour=? WHERE id=?"
        params = (max_events_per_hour, calendar_id)
        try:
            await self.execute(sql, parameters=params, commit=True)
            return await self.get_calendar(calendar_id)
        except Exception:
            logger.exception("update_calendar_capacity failed: calendar_id=%s", calendar_id)
            return None

    # ──────────────────────────────────────────────────────────────────────────
    #  CALENDAR MEMBERS
    # ──────────────────────────────────────────────────────────────────────────

    async def check_member_access(self, calendar_id: str, telegram_id: int) -> bool:
        sql = "SELECT 1 FROM calendar_members WHERE calendar_id=? AND telegram_id=?"
        params = (calendar_id, telegram_id)
        try:
            row = await self.execute(sql, parameters=params, fetchone=True)
            return row is not None
        except Exception:
            logger.exception("check_member_access failed: calendar_id=%s, telegram_id=%s", calendar_id, telegram_id)
            return False

    async def get_calendar_members(self, calendar_id: str) -> List[dict]:
        sql = "SELECT calendar_id, telegram_id, role, can_edit_schedule, employment_type FROM calendar_members WHERE calendar_id=?"
        params = (calendar_id,)
        try:
            rows = await self.execute(sql, parameters=params, fetchall=True)
            if not rows:
                return []
            return [
                {
                    "calendar_id": r[0],
                    "telegram_id": r[1],
                    "role": r[2] or "trainer",
                    "can_edit_schedule": bool(r[3]) if r[3] is not None else True,
                    "employment_type": r[4] or "club",
                }
                for r in rows
            ]
        except Exception:
            logger.exception("get_calendar_members failed: calendar_id=%s", calendar_id)
            return []

    @serialized_transaction
    async def add_member(
        self,
        calendar_id: str,
        telegram_id: int,
        role: str = "trainer",
        can_edit_schedule: bool = True,
        employment_type: str = "club",
    ) -> Optional[dict]:
        try:
            can_edit_int = 1 if (role == "admin" or can_edit_schedule) else 0
            await self.connection.execute(
                """
                INSERT INTO calendar_members (calendar_id, telegram_id, role, can_edit_schedule, employment_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, telegram_id) DO UPDATE SET
                    role=excluded.role,
                    can_edit_schedule=excluded.can_edit_schedule,
                    employment_type=excluded.employment_type
                """,
                (calendar_id, telegram_id, role, can_edit_int, employment_type),
            )
            await self.connection.commit()
            return {
                "calendar_id": calendar_id,
                "telegram_id": telegram_id,
                "role": role,
                "can_edit_schedule": bool(can_edit_int),
                "employment_type": employment_type,
            }
        except Exception:
            logger.exception("add_member failed: calendar_id=%s, telegram_id=%s", calendar_id, telegram_id)
            return None

    async def get_member_role(self, calendar_id: str, telegram_id: int) -> Optional[str]:
        """Возвращает роль пользователя в календаре: 'admin', 'trainer' или None."""
        sql = "SELECT role FROM calendar_members WHERE calendar_id=? AND telegram_id=?"
        try:
            row = await self.execute(sql, (calendar_id, telegram_id), fetchone=True)
            return row[0] if row else None
        except Exception:
            logger.exception("get_member_role failed: calendar_id=%s, telegram_id=%s", calendar_id, telegram_id)
            return None

    async def get_member_info(self, calendar_id: str, telegram_id: int) -> Optional[dict]:
        """Возвращает полную информацию о правах участника в календаре."""
        sql = "SELECT role, can_edit_schedule, employment_type FROM calendar_members WHERE calendar_id=? AND telegram_id=?"
        try:
            row = await self.execute(sql, (calendar_id, telegram_id), fetchone=True)
            if not row:
                return None
            role = row[0] or "trainer"
            can_edit = True if role == "admin" else (bool(row[1]) if row[1] is not None else True)
            return {
                "role": role,
                "can_edit_schedule": can_edit,
                "employment_type": row[2] or "club",
            }
        except Exception:
            logger.exception("get_member_info failed: calendar_id=%s, telegram_id=%s", calendar_id, telegram_id)
            return None

    async def get_monthly_stats(self, calendar_id: str, from_date: str, to_date: str) -> dict:
        """
        Возвращает статистику заработков за период для владельца корта (admin):
          - доходы с обычных броней кортов (bookings)
          - доходы от взносов тренеров-арендаторов (crm_trainer_rent_payments)
          - доходы от штатных клубных учеников (crm_student_payments, где тренер employment_type='club')
        Личные доходы тренеров-арендаторов ('rent') сюда НЕ попадают!
        """
        try:
            rows = await self.execute(
                """
                SELECT booking_date, COUNT(*) as bookings_count, COALESCE(SUM(price), 0) as total_earned
                FROM bookings
                WHERE location = ? AND booking_date BETWEEN ? AND ?
                GROUP BY booking_date
                ORDER BY booking_date
                """,
                (calendar_id, from_date, to_date),
                fetchall=True,
            )
            daily = [
                {"date": r[0], "bookings_count": r[1], "total_earned": r[2]}
                for r in (rows or [])
            ]
            total_bookings = sum(d["bookings_count"] for d in daily)
            bookings_earned = sum(d["total_earned"] for d in daily)

            # Доходы от тренеров-арендаторов (сколько денег тренеры дали клубу)
            rent_row = await self.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM crm_trainer_rent_payments
                WHERE calendar_id = ? AND paid_at BETWEEN ? AND ?
                """,
                (calendar_id, from_date, to_date),
                fetchone=True,
            )
            trainer_rent_earned = rent_row[0] if rent_row else 0

            # Доходы от клубных учеников (тренеры, работающие на сам клуб: employment_type='club' или роль 'admin')
            club_students_row = await self.execute(
                """
                SELECT COALESCE(SUM(sp.amount), 0)
                FROM crm_student_payments sp
                LEFT JOIN calendar_members cm
                  ON cm.calendar_id = ? AND cm.telegram_id = sp.trainer_id
                WHERE (sp.calendar_id = ? OR cm.employment_type = 'club' OR cm.role = 'admin')
                  AND COALESCE(cm.employment_type, 'club') != 'rent'
                  AND sp.paid_at BETWEEN ? AND ?
                """,
                (calendar_id, calendar_id, from_date, to_date),
                fetchone=True,
            )
            club_students_earned = club_students_row[0] if club_students_row else 0

            total_earned = bookings_earned + trainer_rent_earned + club_students_earned
            return {
                "from_date": from_date,
                "to_date": to_date,
                "total_bookings": total_bookings,
                "bookings_earned": bookings_earned,
                "trainer_rent_earned": trainer_rent_earned,
                "club_students_earned": club_students_earned,
                "total_earned": total_earned,
                "daily": daily,
            }
        except Exception:
            logger.exception("get_monthly_stats failed: calendar_id=%s", calendar_id)
            return {
                "from_date": from_date,
                "to_date": to_date,
                "total_bookings": 0,
                "bookings_earned": 0,
                "trainer_rent_earned": 0,
                "club_students_earned": 0,
                "total_earned": 0,
                "daily": [],
            }

    @serialized_transaction
    async def remove_member(self, calendar_id: str, telegram_id: int) -> bool:
        sql = "DELETE FROM calendar_members WHERE calendar_id=? AND telegram_id=?"
        params = (calendar_id, telegram_id)
        try:
            await self.execute(sql, parameters=params, commit=True)
            return True
        except Exception:
            logger.exception("remove_member failed: calendar_id=%s, telegram_id=%s", calendar_id, telegram_id)
            return False

    async def _resolve_default_poll_schedule(self, start_hour: int) -> Tuple[str, int]:
        """Возвращает (send_time, days_before) из crm_poll_schedule_rules для указанного часа."""
        row = await self.execute(
            "SELECT send_time, days_before FROM crm_poll_schedule_rules WHERE event_hour=?",
            (start_hour,),
            fetchone=True,
        )
        if row:
            return row[0], row[1]
        if start_hour <= 10:
            return "20:00", 1
        return f"{max(8, start_hour - 4):02d}:00", 0

    # ──────────────────────────────────────────────────────────────────────────
    #  SINGLE EVENTS
    # ──────────────────────────────────────────────────────────────────────────

    async def get_single_event(self, event_id: int) -> Optional[dict]:
        sql = """
            SELECT id, calendar_id, created_by, title,
                   start_datetime, end_datetime, status, recurring_event_id, created_at,
                   event_type, student_id, group_id, trainer_id,
                   send_poll, poll_send_time, poll_days_before
            FROM single_events WHERE id=?
        """
        params = (event_id,)
        try:
            row = await self.execute(sql, parameters=params, fetchone=True)
            if not row:
                return None
            return {
                "id": row[0],
                "calendar_id": row[1],
                "created_by": row[2],
                "title": row[3],
                "start_datetime": row[4],
                "end_datetime": row[5],
                "status": row[6],
                "recurring_event_id": row[7],
                "created_at": row[8],
                "event_type": row[9],
                "student_id": row[10],
                "group_id": row[11],
                "trainer_id": row[12],
                "send_poll": bool(row[13]) if row[13] is not None else False,
                "poll_send_time": row[14],
                "poll_days_before": row[15] if row[15] is not None else 0,
            }
        except Exception:
            logger.exception("get_single_event failed: event_id=%s", event_id)
            return None

    @serialized_transaction
    async def update_single_event(
        self,
        event_id: int,
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        title: Optional[str] = None,
        event_type: Any = ...,
        student_id: Any = ...,
        group_id: Any = ...,
        trainer_id: Any = ...,
        send_poll: Optional[bool] = None,
        poll_send_time: Any = ...,
        poll_days_before: Optional[int] = None,
    ):
        updates = []
        params = []
        if start_datetime is not None:
            updates.append("start_datetime=?")
            params.append(start_datetime)
        if end_datetime is not None:
            updates.append("end_datetime=?")
            params.append(end_datetime)
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if event_type is not ...:
            updates.append("event_type=?")
            params.append(event_type)
        if student_id is not ...:
            updates.append("student_id=?")
            params.append(student_id)
        if group_id is not ...:
            updates.append("group_id=?")
            params.append(group_id)
        if trainer_id is not ...:
            updates.append("trainer_id=?")
            params.append(trainer_id)
        if send_poll is not None:
            updates.append("send_poll=?")
            params.append(1 if send_poll else 0)
        if poll_send_time is not ...:
            updates.append("poll_send_time=?")
            params.append(poll_send_time)
        if poll_days_before is not None:
            updates.append("poll_days_before=?")
            params.append(poll_days_before)

        if updates:
            sql = f"UPDATE single_events SET {', '.join(updates)} WHERE id=?"
            params.append(event_id)
            await self.execute(sql, parameters=tuple(params), commit=True)
        return await self.get_single_event(event_id)

    @serialized_transaction
    async def delete_single_event(self, event_id: int) -> bool:
        sql = "DELETE FROM single_events WHERE id=?"
        await self.execute(sql, parameters=(event_id,), commit=True)
        return True

    async def get_single_events_for_calendar(
            self,
            calendar_id: str,
            from_dt: str,
            to_dt: str,
            statuses: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        from_dt / to_dt — строки в формате 'YYYY-MM-DD HH:MM:SS'.
        statuses — опциональный список статусов (напр. ['confirmed', 'pending_payment']).
        """
        try:
            if statuses:
                placeholders = ", ".join("?" * len(statuses))
                sql = f"""
                    SELECT id, calendar_id, created_by, title,
                           start_datetime, end_datetime, status, recurring_event_id, created_at,
                           event_type, student_id, group_id, trainer_id,
                           send_poll, poll_send_time, poll_days_before
                    FROM single_events
                    WHERE calendar_id=? AND start_datetime<? AND end_datetime>?
                      AND status IN ({placeholders})
                """
                params = (calendar_id, to_dt, from_dt, *statuses)
            else:
                sql = """
                    SELECT id, calendar_id, created_by, title,
                           start_datetime, end_datetime, status, recurring_event_id, created_at,
                           event_type, student_id, group_id, trainer_id,
                           send_poll, poll_send_time, poll_days_before
                    FROM single_events
                    WHERE calendar_id=? AND start_datetime<? AND end_datetime>?
                """
                params = (calendar_id, to_dt, from_dt)

            lst = []
            pending_table_sql = "SELECT * FROM pending_table WHERE booking_date BETWEEN ? AND ? AND location = ? AND expires_at > ?"
            pendinds_params = (from_dt[:10], to_dt[:10], calendar_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            pending_table = await self.execute(pending_table_sql, parameters=pendinds_params, fetchall=True)

            pending_bookings_sql = "SELECT * FROM pending_bookings WHERE booking_date BETWEEN ? AND ? AND location = ?"
            pending_bookings_params = (from_dt[:10], to_dt[:10], calendar_id)
            pending_bookings = await self.execute(pending_bookings_sql, parameters=pending_bookings_params, fetchall=True)

            if pending_table:
                for pending in pending_table:
                    lst.append(
                        {
                            "id": pending[5],
                            "calendar_id": calendar_id,
                            "created_by": 0,
                            "title": "Пользователь думает...",
                            "start_datetime": f"{pending[1]} {pending[2][:5]}:00",
                            "end_datetime": f"{pending[1]} {pending[2][6:]}:00",
                            "status": "pending",
                            "recurring_event_id": 0,
                            "created_at": 0,
                            "event_type": None,
                            "student_id": None,
                            "group_id": None,
                            "trainer_id": None,
                            "send_poll": False,
                            "poll_send_time": None,
                            "poll_days_before": 0,
                        }
                    )

            if pending_bookings:
                for pending in pending_bookings:
                    lst.append(
                        {
                            "id": pending[0],
                            "calendar_id": calendar_id,
                            "created_by": 0,
                            "title": f"{pending[0]} ждет подтверждения от админа",
                            "start_datetime": f"{pending[2]} {pending[3][:5]}:00",
                            "end_datetime": f"{pending[2]} {pending[3][6:]}:00",
                            "status": "pending",
                            "recurring_event_id": 0,
                            "created_at": 0,
                            "event_type": None,
                            "student_id": None,
                            "group_id": None,
                            "trainer_id": None,
                            "send_poll": False,
                            "poll_send_time": None,
                            "poll_days_before": 0,
                        }
                    )

            rows = await self.execute(sql, parameters=params, fetchall=True)
            return_rows = []
            if rows:
                return_rows = [
                    {
                        "id": r[0],
                        "calendar_id": r[1],
                        "created_by": r[2],
                        "title": r[3],
                        "start_datetime": r[4],
                        "end_datetime": r[5],
                        "status": r[6],
                        "recurring_event_id": r[7],
                        "created_at": r[8],
                        "event_type": r[9],
                        "student_id": r[10],
                        "group_id": r[11],
                        "trainer_id": r[12],
                        "send_poll": bool(r[13]) if r[13] is not None else False,
                        "poll_send_time": r[14],
                        "poll_days_before": r[15] if r[15] is not None else 0,
                    }
                    for r in rows
                ]

            final = return_rows + lst
            if not final:
                return []
            return sorted(final, key=lambda x: x["start_datetime"])
        except Exception:
            logger.exception("get_single_events_for_calendar failed: calendar_id=%s", calendar_id)
            return []

    @serialized_transaction
    async def create_single_event(
            self,
            calendar_id: str,
            created_by: Optional[int],
            title: str,
            start_datetime: str,
            end_datetime: str,
            status: str,
            recurring_event_id: Optional[int] = None,
            event_type: Optional[str] = None,
            student_id: Optional[int] = None,
            group_id: Optional[int] = None,
            trainer_id: Optional[int] = None,
            send_poll: bool = False,
            poll_send_time: Optional[str] = None,
            poll_days_before: Optional[int] = None,
    ) -> Optional[dict]:
        """start_datetime / end_datetime — строки 'YYYY-MM-DD HH:MM:SS'."""
        try:
            # Гарантируем существование записи в calendars для этого calendar_id
            await self.connection.execute(
                "INSERT OR IGNORE INTO calendars (id, name, max_events_per_hour) VALUES (?, ?, ?)",
                (calendar_id, f"Корт {calendar_id}", courts.get(calendar_id, 1)),
            )

            # Гарантируем существование записи в calendar_users для created_by (если указан)
            if created_by:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                    (created_by, "", "User"),
                )
            else:
                created_by = None

            if trainer_id:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                    (trainer_id, "", "Trainer"),
                )
            elif event_type in ("individual", "group") and created_by:
                trainer_id = created_by

            if recurring_event_id:
                async with self.connection.execute("SELECT id FROM recurring_events WHERE id=?", (recurring_event_id,)) as cursor:
                    rec_row = await cursor.fetchone()
                if not rec_row:
                    recurring_event_id = None

            if send_poll and not poll_send_time:
                try:
                    start_h = int(start_datetime[11:13])
                except Exception:
                    start_h = 12
                default_time, default_days = await self._resolve_default_poll_schedule(start_h)
                poll_send_time = default_time
                if poll_days_before is None:
                    poll_days_before = default_days
            elif poll_days_before is None:
                poll_days_before = 0

            sql = """
                INSERT INTO single_events
                    (calendar_id, created_by, title, start_datetime, end_datetime, status, recurring_event_id,
                     event_type, student_id, group_id, trainer_id, send_poll, poll_send_time, poll_days_before)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                calendar_id, created_by, title, start_datetime, end_datetime, status, recurring_event_id,
                event_type, student_id, group_id, trainer_id, 1 if send_poll else 0, poll_send_time, poll_days_before,
            )
            await self.connection.execute(sql, params)
            await self.connection.commit()

            row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
            return await self.get_single_event(row[0])
        except Exception:
            await self.connection.rollback()
            logger.exception("create_single_event failed: calendar_id=%s", calendar_id)
            return None

    async def get_recurring_events_for_calendar(
            self,
            calendar_id: str,
            days_of_week: Optional[List[int]] = None,
    ) -> List[dict]:
        try:
            if days_of_week is not None:
                placeholders = ", ".join("?" * len(days_of_week))
                sql = f"""
                    SELECT id, calendar_id, created_by, title,
                           day_of_week, start_time, end_time, created_at,
                           event_type, student_id, group_id, trainer_id,
                           send_poll, poll_send_time, poll_days_before
                    FROM recurring_events
                    WHERE calendar_id=? AND day_of_week IN ({placeholders})
                """
                params = (calendar_id, *days_of_week)
            else:
                sql = """
                    SELECT id, calendar_id, created_by, title,
                           day_of_week, start_time, end_time, created_at,
                           event_type, student_id, group_id, trainer_id,
                           send_poll, poll_send_time, poll_days_before
                    FROM recurring_events WHERE calendar_id=?
                """
                params = (calendar_id,)

            rows = await self.execute(sql, parameters=params, fetchall=True)
            if not rows:
                return []
            return [
                {
                    "id": r[0],
                    "calendar_id": r[1],
                    "created_by": r[2],
                    "title": r[3],
                    "day_of_week": r[4],
                    "start_time": r[5],
                    "end_time": r[6],
                    "created_at": r[7],
                    "event_type": r[8],
                    "student_id": r[9],
                    "group_id": r[10],
                    "trainer_id": r[11],
                    "send_poll": bool(r[12]) if r[12] is not None else False,
                    "poll_send_time": r[13],
                    "poll_days_before": r[14] if r[14] is not None else 0,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("get_recurring_events_for_calendar failed: calendar_id=%s", calendar_id)
            return []

    async def get_recurring_event(self, event_id: int) -> Optional[dict]:
        sql = """
            SELECT id, calendar_id, created_by, title,
                   day_of_week, start_time, end_time, created_at,
                   event_type, student_id, group_id, trainer_id,
                   send_poll, poll_send_time, poll_days_before
            FROM recurring_events WHERE id=?
        """
        try:
            row = await self.execute(sql, (event_id,), fetchone=True)
            if not row:
                return None
            return {
                "id": row[0],
                "calendar_id": row[1],
                "created_by": row[2],
                "title": row[3],
                "day_of_week": row[4],
                "start_time": row[5],
                "end_time": row[6],
                "created_at": row[7],
                "event_type": row[8],
                "student_id": row[9],
                "group_id": row[10],
                "trainer_id": row[11],
                "send_poll": bool(row[12]) if row[12] is not None else False,
                "poll_send_time": row[13],
                "poll_days_before": row[14] if row[14] is not None else 0,
            }
        except Exception:
            logger.exception("get_recurring_event failed: event_id=%s", event_id)
            return None

    @serialized_transaction
    async def create_recurring_event(
            self,
            calendar_id: str,
            created_by: Optional[int],
            title: str,
            day_of_week: int,
            start_time: str,
            end_time: str,
            event_type: Optional[str] = None,
            student_id: Optional[int] = None,
            group_id: Optional[int] = None,
            trainer_id: Optional[int] = None,
            send_poll: bool = False,
            poll_send_time: Optional[str] = None,
            poll_days_before: Optional[int] = None,
    ) -> Optional[dict]:
        """start_time / end_time — строки 'HH:MM:SS'."""
        try:
            await self.connection.execute(
                "INSERT OR IGNORE INTO calendars (id, name, max_events_per_hour) VALUES (?, ?, ?)",
                (calendar_id, f"Корт {calendar_id}", courts.get(calendar_id, 1)),
            )
            if created_by:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                    (created_by, "", "User"),
                )
            else:
                created_by = None

            if trainer_id:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                    (trainer_id, "", "Trainer"),
                )
            elif event_type in ("individual", "group") and created_by:
                trainer_id = created_by

            if send_poll and not poll_send_time:
                try:
                    start_h = int(start_time[:2])
                except Exception:
                    start_h = 12
                default_time, default_days = await self._resolve_default_poll_schedule(start_h)
                poll_send_time = default_time
                if poll_days_before is None:
                    poll_days_before = default_days
            elif poll_days_before is None:
                poll_days_before = 0

            sql = """
                INSERT INTO recurring_events
                    (calendar_id, created_by, title, day_of_week, start_time, end_time,
                     event_type, student_id, group_id, trainer_id, send_poll, poll_send_time, poll_days_before)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                calendar_id, created_by, title, day_of_week, start_time, end_time,
                event_type, student_id, group_id, trainer_id, 1 if send_poll else 0, poll_send_time, poll_days_before,
            )
            await self.connection.execute(sql, params)
            await self.connection.commit()

            row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
            return await self.get_recurring_event(row[0])
        except Exception:
            await self.connection.rollback()
            logger.exception("create_recurring_event failed: calendar_id=%s", calendar_id)
            return None

    @serialized_transaction
    async def create_recurring_events_bulk(
            self,
            calendar_id: str,
            created_by: int,
            title: str,
            days_of_week: List[int],
            start_time: str,
            end_time: str,
            event_type: Optional[str] = None,
            student_id: Optional[int] = None,
            group_id: Optional[int] = None,
            trainer_id: Optional[int] = None,
            send_poll: bool = False,
            poll_send_time: Optional[str] = None,
            poll_days_before: Optional[int] = None,
    ) -> List[dict]:
        """Создать события для нескольких дней недели в одном коммите."""
        sql = """
            INSERT INTO recurring_events
                (calendar_id, created_by, title, day_of_week, start_time, end_time,
                 event_type, student_id, group_id, trainer_id, send_poll, poll_send_time, poll_days_before)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            if trainer_id:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                    (trainer_id, "", "Trainer"),
                )
            elif event_type in ("individual", "group") and created_by:
                trainer_id = created_by

            if send_poll and not poll_send_time:
                try:
                    start_h = int(start_time[:2])
                except Exception:
                    start_h = 12
                default_time, default_days = await self._resolve_default_poll_schedule(start_h)
                poll_send_time = default_time
                if poll_days_before is None:
                    poll_days_before = default_days
            elif poll_days_before is None:
                poll_days_before = 0

            ids = []
            for day in days_of_week:
                await self.connection.execute(
                    sql,
                    (
                        calendar_id, created_by, title, day, start_time, end_time,
                        event_type, student_id, group_id, trainer_id,
                        1 if send_poll else 0, poll_send_time, poll_days_before,
                    ),
                )
                cursor = await self.connection.execute("SELECT last_insert_rowid()")
                row = await cursor.fetchone()
                ids.append(row[0])
            await self.connection.commit()

            result = []
            for event_id in ids:
                event = await self.get_recurring_event(event_id)
                if event:
                    result.append(event)
            return result
        except Exception:
            await self.connection.rollback()
            logger.exception("create_recurring_events_bulk failed: calendar_id=%s", calendar_id)
            return []

    @serialized_transaction
    async def update_recurring_event_title(
        self,
        event_id: int,
        title: Optional[str] = None,
        event_type: Any = ...,
        student_id: Any = ...,
        group_id: Any = ...,
        trainer_id: Any = ...,
        send_poll: Optional[bool] = None,
        poll_send_time: Any = ...,
        poll_days_before: Optional[int] = None,
    ) -> Optional[dict]:
        """Обновляет название и/или CRM-поля повторяющегося события."""
        updates = []
        params = []
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if event_type is not ...:
            updates.append("event_type=?")
            params.append(event_type)
        if student_id is not ...:
            updates.append("student_id=?")
            params.append(student_id)
        if group_id is not ...:
            updates.append("group_id=?")
            params.append(group_id)
        if trainer_id is not ...:
            updates.append("trainer_id=?")
            params.append(trainer_id)
        if send_poll is not None:
            updates.append("send_poll=?")
            params.append(1 if send_poll else 0)
        if poll_send_time is not ...:
            updates.append("poll_send_time=?")
            params.append(poll_send_time)
        if poll_days_before is not None:
            updates.append("poll_days_before=?")
            params.append(poll_days_before)

        try:
            if updates:
                sql = f"UPDATE recurring_events SET {', '.join(updates)} WHERE id=?"
                params.append(event_id)
                await self.execute(sql, parameters=tuple(params), commit=True)
            return await self.get_recurring_event(event_id)
        except Exception:
            logger.exception("update_recurring_event_title failed: event_id=%s", event_id)
            return None

    @serialized_transaction
    async def delete_recurring_event(self, event_id: int) -> bool:
        sql = "DELETE FROM recurring_events WHERE id=?"
        params = (event_id,)
        try:
            await self.execute(sql, parameters=params, commit=True)
            return True
        except Exception:
            logger.exception("delete_recurring_event failed: event_id=%s", event_id)
            return False

    @serialized_transaction
    async def cancel_recurring_instance(
            self,
            recurring_event_id: int,
            cancel_date: str,
            created_by: int,
    ) -> Optional[dict]:
        try:
            rec = await self.get_recurring_event(recurring_event_id)
            if not rec:
                return None

            dt = datetime.strptime(cancel_date, "%Y-%m-%d")
            if dt.weekday() != rec["day_of_week"]:
                return None

            start_dt = f"{cancel_date} {rec['start_time']}"
            end_dt = f"{cancel_date} {rec['end_time']}"

            sql = """
                INSERT INTO single_events
                    (calendar_id, created_by, title, start_datetime, end_datetime, status, recurring_event_id)
                VALUES (?, ?, ?, ?, ?, 'cancelled', ?)
            """
            params = (rec["calendar_id"], created_by, rec["title"], start_dt, end_dt, recurring_event_id)
            await self.execute(sql, parameters=params, commit=True)

            row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
            return await self.get_single_event(row[0])
        except Exception:
            logger.exception("cancel_recurring_instance failed: recurring_event_id=%s", recurring_event_id)
            return None


    # ──────────────────────────────────────────────────────────────────────────
    #  STATS
    # ──────────────────────────────────────────────────────────────────────────

    async def get_calendar_stats(self, calendar_id: str) -> dict:
        """Статистика: активные, отменённые, кол-во повторяющихся серий."""
        try:
            active_row = await self.execute(
                "SELECT COUNT(*) FROM single_events WHERE calendar_id=? AND status IN ('confirmed', 'pending_payment')",
                parameters=(calendar_id,),
                fetchone=True,
            )
            cancelled_row = await self.execute(
                "SELECT COUNT(*) FROM single_events WHERE calendar_id=? AND status='cancelled'",
                parameters=(calendar_id,),
                fetchone=True,
            )
            recurring_row = await self.execute(
                "SELECT COUNT(*) FROM recurring_events WHERE calendar_id=?",
                parameters=(calendar_id,),
                fetchone=True,
            )
            return {
                "calendar_id": calendar_id,
                "active_single_events": active_row[0] if active_row else 0,
                "cancelled_events": cancelled_row[0] if cancelled_row else 0,
                "recurring_series": recurring_row[0] if recurring_row else 0,
            }
        except Exception:
            logger.exception("get_calendar_stats failed: calendar_id=%s", calendar_id)
            return {
                "calendar_id": calendar_id,
                "active_single_events": 0,
                "cancelled_events": 0,
                "recurring_series": 0,
            }

    async def get_events_on_date(self, calendar_id: str, target_date: str) -> dict:
        """
        Все активные события на конкретную дату.
        target_date — строка 'YYYY-MM-DD'.

        Возвращает: {"date": ..., "single_events": [...], "recurring_instances": [...]}
        """
        try:
            from_dt = f"{target_date} 00:00:00"
            to_dt = f"{target_date} 23:59:59"

            # Одиночные (confirmed / pending_payment)
            single_rows = await self.execute(
                """
                SELECT id, calendar_id, created_by, title,
                       start_datetime, end_datetime, status, recurring_event_id, created_at,
                       event_type, student_id, group_id, trainer_id,
                       send_poll, poll_send_time, poll_days_before
                FROM single_events
                WHERE calendar_id=? AND start_datetime>=? AND end_datetime<=?
                  AND status IN ('confirmed', 'pending_payment')
                """,
                parameters=(calendar_id, from_dt, to_dt),
                fetchall=True,
            )

            # Отменённые экземпляры повторяющихся (чтобы исключить из виртуальных)
            cancelled_rows = await self.execute(
                """
                SELECT recurring_event_id FROM single_events
                WHERE calendar_id=? AND start_datetime>=? AND end_datetime<=?
                  AND status='cancelled' AND recurring_event_id IS NOT NULL
                """,
                parameters=(calendar_id, from_dt, to_dt),
                fetchall=True,
            )
            cancelled_ids = {r[0] for r in cancelled_rows} if cancelled_rows else set()

            # Повторяющиеся на нужный день недели (0=Пн, 6=Вс)
            weekday = datetime.strptime(target_date, "%Y-%m-%d").weekday()
            recurring_rows = await self.execute(
                """
                SELECT id, title, start_time, end_time,
                       event_type, student_id, group_id, trainer_id,
                       send_poll, poll_send_time, poll_days_before
                FROM recurring_events
                WHERE calendar_id=? AND day_of_week=?
                """,
                parameters=(calendar_id, weekday),
                fetchall=True,
            )

            recurring_instances = [
                {
                    "recurring_event_id": r[0],
                    "title": r[1],
                    "start_datetime": f"{target_date} {r[2]}",
                    "end_datetime": f"{target_date} {r[3]}",
                    "status": "confirmed",
                    "type": "recurring_instance",
                    "event_type": r[4],
                    "student_id": r[5],
                    "group_id": r[6],
                    "trainer_id": r[7],
                    "send_poll": bool(r[8]) if r[8] is not None else False,
                    "poll_send_time": r[9],
                    "poll_days_before": r[10] if r[10] is not None else 0,
                }
                for r in (recurring_rows or [])
                if r[0] not in cancelled_ids
            ]

            return {
                "date": target_date,
                "single_events": [
                    {
                        "id": r[0],
                        "calendar_id": r[1],
                        "created_by": r[2],
                        "title": r[3],
                        "start_datetime": r[4],
                        "end_datetime": r[5],
                        "status": r[6],
                        "recurring_event_id": r[7],
                        "created_at": r[8],
                        "event_type": r[9],
                        "student_id": r[10],
                        "group_id": r[11],
                        "trainer_id": r[12],
                        "send_poll": bool(r[13]) if r[13] is not None else False,
                        "poll_send_time": r[14],
                        "poll_days_before": r[15] if r[15] is not None else 0,
                    }
                    for r in (single_rows or [])
                ],
                "recurring_instances": recurring_instances,
            }
        except Exception:
            logger.exception("get_events_on_date failed: calendar_id=%s, target_date=%s", calendar_id, target_date)
            return {"date": target_date, "single_events": [], "recurring_instances": []}

    # ──────────────────────────────────────────────────────────────────────────
    #  CRM: STUDENTS, GROUPS, PAYMENTS, ATTENDANCE, STUDENT PANEL
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_student(row: tuple) -> dict:
        token = row[4]
        skills_raw = row[13]
        try:
            skills = json.loads(skills_raw) if skills_raw else None
        except Exception:
            skills = None
        return {
            "id": row[0],
            "full_name": row[1],
            "phone": row[2],
            "telegram_id": row[3],
            "is_telegram_linked": row[3] is not None,
            "invite_token": token,
            "invite_link": f"{BOT_LINK}?start=stu_{token}" if token else None,
            "status": row[5],
            "next_payment_date": row[6],
            "payment_amount": row[7],
            "trainer_id": row[8],
            "calendar_id": row[9],
            "notes": row[10],
            "coach_comment": row[11],
            "skill_level": row[12],
            "skills": skills,
            "polls_enabled": bool(row[14]) if row[14] is not None else True,
            "created_by": row[15],
            "created_at": row[16],
        }

    async def get_crm_student(self, student_id: int) -> Optional[dict]:
        sql = """
            SELECT id, full_name, phone, telegram_id, invite_token, status,
                   next_payment_date, payment_amount, trainer_id, calendar_id,
                   notes, coach_comment, skill_level, skills_json, polls_enabled,
                   created_by, created_at
            FROM crm_students WHERE id=?
        """
        row = await self.execute(sql, (student_id,), fetchone=True)
        return self._row_to_student(row) if row else None

    async def get_crm_student_by_telegram_id(self, telegram_id: int) -> Optional[dict]:
        sql = """
            SELECT id, full_name, phone, telegram_id, invite_token, status,
                   next_payment_date, payment_amount, trainer_id, calendar_id,
                   notes, coach_comment, skill_level, skills_json, polls_enabled,
                   created_by, created_at
            FROM crm_students WHERE telegram_id=?
        """
        row = await self.execute(sql, (telegram_id,), fetchone=True)
        return self._row_to_student(row) if row else None

    async def get_crm_student_by_token(self, invite_token: str) -> Optional[dict]:
        sql = """
            SELECT id, full_name, phone, telegram_id, invite_token, status,
                   next_payment_date, payment_amount, trainer_id, calendar_id,
                   notes, coach_comment, skill_level, skills_json, polls_enabled,
                   created_by, created_at
            FROM crm_students WHERE invite_token=?
        """
        row = await self.execute(sql, (invite_token,), fetchone=True)
        return self._row_to_student(row) if row else None

    @serialized_transaction
    async def create_crm_student(
        self,
        full_name: str,
        phone: Optional[str] = None,
        status: Optional[str] = None,
        next_payment_date: Optional[str] = None,
        payment_amount: Optional[float] = None,
        trainer_id: Optional[int] = None,
        calendar_id: Optional[str] = None,
        notes: Optional[str] = None,
        coach_comment: Optional[str] = None,
        skill_level: Optional[str] = None,
        skills: Optional[dict] = None,
        polls_enabled: bool = True,
        created_by: Optional[int] = None,
    ) -> Optional[dict]:
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
        skills_json = json.dumps(skills, ensure_ascii=False) if skills is not None else None
        effective_trainer = trainer_id if trainer_id is not None else created_by
        sql = """
            INSERT INTO crm_students (
                full_name, phone, invite_token, status, next_payment_date,
                payment_amount, trainer_id, calendar_id, notes, coach_comment,
                skill_level, skills_json, polls_enabled, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            full_name, phone, token, status, next_payment_date,
            payment_amount, effective_trainer, calendar_id, notes, coach_comment,
            skill_level, skills_json, 1 if polls_enabled else 0, created_by,
        )
        await self.execute(sql, params, commit=True)
        row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
        return await self.get_crm_student(row[0])

    @serialized_transaction
    async def update_crm_student(
        self,
        student_id: int,
        full_name: Optional[str] = None,
        phone: Any = ...,
        status: Any = ...,
        next_payment_date: Any = ...,
        payment_amount: Any = ...,
        trainer_id: Any = ...,
        calendar_id: Any = ...,
        notes: Any = ...,
        coach_comment: Any = ...,
        skill_level: Any = ...,
        skills: Any = ...,
        polls_enabled: Optional[bool] = None,
    ) -> Optional[dict]:
        updates = []
        params = []
        if full_name is not None:
            updates.append("full_name=?")
            params.append(full_name)
        if phone is not ...:
            updates.append("phone=?")
            params.append(phone)
        if status is not ...:
            updates.append("status=?")
            params.append(status)
        if next_payment_date is not ...:
            updates.append("next_payment_date=?")
            params.append(next_payment_date)
            # Сбрасываем метки отправленных уведомлений при смене даты оплаты
            updates.append("last_notif_7d_date=NULL")
            updates.append("last_notif_3d_date=NULL")
        if payment_amount is not ...:
            updates.append("payment_amount=?")
            params.append(payment_amount)
        if trainer_id is not ...:
            updates.append("trainer_id=?")
            params.append(trainer_id)
        if calendar_id is not ...:
            updates.append("calendar_id=?")
            params.append(calendar_id)
        if notes is not ...:
            updates.append("notes=?")
            params.append(notes)
        if coach_comment is not ...:
            updates.append("coach_comment=?")
            params.append(coach_comment)
        if skill_level is not ...:
            updates.append("skill_level=?")
            params.append(skill_level)
        if skills is not ...:
            updates.append("skills_json=?")
            params.append(json.dumps(skills, ensure_ascii=False) if skills is not None else None)
        if polls_enabled is not None:
            updates.append("polls_enabled=?")
            params.append(1 if polls_enabled else 0)

        if updates:
            sql = f"UPDATE crm_students SET {', '.join(updates)} WHERE id=?"
            params.append(student_id)
            await self.execute(sql, tuple(params), commit=True)
        return await self.get_crm_student(student_id)

    @serialized_transaction
    async def delete_crm_student(self, student_id: int) -> bool:
        await self.execute("DELETE FROM crm_students WHERE id=?", (student_id,), commit=True)
        return True

    @serialized_transaction
    async def link_crm_student_by_token(
        self, invite_token: str, telegram_id: int, phone: Optional[str] = None
    ) -> dict:
        """
        Привязывает Telegram ID пользователя к карточке ученика по диплинк-токену.
        Возвращает {"status": "linked" | "already_linked_to_you" | "already_used" | "not_found", "student": ...}.
        """
        student = await self.get_crm_student_by_token(invite_token)
        if not student:
            return {"status": "not_found", "student": None}

        if student["telegram_id"] is not None and student["telegram_id"] != telegram_id:
            return {"status": "already_used", "student": None}

        if student["telegram_id"] == telegram_id:
            return {"status": "already_linked_to_you", "student": student}

        # Если у пользователя нет телефона в карточке ученика, подтягиваем из таблицы users
        if not phone:
            phone = await self.get_number_by_id(telegram_id)

        new_phone = student["phone"] or phone
        await self.execute(
            "UPDATE crm_students SET telegram_id=?, phone=?, status=COALESCE(status, 'active') WHERE id=?",
            (telegram_id, new_phone, student["id"]),
            commit=True,
        )
        updated = await self.get_crm_student(student["id"])
        return {"status": "linked", "student": updated}

    async def _is_rent_or_independent_trainer(self, trainer_id: Optional[int], calendar_id: Optional[str] = None) -> bool:
        """Проверяет, является ли тренер независимым арендатором (чьи доходы и ученики скрыты от админа клуба)."""
        if not trainer_id:
            return False
        u_row = await self.execute(
            "SELECT is_independent FROM calendar_users WHERE telegram_id=?", (trainer_id,), fetchone=True
        )
        if u_row and u_row[0]:
            return True
        if calendar_id:
            m_row = await self.execute(
                "SELECT employment_type, role FROM calendar_members WHERE calendar_id=? AND telegram_id=?",
                (calendar_id, trainer_id),
                fetchone=True,
            )
            if m_row and m_row[0] == "rent" and m_row[1] != "admin":
                return True
        else:
            # Если календарь не передан, проверяем, есть ли у тренера статус 'rent' и нет ли 'club'/'admin'
            rows = await self.execute(
                "SELECT employment_type, role FROM calendar_members WHERE telegram_id=?",
                (trainer_id,),
                fetchall=True,
            )
            if rows and all(r[0] == "rent" and r[1] != "admin" for r in rows):
                return True
        return False

    async def can_access_student_finances(
        self, requester_telegram_id: int, student: dict, calendar_id: Optional[str] = None
    ) -> bool:
        """
        Финансовая и клиентская изоляция:
        1. Сам тренер ученика (student.trainer_id == requester_telegram_id) ВСЕГДА имеет доступ.
        2. Если тренер ученика — арендатор ('rent') или свободный тренер ('is_independent=1'),
           НИКТО кроме самого тренера не видит его ученика и его оплаты (даже админ корта!).
        3. Если ученик клубный (или у тренера employment_type == 'club'), админ этого клуба имеет доступ.
        """
        st_trainer = student.get("trainer_id")
        if st_trainer == requester_telegram_id or student.get("created_by") == requester_telegram_id:
            return True

        eff_cal = calendar_id or student.get("calendar_id")
        if await self._is_rent_or_independent_trainer(st_trainer, eff_cal):
            return False

        # Проверяем, является ли запрашивающий админом в календаре ученика или общем календаре с тренером
        if eff_cal:
            role = await self.get_member_role(eff_cal, requester_telegram_id)
            return role == "admin"

        admin_rows = await self.execute(
            "SELECT calendar_id FROM calendar_members WHERE telegram_id=? AND role='admin'",
            (requester_telegram_id,),
            fetchall=True,
        )
        if not admin_rows:
            return False
        if st_trainer is None:
            return True

        admin_cals = [r[0] for r in admin_rows]
        placeholders = ", ".join("?" * len(admin_cals))
        shared = await self.execute(
            f"""
            SELECT 1 FROM calendar_members
            WHERE telegram_id=? AND calendar_id IN ({placeholders})
              AND COALESCE(employment_type, 'club') = 'club'
            """,
            (st_trainer, *admin_cals),
            fetchone=True,
        )
        return shared is not None

    async def search_crm_students(
        self,
        query: str,
        requester_telegram_id: int,
        calendar_id: Optional[str] = None,
        limit: int = 30,
    ) -> List[dict]:
        """
        Умный поиск учеников по имени (без учета регистра для кириллицы/латиницы) или номеру телефона.
        Соблюдает приватность между тренерами-арендаторами и клубом.
        """
        q_clean = (query or "").strip().lower()
        q_digits = "".join(ch for ch in q_clean if ch.isdigit())

        sql = """
            SELECT id, full_name, phone, telegram_id, invite_token, status,
                   next_payment_date, payment_amount, trainer_id, calendar_id,
                   notes, coach_comment, skill_level, skills_json, polls_enabled,
                   created_by, created_at
            FROM crm_students
            ORDER BY full_name ASC
        """
        rows = await self.execute(sql, fetchall=True)
        result = []
        for r in (rows or []):
            st = self._row_to_student(r)
            if q_clean:
                name_lower = (st["full_name"] or "").lower()
                phone_raw = st["phone"] or ""
                phone_digits = "".join(ch for ch in phone_raw if ch.isdigit())
                name_match = q_clean in name_lower
                phone_match = (q_clean in phone_raw.lower()) or (bool(q_digits) and q_digits in phone_digits)
                if not (name_match or phone_match):
                    continue
            if await self.can_access_student_finances(requester_telegram_id, st, calendar_id):
                result.append(st)
                if len(result) >= limit:
                    break
        return result

    # ──────────────────────────────────────────────────────────────────────────
    #  CRM GROUPS
    # ──────────────────────────────────────────────────────────────────────────

    async def get_crm_group(self, group_id: int) -> Optional[dict]:
        row = await self.execute(
            "SELECT id, name, trainer_id, calendar_id, notes, created_at FROM crm_groups WHERE id=?",
            (group_id,),
            fetchone=True,
        )
        if not row:
            return None
        member_rows = await self.execute(
            """
            SELECT s.id, s.full_name, s.phone, s.telegram_id, s.invite_token, s.status,
                   s.next_payment_date, s.payment_amount, s.trainer_id, s.calendar_id,
                   s.notes, s.coach_comment, s.skill_level, s.skills_json, s.polls_enabled,
                   s.created_by, s.created_at
            FROM crm_group_members gm
            JOIN crm_students s ON s.id = gm.student_id
            WHERE gm.group_id = ?
            ORDER BY s.full_name ASC
            """,
            (group_id,),
            fetchall=True,
        )
        students = [self._row_to_student(r) for r in (member_rows or [])]
        return {
            "id": row[0],
            "name": row[1],
            "trainer_id": row[2],
            "calendar_id": row[3],
            "notes": row[4],
            "created_at": row[5],
            "students": students,
            "student_ids": [s["id"] for s in students],
        }

    @serialized_transaction
    async def create_crm_group(
        self,
        name: str,
        trainer_id: Optional[int] = None,
        calendar_id: Optional[str] = None,
        notes: Optional[str] = None,
        student_ids: Optional[List[int]] = None,
    ) -> Optional[dict]:
        if trainer_id:
            await self.connection.execute(
                "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
                (trainer_id, "", "Trainer"),
            )
        await self.connection.execute(
            "INSERT INTO crm_groups (name, trainer_id, calendar_id, notes) VALUES (?, ?, ?, ?)",
            (name, trainer_id, calendar_id, notes),
        )
        row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
        group_id = row[0]

        for sid in (student_ids or []):
            await self.connection.execute(
                "INSERT OR IGNORE INTO crm_group_members (group_id, student_id) VALUES (?, ?)",
                (group_id, sid),
            )
        await self.connection.commit()
        return await self.get_crm_group(group_id)

    @serialized_transaction
    async def update_crm_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        trainer_id: Any = ...,
        calendar_id: Any = ...,
        notes: Any = ...,
        student_ids: Optional[List[int]] = None,
    ) -> Optional[dict]:
        updates = []
        params = []
        if name is not None:
            updates.append("name=?")
            params.append(name)
        if trainer_id is not ...:
            updates.append("trainer_id=?")
            params.append(trainer_id)
        if calendar_id is not ...:
            updates.append("calendar_id=?")
            params.append(calendar_id)
        if notes is not ...:
            updates.append("notes=?")
            params.append(notes)

        if updates:
            sql = f"UPDATE crm_groups SET {', '.join(updates)} WHERE id=?"
            params.append(group_id)
            await self.connection.execute(sql, tuple(params))

        if student_ids is not None:
            await self.connection.execute("DELETE FROM crm_group_members WHERE group_id=?", (group_id,))
            for sid in student_ids:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO crm_group_members (group_id, student_id) VALUES (?, ?)",
                    (group_id, sid),
                )
        await self.connection.commit()
        return await self.get_crm_group(group_id)

    @serialized_transaction
    async def delete_crm_group(self, group_id: int) -> bool:
        await self.execute("DELETE FROM crm_groups WHERE id=?", (group_id,), commit=True)
        return True

    async def list_crm_groups(
        self, requester_telegram_id: int, calendar_id: Optional[str] = None
    ) -> List[dict]:
        sql = "SELECT id, trainer_id, calendar_id FROM crm_groups ORDER BY id DESC"
        rows = await self.execute(sql, fetchall=True)
        result = []
        for r in (rows or []):
            gid, g_trainer, g_cal = r[0], r[1], r[2]
            fake_st = {"trainer_id": g_trainer, "calendar_id": g_cal, "created_by": g_trainer}
            if await self.can_access_student_finances(requester_telegram_id, fake_st, calendar_id):
                grp = await self.get_crm_group(gid)
                if grp:
                    result.append(grp)
        return result

    # ──────────────────────────────────────────────────────────────────────────
    #  CRM PAYMENTS & FINANCIAL ISOLATION
    # ──────────────────────────────────────────────────────────────────────────

    async def get_upcoming_student_payments(
        self,
        requester_telegram_id: int,
        calendar_id: Optional[str] = None,
        days_ahead: int = 7,
    ) -> List[dict]:
        """
        Возвращает учеников, которым нужно сделать оплату в течение ближайших days_ahead дней
        (или у кого оплата уже просрочена / статус 'waiting_for_payment').
        Строго соблюдает финансовую тайну тренеров-арендаторов.
        """
        today_dt = datetime.now(tashkent_tz).date()
        limit_date = (today_dt + timedelta(days=days_ahead)).isoformat()

        sql = """
            SELECT id, full_name, phone, telegram_id, invite_token, status,
                   next_payment_date, payment_amount, trainer_id, calendar_id,
                   notes, coach_comment, skill_level, skills_json, polls_enabled,
                   created_by, created_at
            FROM crm_students
            WHERE (next_payment_date IS NOT NULL AND next_payment_date <= ?)
               OR status = 'waiting_for_payment'
            ORDER BY COALESCE(next_payment_date, '1970-01-01') ASC
        """
        rows = await self.execute(sql, (limit_date,), fetchall=True)
        result = []
        for r in (rows or []):
            st = self._row_to_student(r)
            if await self.can_access_student_finances(requester_telegram_id, st, calendar_id):
                if st["next_payment_date"]:
                    try:
                        np_date = date_type.fromisoformat(st["next_payment_date"])
                        st["days_until_payment"] = (np_date - today_dt).days
                    except Exception:
                        st["days_until_payment"] = None
                else:
                    st["days_until_payment"] = None
                result.append(st)
        return result

    @serialized_transaction
    async def record_student_payment(
        self,
        student_id: int,
        payment_made: bool,
        next_payment_date: Optional[str] = None,
        amount: Optional[float] = None,
        status: Optional[str] = None,
        comment: Optional[str] = None,
        paid_at: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Обрабатывает отметку об оплате ученика или изменение/удаление даты следующей оплаты.
        Если next_payment_date=None — дата убирается (например, ученик перестал ходить).
        """
        student = await self.get_crm_student(student_id)
        if not student:
            return None

        today_str = paid_at or datetime.now(tashkent_tz).date().isoformat()
        eff_amount = amount if amount is not None else (student.get("payment_amount") or 0.0)

        if payment_made:
            await self.connection.execute(
                """
                INSERT INTO crm_student_payments
                    (student_id, trainer_id, calendar_id, amount, paid_at, next_payment_date, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_id,
                    student.get("trainer_id"),
                    student.get("calendar_id"),
                    eff_amount,
                    today_str,
                    next_payment_date,
                    comment,
                ),
            )
            new_status = status if status is not None else "active"
        else:
            new_status = status if status is not None else (None if next_payment_date is None else student.get("status"))

        await self.connection.execute(
            """
            UPDATE crm_students
            SET next_payment_date=?,
                status=?,
                payment_amount=COALESCE(?, payment_amount),
                last_notif_7d_date=NULL,
                last_notif_3d_date=NULL
            WHERE id=?
            """,
            (next_payment_date, new_status, amount, student_id),
        )
        await self.connection.commit()
        return await self.get_crm_student(student_id)

    async def get_trainer_personal_earnings(
        self, trainer_id: int, from_date: str, to_date: str
    ) -> dict:
        """Личная финансовая статистика тренера (доступна ТОЛЬКО самому тренеру)."""
        rows = await self.execute(
            """
            SELECT sp.id, sp.student_id, s.full_name, sp.amount, sp.paid_at, sp.next_payment_date, sp.comment
            FROM crm_student_payments sp
            JOIN crm_students s ON s.id = sp.student_id
            WHERE sp.trainer_id = ? AND sp.paid_at BETWEEN ? AND ?
            ORDER BY sp.paid_at DESC, sp.id DESC
            """,
            (trainer_id, from_date, to_date),
            fetchall=True,
        )
        payments = [
            {
                "id": r[0],
                "student_id": r[1],
                "student_name": r[2],
                "amount": r[3],
                "paid_at": r[4],
                "next_payment_date": r[5],
                "comment": r[6],
            }
            for r in (rows or [])
        ]
        total_earned = sum(p["amount"] or 0 for p in payments)
        return {
            "trainer_id": trainer_id,
            "from_date": from_date,
            "to_date": to_date,
            "payments_count": len(payments),
            "total_earned": total_earned,
            "payments": payments,
        }

    @serialized_transaction
    async def record_trainer_rent_payment(
        self,
        calendar_id: str,
        trainer_id: int,
        amount: float,
        paid_at: Optional[str] = None,
        next_payment_date: Optional[str] = None,
        comment: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> dict:
        """Фиксирует сумму, которую тренер-арендатор заплатил владельцу корта/клубу."""
        await self.connection.execute(
            "INSERT OR IGNORE INTO calendar_users (telegram_id, username, full_name) VALUES (?, ?, ?)",
            (trainer_id, "", "Trainer"),
        )
        paid_str = paid_at or datetime.now(tashkent_tz).date().isoformat()
        await self.connection.execute(
            """
            INSERT INTO crm_trainer_rent_payments
                (calendar_id, trainer_id, amount, paid_at, next_payment_date, comment, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (calendar_id, trainer_id, amount, paid_str, next_payment_date, comment, created_by),
        )
        await self.connection.commit()
        row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
        return {
            "id": row[0],
            "calendar_id": calendar_id,
            "trainer_id": trainer_id,
            "amount": amount,
            "paid_at": paid_str,
            "next_payment_date": next_payment_date,
            "comment": comment,
            "created_by": created_by,
        }

    async def get_trainer_rent_payments(
        self, calendar_id: str, from_date: str, to_date: str
    ) -> dict:
        rows = await self.execute(
            """
            SELECT rp.id, rp.trainer_id, cu.full_name, rp.amount, rp.paid_at, rp.next_payment_date, rp.comment
            FROM crm_trainer_rent_payments rp
            LEFT JOIN calendar_users cu ON cu.telegram_id = rp.trainer_id
            WHERE rp.calendar_id = ? AND rp.paid_at BETWEEN ? AND ?
            ORDER BY rp.paid_at DESC, rp.id DESC
            """,
            (calendar_id, from_date, to_date),
            fetchall=True,
        )
        items = [
            {
                "id": r[0],
                "trainer_id": r[1],
                "trainer_name": r[2],
                "amount": r[3],
                "paid_at": r[4],
                "next_payment_date": r[5],
                "comment": r[6],
            }
            for r in (rows or [])
        ]
        return {
            "calendar_id": calendar_id,
            "from_date": from_date,
            "to_date": to_date,
            "total_rent_earned": sum(i["amount"] or 0 for i in items),
            "payments": items,
        }

    # ──────────────────────────────────────────────────────────────────────────
    #  CRM ATTENDANCE & POLL RULES
    # ──────────────────────────────────────────────────────────────────────────

    async def get_poll_schedule_rules(self) -> List[dict]:
        rows = await self.execute(
            "SELECT event_hour, send_time, days_before FROM crm_poll_schedule_rules ORDER BY event_hour ASC",
            fetchall=True,
        )
        return [
            {"event_hour": r[0], "send_time": r[1], "days_before": r[2]}
            for r in (rows or [])
        ]

    @serialized_transaction
    async def upsert_poll_schedule_rule(self, event_hour: int, send_time: str, days_before: int) -> dict:
        await self.execute(
            """
            INSERT INTO crm_poll_schedule_rules (event_hour, send_time, days_before)
            VALUES (?, ?, ?)
            ON CONFLICT(event_hour) DO UPDATE SET
                send_time=excluded.send_time,
                days_before=excluded.days_before
            """,
            (event_hour, send_time, days_before),
            commit=True,
        )
        return {"event_hour": event_hour, "send_time": send_time, "days_before": days_before}

    @serialized_transaction
    async def upsert_attendance(
        self,
        student_id: int,
        event_date: str,
        status: str,
        single_event_id: Optional[int] = None,
        recurring_event_id: Optional[int] = None,
        poll_sent: Optional[bool] = None,
    ) -> dict:
        if single_event_id is not None:
            existing = await self.execute(
                "SELECT id, poll_sent FROM crm_attendance WHERE student_id=? AND single_event_id=? AND event_date=?",
                (student_id, single_event_id, event_date),
                fetchone=True,
            )
        else:
            existing = await self.execute(
                "SELECT id, poll_sent FROM crm_attendance WHERE student_id=? AND recurring_event_id=? AND event_date=?",
                (student_id, recurring_event_id, event_date),
                fetchone=True,
            )

        if existing:
            att_id = existing[0]
            ps_val = (1 if poll_sent else 0) if poll_sent is not None else existing[1]
            await self.execute(
                "UPDATE crm_attendance SET status=?, poll_sent=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, ps_val, att_id),
                commit=True,
            )
        else:
            ps_val = 1 if poll_sent else 0
            await self.execute(
                """
                INSERT INTO crm_attendance (student_id, single_event_id, recurring_event_id, event_date, status, poll_sent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (student_id, single_event_id, recurring_event_id, event_date, status, ps_val),
                commit=True,
            )
            row = await self.execute("SELECT last_insert_rowid()", fetchone=True)
            att_id = row[0]

        return await self.get_attendance_by_id(att_id)

    async def get_attendance_by_id(self, attendance_id: int) -> Optional[dict]:
        row = await self.execute(
            """
            SELECT a.id, a.student_id, s.full_name, a.single_event_id, a.recurring_event_id,
                   a.event_date, a.status, a.poll_sent, a.updated_at
            FROM crm_attendance a
            JOIN crm_students s ON s.id = a.student_id
            WHERE a.id=?
            """,
            (attendance_id,),
            fetchone=True,
        )
        if not row:
            return None
        return {
            "id": row[0],
            "student_id": row[1],
            "student_name": row[2],
            "single_event_id": row[3],
            "recurring_event_id": row[4],
            "event_date": row[5],
            "status": row[6],
            "poll_sent": bool(row[7]),
            "updated_at": row[8],
        }

    @serialized_transaction
    async def update_attendance_status_by_id(self, attendance_id: int, status: str) -> Optional[dict]:
        await self.execute(
            "UPDATE crm_attendance SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, attendance_id),
            commit=True,
        )
        return await self.get_attendance_by_id(attendance_id)

    async def get_event_attendance(
        self,
        event_date: str,
        single_event_id: Optional[int] = None,
        recurring_event_id: Optional[int] = None,
    ) -> List[dict]:
        if single_event_id is not None:
            rows = await self.execute(
                """
                SELECT a.id, a.student_id, s.full_name, s.phone, a.status, a.poll_sent, a.updated_at
                FROM crm_attendance a
                JOIN crm_students s ON s.id = a.student_id
                WHERE a.single_event_id=? AND a.event_date=?
                """,
                (single_event_id, event_date),
                fetchall=True,
            )
        else:
            rows = await self.execute(
                """
                SELECT a.id, a.student_id, s.full_name, s.phone, a.status, a.poll_sent, a.updated_at
                FROM crm_attendance a
                JOIN crm_students s ON s.id = a.student_id
                WHERE a.recurring_event_id=? AND a.event_date=?
                """,
                (recurring_event_id, event_date),
                fetchall=True,
            )
        return [
            {
                "id": r[0],
                "student_id": r[1],
                "student_name": r[2],
                "phone": r[3],
                "status": r[4],
                "poll_sent": bool(r[5]),
                "updated_at": r[6],
            }
            for r in (rows or [])
        ]

    # ──────────────────────────────────────────────────────────────────────────
    #  STUDENT PANEL (7 BLOCKS + NON-STUDENT SHOWCASE)
    # ──────────────────────────────────────────────────────────────────────────

    async def get_student_panel_data(self, telegram_id: int) -> dict:
        """
        Собирает данные для Панели ученика по X-Telegram-Id.
        Если пользователь не ученик (например, обычный бронирующий),
        возвращает is_student=False + витрину тренеров и кнопку брони корта.
        """
        student = await self.get_crm_student_by_telegram_id(telegram_id)
        all_trainers = await self.get_all_calendar_users()
        is_trainer = any(t["telegram_id"] == telegram_id for t in all_trainers)
        if not is_trainer:
            cm_row = await self.execute(
                "SELECT 1 FROM calendar_members WHERE telegram_id=? LIMIT 1",
                (telegram_id,),
                fetchone=True,
            )
            is_trainer = cm_row is not None

        trainer_banner = (
            {
                "show": True,
                "title": "Вы вошли как тренер / администратор",
                "description": (
                    "Управление расписанием, учениками, группами и оплатами "
                    "находится в Админ-панели (CRM)."
                ),
                "cta_text": "Перейти в Админ-панель (CRM)",
            }
            if is_trainer
            else None
        )

        trainers_showcase = [
            {
                "telegram_id": t["telegram_id"],
                "full_name": t["full_name"],
                "username": t["username"],
                "phone": t["phone"],
                "bio": t["bio"],
                "photo_url": t.get("photo_url"),
                "is_independent": t["is_independent"],
            }
            for t in all_trainers
        ]

        if not student:
            return {
                "is_student": False,
                "is_trainer": is_trainer,
                "trainer_banner": trainer_banner,
                "promo_banner": {
                    "title": "Станьте учеником клуба!",
                    "description": (
                        "Панель ученика доступна тем, кто занимается с тренером. "
                        "Выберите тренера для персональных или групповых тренировок, "
                        "чтобы отслеживать свой прогресс, навыки и расписание!"
                    ),
                },
                "student_data": None,
                "trainers": trainers_showcase,
                "self_practice_booking": {
                    "enabled": True,
                    "cta_text": "Забронировать корт для самостоятельной игры",
                },
            }

        student_id = student["id"]
        today_dt = datetime.now(tashkent_tz).date()
        today_str = today_dt.isoformat()

        # 1. Блок оплаты и индикатор
        days_left = None
        payment_color = "none"
        if student["next_payment_date"]:
            try:
                np_date = date_type.fromisoformat(student["next_payment_date"])
                days_left = (np_date - today_dt).days
                if days_left <= 3 or student["status"] == "waiting_for_payment":
                    payment_color = "red"
                elif days_left <= 7:
                    payment_color = "yellow"
                else:
                    payment_color = "green"
            except Exception:
                pass

        # Группы, в которых состоит ученик
        grp_rows = await self.execute(
            "SELECT group_id FROM crm_group_members WHERE student_id=?",
            (student_id,),
            fetchall=True,
        )
        group_ids = [r[0] for r in (grp_rows or [])]

        # 2. Ближайшие тренировки (на 14 дней вперед: одиночные + повторяющиеся)
        lookahead_dt = today_dt + timedelta(days=14)
        from_dt_str = f"{today_str} 00:00:00"
        to_dt_str = f"{lookahead_dt.isoformat()} 23:59:59"

        upcoming = []
        if group_ids:
            g_placeholders = ", ".join("?" * len(group_ids))
            single_sql = f"""
                SELECT id, calendar_id, title, start_datetime, end_datetime, event_type, group_id, trainer_id
                FROM single_events
                WHERE status IN ('confirmed', 'pending_payment')
                  AND start_datetime BETWEEN ? AND ?
                  AND (student_id = ? OR group_id IN ({g_placeholders}))
            """
            single_params = (from_dt_str, to_dt_str, student_id, *group_ids)
        else:
            single_sql = """
                SELECT id, calendar_id, title, start_datetime, end_datetime, event_type, group_id, trainer_id
                FROM single_events
                WHERE status IN ('confirmed', 'pending_payment')
                  AND start_datetime BETWEEN ? AND ?
                  AND student_id = ?
            """
            single_params = (from_dt_str, to_dt_str, student_id)

        s_rows = await self.execute(single_sql, single_params, fetchall=True)
        for r in (s_rows or []):
            ev_date = r[3][:10]
            att_row = await self.execute(
                "SELECT id, status FROM crm_attendance WHERE student_id=? AND single_event_id=? AND event_date=?",
                (student_id, r[0], ev_date),
                fetchone=True,
            )
            upcoming.append({
                "event_id": r[0],
                "type": "single",
                "calendar_id": r[1],
                "title": r[2],
                "date": ev_date,
                "start_datetime": r[3],
                "end_datetime": r[4],
                "event_type": r[5] or "individual",
                "group_id": r[6],
                "trainer_id": r[7],
                "attendance_id": att_row[0] if att_row else None,
                "attendance_status": att_row[1] if att_row else "planned",
            })

        # Повторяющиеся события ученика/групп
        if group_ids:
            g_placeholders = ", ".join("?" * len(group_ids))
            rec_sql = f"""
                SELECT id, calendar_id, title, day_of_week, start_time, end_time, event_type, group_id, trainer_id
                FROM recurring_events
                WHERE student_id = ? OR group_id IN ({g_placeholders})
            """
            rec_params = (student_id, *group_ids)
        else:
            rec_sql = """
                SELECT id, calendar_id, title, day_of_week, start_time, end_time, event_type, group_id, trainer_id
                FROM recurring_events
                WHERE student_id = ?
            """
            rec_params = (student_id,)

        r_rows = await self.execute(rec_sql, rec_params, fetchall=True)
        if r_rows:
            cur_d = today_dt
            while cur_d <= lookahead_dt:
                d_str = cur_d.isoformat()
                wd = cur_d.weekday()
                for rr in r_rows:
                    if rr[3] == wd:
                        # Проверяем, не отменен ли этот экземпляр
                        canc = await self.execute(
                            """
                            SELECT 1 FROM single_events
                            WHERE recurring_event_id=? AND substr(start_datetime, 1, 10)=? AND status='cancelled'
                            """,
                            (rr[0], d_str),
                            fetchone=True,
                        )
                        if canc:
                            continue
                        att_row = await self.execute(
                            "SELECT id, status FROM crm_attendance WHERE student_id=? AND recurring_event_id=? AND event_date=?",
                            (student_id, rr[0], d_str),
                            fetchone=True,
                        )
                        upcoming.append({
                            "event_id": rr[0],
                            "type": "recurring_instance",
                            "calendar_id": rr[1],
                            "title": rr[2],
                            "date": d_str,
                            "start_datetime": f"{d_str} {rr[4]}",
                            "end_datetime": f"{d_str} {rr[5]}",
                            "event_type": rr[6] or ("group" if rr[7] else "individual"),
                            "group_id": rr[7],
                            "trainer_id": rr[8],
                            "attendance_id": att_row[0] if att_row else None,
                            "attendance_status": att_row[1] if att_row else "planned",
                        })
                cur_d += timedelta(days=1)

        upcoming.sort(key=lambda x: x["start_datetime"])
        upcoming = upcoming[:5]

        # 3. Геймификация и статистика посещений + История посещений
        att_rows = await self.execute(
            """
            SELECT id, event_date, status, single_event_id, recurring_event_id
            FROM crm_attendance
            WHERE student_id = ?
            ORDER BY event_date DESC, id DESC
            """,
            (student_id,),
            fetchall=True,
        )
        attended_statuses = ("attended", "confirmed")
        missed_statuses = ("missed", "declined")

        total_attended = sum(1 for a in (att_rows or []) if a[2] in attended_statuses and a[1] <= today_str)
        month_prefix = today_str[:7]
        month_attended = sum(
            1 for a in (att_rows or [])
            if a[2] in attended_statuses and a[1].startswith(month_prefix) and a[1] <= today_str
        )

        streak = 0
        for a in (att_rows or []):
            if a[1] > today_str:
                continue
            if a[2] in attended_statuses:
                streak += 1
            elif a[2] in missed_statuses:
                break

        recent_attendance = [
            {
                "id": a[0],
                "date": a[1],
                "status": a[2],
                "single_event_id": a[3],
                "recurring_event_id": a[4],
            }
            for a in (att_rows or [])[:10]
        ]

        # 4. Карточка игрока (Уровень и Навыки для графиков на фронте)
        default_skills = {
            "forehand": 5,
            "backhand": 5,
            "serve": 5,
            "footwork": 5,
            "endurance": 5,
        }
        skills_data = student["skills"] if isinstance(student["skills"], dict) else default_skills

        # 6. История последних оплат
        pay_rows = await self.execute(
            """
            SELECT id, amount, paid_at, next_payment_date, comment
            FROM crm_student_payments
            WHERE student_id = ?
            ORDER BY paid_at DESC, id DESC
            LIMIT 5
            """,
            (student_id,),
            fetchall=True,
        )
        recent_payments = [
            {
                "id": p[0],
                "amount": p[1],
                "paid_at": p[2],
                "next_payment_date": p[3],
                "comment": p[4],
            }
            for p in (pay_rows or [])
        ]

        # 7. Мой тренер и другие тренеры
        my_trainer = None
        other_trainers = []
        for t in trainers_showcase:
            if student["trainer_id"] and t["telegram_id"] == student["trainer_id"]:
                my_trainer = t
            else:
                other_trainers.append(t)

        return {
            "is_student": True,
            "is_trainer": is_trainer,
            "trainer_banner": trainer_banner,
            "student_data": {
                "id": student["id"],
                "full_name": student["full_name"],
                "phone": student["phone"],
                "status": student["status"] or "active",
                "polls_enabled": student["polls_enabled"],
                "payment_info": {
                    "next_payment_date": student["next_payment_date"],
                    "days_left": days_left,
                    "indicator_color": payment_color,
                    "status": student["status"] or "active",
                },
                "upcoming_trainings": upcoming,
                "gamification": {
                    "total_attended": total_attended,
                    "month_attended": month_attended,
                    "streak": streak,
                },
                "player_card": {
                    "skill_level": student["skill_level"] or "Начинающий",
                    "skills": skills_data,
                },
                "coach_comment": student["coach_comment"],
                "history": {
                    "recent_attendance": recent_attendance,
                    "recent_payments": recent_payments,
                },
                "my_trainer": my_trainer,
            },
            "other_trainers": other_trainers,
            "self_practice_booking": {
                "enabled": True,
                "cta_text": "Хотите потренировать подачу самостоятельно? Забронируйте свободный корт!",
            },
        }


db = Database()
# asyncio.run(db.connect())
# asyncio.run(db.gg())


