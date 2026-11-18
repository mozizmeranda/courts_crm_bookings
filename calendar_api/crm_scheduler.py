import asyncio
import logging
from datetime import datetime, timedelta, date as date_type
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.database import db

logger = logging.getLogger(__name__)
tashkent_tz = ZoneInfo("Asia/Tashkent")


def build_poll_keyboard(attendance_id: int, student_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, приду",
                    callback_data=f"crm_poll:yes:{attendance_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Нет, не приду",
                    callback_data=f"crm_poll:no:{attendance_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔕 Отключить опросы",
                    callback_data=f"crm_poll:mute:{student_id}",
                )
            ],
        ]
    )


async def _get_event_students(event: dict) -> list[dict]:
    """Возвращает список учеников для события (индивидуального или группового)."""
    students = []
    if event.get("student_id"):
        st = await db.get_crm_student(event["student_id"])
        if st:
            students.append(st)
    if event.get("group_id"):
        grp = await db.get_crm_group(event["group_id"])
        if grp and grp.get("students"):
            seen = {s["id"] for s in students}
            for s in grp["students"]:
                if s["id"] not in seen:
                    students.append(s)
                    seen.add(s["id"])
    return students


async def process_due_polls(now_dt: Optional[datetime] = None) -> int:
    """
    Проверяет тренировки (на сегодня и на завтра, с учетом poll_days_before и poll_send_time в БД)
    и отправляет опросы тем ученикам, у которых включены опросы (polls_enabled=1) и привязан Telegram.
    Возвращает количество отправленных опросов.
    """
    from telegram_bot.bot_app import bot

    if now_dt is None:
        now_dt = datetime.now(tashkent_tz)

    today_d = now_dt.date()
    now_time_str = now_dt.strftime("%H:%M")
    sent_count = 0

    # Проверяем события на сегодня (days_before=0) и на завтра (days_before=1, напр. для утренней в 07:00)
    for offset_days in (0, 1):
        target_date = today_d + timedelta(days=offset_days)
        target_date_str = target_date.isoformat()
        from_dt = f"{target_date_str} 00:00:00"
        to_dt = f"{target_date_str} 23:59:59"

        # 1. Одиночные события с send_poll=1
        single_rows = await db.execute(
            """
            SELECT id, calendar_id, created_by, title, start_datetime, end_datetime,
                   event_type, student_id, group_id, trainer_id,
                   send_poll, poll_send_time, poll_days_before
            FROM single_events
            WHERE start_datetime BETWEEN ? AND ?
              AND status IN ('confirmed', 'pending_payment')
              AND send_poll = 1
            """,
            (from_dt, to_dt),
            fetchall=True,
        )

        for r in (single_rows or []):
            ev = {
                "id": r[0],
                "calendar_id": r[1],
                "created_by": r[2],
                "title": r[3],
                "start_datetime": r[4],
                "end_datetime": r[5],
                "event_type": r[6],
                "student_id": r[7],
                "group_id": r[8],
                "trainer_id": r[9],
                "send_poll": bool(r[10]),
                "poll_send_time": r[11],
                "poll_days_before": r[12] if r[12] is not None else 0,
            }
            start_hour = int(ev["start_datetime"][11:13])
            send_time, days_before = (
                (ev["poll_send_time"], ev["poll_days_before"])
                if ev["poll_send_time"]
                else await db._resolve_default_poll_schedule(start_hour)
            )
            if days_before != offset_days or now_time_str < send_time[:5]:
                continue

            students = await _get_event_students(ev)
            for st in students:
                if not st.get("telegram_id") or not st.get("polls_enabled", True):
                    continue
                existing = await db.execute(
                    "SELECT id, poll_sent FROM crm_attendance WHERE student_id=? AND single_event_id=? AND event_date=?",
                    (st["id"], ev["id"], target_date_str),
                    fetchone=True,
                )
                if existing and existing[1]:
                    continue

                att = await db.upsert_attendance(
                    student_id=st["id"],
                    event_date=target_date_str,
                    status="planned",
                    single_event_id=ev["id"],
                    poll_sent=True,
                )
                time_label = ev["start_datetime"][11:16]
                day_word = "Сегодня" if offset_days == 0 else f"Завтра ({target_date_str})"
                text = (
                    f"🎾 <b>Опрос перед тренировкой</b>\n\n"
                    f"{day_word} в <b>{time_label}</b> у вас занятие: <b>{ev['title']}</b>.\n"
                    f"Подскажите, вы придете?"
                )
                try:
                    await bot.send_message(
                        chat_id=st["telegram_id"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=build_poll_keyboard(att["id"], st["id"]),
                    )
                    sent_count += 1
                except Exception as e:
                    logger.warning("Failed to send poll to student %s: %s", st["id"], e)

        # 2. Повторяющиеся события с send_poll=1
        weekday = target_date.weekday()
        rec_rows = await db.execute(
            """
            SELECT id, calendar_id, created_by, title, day_of_week, start_time, end_time,
                   event_type, student_id, group_id, trainer_id,
                   send_poll, poll_send_time, poll_days_before
            FROM recurring_events
            WHERE day_of_week = ? AND send_poll = 1
            """,
            (weekday,),
            fetchall=True,
        )

        for r in (rec_rows or []):
            rec_id = r[0]
            # Проверяем, не отменено ли на эту дату
            canc = await db.execute(
                """
                SELECT 1 FROM single_events
                WHERE recurring_event_id=? AND substr(start_datetime, 1, 10)=? AND status='cancelled'
                """,
                (rec_id, target_date_str),
                fetchone=True,
            )
            if canc:
                continue

            ev = {
                "id": rec_id,
                "calendar_id": r[1],
                "created_by": r[2],
                "title": r[3],
                "day_of_week": r[4],
                "start_time": r[5],
                "end_time": r[6],
                "event_type": r[7],
                "student_id": r[8],
                "group_id": r[9],
                "trainer_id": r[10],
                "send_poll": bool(r[11]),
                "poll_send_time": r[12],
                "poll_days_before": r[13] if r[13] is not None else 0,
            }
            start_hour = int(ev["start_time"][:2])
            send_time, days_before = (
                (ev["poll_send_time"], ev["poll_days_before"])
                if ev["poll_send_time"]
                else await db._resolve_default_poll_schedule(start_hour)
            )
            if days_before != offset_days or now_time_str < send_time[:5]:
                continue

            students = await _get_event_students(ev)
            for st in students:
                if not st.get("telegram_id") or not st.get("polls_enabled", True):
                    continue
                existing = await db.execute(
                    "SELECT id, poll_sent FROM crm_attendance WHERE student_id=? AND recurring_event_id=? AND event_date=?",
                    (st["id"], rec_id, target_date_str),
                    fetchone=True,
                )
                if existing and existing[1]:
                    continue

                att = await db.upsert_attendance(
                    student_id=st["id"],
                    event_date=target_date_str,
                    status="planned",
                    recurring_event_id=rec_id,
                    poll_sent=True,
                )
                time_label = ev["start_time"][:5]
                day_word = "Сегодня" if offset_days == 0 else f"Завтра ({target_date_str})"
                text = (
                    f"🎾 <b>Опрос перед тренировкой</b>\n\n"
                    f"{day_word} в <b>{time_label}</b> у вас занятие: <b>{ev['title']}</b>.\n"
                    f"Подскажите, вы придете?"
                )
                try:
                    await bot.send_message(
                        chat_id=st["telegram_id"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=build_poll_keyboard(att["id"], st["id"]),
                    )
                    sent_count += 1
                except Exception as e:
                    logger.warning("Failed to send recurring poll to student %s: %s", st["id"], e)

    return sent_count


async def process_payment_notifications(today_d: Optional[date_type] = None) -> int:
    """
    Проверяет даты следующей оплаты учеников:
    - За 7 дней: отправляет напоминание ученику (и тренеру).
    - За 3 дня (если оплата еще не была проведена): ставит статус 'waiting_for_payment' и шлет напоминание.
    Возвращает количество обработанных уведомлений.
    """
    from telegram_bot.bot_app import bot

    if today_d is None:
        today_d = datetime.now(tashkent_tz).date()

    date_in_7d = (today_d + timedelta(days=7)).isoformat()
    date_in_3d = (today_d + timedelta(days=3)).isoformat()
    processed = 0

    # 1. За 7 дней до оплаты
    rows_7d = await db.execute(
        """
        SELECT id, full_name, telegram_id, next_payment_date, payment_amount, trainer_id
        FROM crm_students
        WHERE next_payment_date = ?
          AND COALESCE(last_notif_7d_date, '') != next_payment_date
        """,
        (date_in_7d,),
        fetchall=True,
    )
    for r in (rows_7d or []):
        st_id, full_name, tg_id, np_date, amount, trainer_id = r
        await db.execute(
            "UPDATE crm_students SET last_notif_7d_date=? WHERE id=?",
            (np_date, st_id),
            commit=True,
        )
        processed += 1
        if tg_id:
            try:
                amt_text = f" на сумму <b>{amount}</b>" if amount else ""
                await bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"💳 <b>Напоминание об оплате</b>\n\n"
                        f"Здравствуйте, {full_name}! Через 7 дней (<b>{np_date}</b>) "
                        f"подходит дата следующей оплаты тренировок{amt_text}."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to send 7d payment reminder to student %s: %s", st_id, e)

    # 2. За 3 дня до оплаты (если оплата еще не проведена -> waiting_for_payment)
    rows_3d = await db.execute(
        """
        SELECT id, full_name, telegram_id, next_payment_date, payment_amount, trainer_id
        FROM crm_students
        WHERE next_payment_date = ?
          AND COALESCE(last_notif_3d_date, '') != next_payment_date
        """,
        (date_in_3d,),
        fetchall=True,
    )
    for r in (rows_3d or []):
        st_id, full_name, tg_id, np_date, amount, trainer_id = r
        await db.execute(
            "UPDATE crm_students SET status='waiting_for_payment', last_notif_3d_date=? WHERE id=?",
            (np_date, st_id),
            commit=True,
        )
        processed += 1
        if tg_id:
            try:
                amt_text = f" (сумма: <b>{amount}</b>)" if amount else ""
                await bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"⏳ <b>Скоро дата оплаты тренировок</b>\n\n"
                        f"Здравствуйте, {full_name}! Напоминаем, что через 3 дня (<b>{np_date}</b>) "
                        f"необходимо внести оплату за тренировки{amt_text}."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to send 3d payment reminder to student %s: %s", st_id, e)
        if trainer_id:
            try:
                await bot.send_message(
                    chat_id=trainer_id,
                    text=(
                        f"🔔 Напоминание тренеру: у ученика <b>{full_name}</b> "
                        f"через 3 дня ({np_date}) дата следующей оплаты."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to send 3d payment reminder to trainer %s: %s", trainer_id, e)

    return processed


async def crm_background_loop():
    """Фоновый цикл для отправки опросов по расписанию и уведомлений об оплатах."""
    while True:
        try:
            await process_due_polls()
            now_tashkent = datetime.now(tashkent_tz)
            # Уведомления об оплате проверяем в дневное время (с 09:00 до 21:00)
            if 9 <= now_tashkent.hour <= 21:
                await process_payment_notifications(now_tashkent.date())
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in crm_background_loop")
        await asyncio.sleep(60)
