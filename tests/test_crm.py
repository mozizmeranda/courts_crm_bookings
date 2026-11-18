from datetime import datetime, date, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from httpx import AsyncClient

from database.database import db
from calendar_api.crm_scheduler import process_due_polls, process_payment_notifications, tashkent_tz

pytestmark = pytest.mark.asyncio


async def test_student_create_and_search(client: AsyncClient):
    # 1. Создание ученика только с обязательным full_name
    resp = await client.post(
        "/v1/crm/students",
        json={"full_name": "Иван Петров"},
        headers={"X-Telegram-Id": "123"},
    )
    assert resp.status_code == 201
    st1 = resp.json()
    assert st1["full_name"] == "Иван Петров"
    assert st1["phone"] is None
    assert st1["next_payment_date"] is None
    assert st1["status"] is None
    assert st1["invite_token"] is not None
    assert "?start=stu_" in st1["invite_link"]

    # 2. Создание ученика с телефоном и датой следующей оплаты
    resp2 = await client.post(
        "/v1/crm/students",
        json={
            "full_name": "Азиза Каримова",
            "phone": "+998901234567",
            "status": "waiting_for_payment",
            "next_payment_date": "2026-11-20",
            "payment_amount": 500000,
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert resp2.status_code == 201

    # 3. Поиск по имени (регистронезависимый для кириллицы)
    search_name = await client.get(
        "/v1/crm/students/search?q=иван",
        headers={"X-Telegram-Id": "123"},
    )
    assert search_name.status_code == 200
    found_names = [s["full_name"] for s in search_name.json()]
    assert "Иван Петров" in found_names

    # 4. Поиск по части номера телефона
    search_phone = await client.get(
        "/v1/crm/students/search?q=901234",
        headers={"X-Telegram-Id": "123"},
    )
    assert search_phone.status_code == 200
    found_phones = [s["full_name"] for s in search_phone.json()]
    assert "Азиза Каримова" in found_phones


async def test_inline_student_and_group_in_calendar_events(client: AsyncClient):
    # Создание события с прикреплением нового ученика прямо из модалки календаря
    ev_resp = await client.post(
        "/v1/calendars/Test 2/single-events",
        json={
            "title": "Индивидуальная тренировка",
            "start_datetime": "2026-10-15T09:00:00Z",
            "end_datetime": "2026-10-15T10:00:00Z",
            "status": "confirmed",
            "new_student": {
                "full_name": "Тимур Алиев",
                "phone": "+998909998877",
            },
            "send_poll": True,
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert ev_resp.status_code == 201
    ev_data = ev_resp.json()
    assert ev_data["event_type"] == "individual"
    assert ev_data["student_id"] is not None
    assert ev_data["created_student"]["full_name"] == "Тимур Алиев"
    # Для 09:00 утра дефолтное правило опроса: 20:00 за 1 день до тренировки
    assert ev_data["poll_send_time"] == "20:00"
    assert ev_data["poll_days_before"] == 1

    # Создание объекта группы с готовыми участниками
    grp_resp = await client.post(
        "/v1/crm/groups",
        json={
            "name": "Утренняя группа ПН-СР",
            "calendar_id": "Test 2",
            "student_ids": [ev_data["student_id"]],
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert grp_resp.status_code == 201
    grp = grp_resp.json()
    assert grp["name"] == "Утренняя группа ПН-СР"
    assert len(grp["students"]) == 1

    # Создание постоянного группового события
    rec_resp = await client.post(
        "/v1/calendars/Test 2/recurring-events",
        json={
            "title": "Группа ПН",
            "days_of_week": [0],
            "start_time": "16:00:00Z",
            "end_time": "17:00:00Z",
            "event_type": "group",
            "group_id": grp["id"],
            "send_poll": True,
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert rec_resp.status_code == 201
    rec_list = rec_resp.json()
    assert rec_list[0]["event_type"] == "group"
    assert rec_list[0]["group_id"] == grp["id"]
    assert rec_list[0]["poll_send_time"] == "12:00"
    assert rec_list[0]["poll_days_before"] == 0


async def test_trainer_schedule_permissions(client: AsyncClient):
    # Создаем тренера 555, которому ЗАПРЕЩЕНО менять расписание (can_edit_schedule=False)
    await client.post(
        "/v1/calendars/create_user",
        json={"telegram_id": 555, "username": "restricted_tr", "full_name": "Тренер Без Расписания"},
    )
    await client.post(
        "/v1/calendars/calendar_members",
        json={
            "calendar_id": "Test 2",
            "telegram_id": 555,
            "role": "trainer",
            "can_edit_schedule": False,
            "employment_type": "club",
        },
    )

    # 1. Тренер 555 пытается сам создать новый слот в расписании -> 403 Forbidden
    create_attempt = await client.post(
        "/v1/calendars/Test 2/single-events",
        json={
            "title": "Самовольный слот",
            "start_datetime": "2026-10-16T10:00:00Z",
            "end_datetime": "2026-10-16T11:00:00Z",
            "status": "confirmed",
        },
        headers={"X-Telegram-Id": "555"},
    )
    assert create_attempt.status_code == 403

    # 2. Админ 123 выделяет тренеру 555 время в расписании (trainer_id=555)
    admin_slot = await client.post(
        "/v1/calendars/Test 2/single-events",
        json={
            "title": "Время тренера 555",
            "start_datetime": "2026-10-16T14:00:00Z",
            "end_datetime": "2026-10-16T15:00:00Z",
            "status": "confirmed",
            "trainer_id": 555,
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert admin_slot.status_code == 201
    slot_id = admin_slot.json()["id"]

    # 3. Тренер 555 пытается передвинуть время своего слота -> 403 Forbidden
    move_attempt = await client.patch(
        f"/v1/single-events/{slot_id}",
        json={
            "start_datetime": "2026-10-16T15:00:00Z",
            "end_datetime": "2026-10-16T16:00:00Z",
        },
        headers={"X-Telegram-Id": "555"},
    )
    assert move_attempt.status_code == 403

    # 4. Но тренер 555 МОЖЕТ прикрепить своего ученика внутри выделенного ему слота!
    attach_student = await client.patch(
        f"/v1/single-events/{slot_id}",
        json={
            "title": "Тренировка с учеником",
            "new_student": {"full_name": "Ученик Тренера 555"},
        },
        headers={"X-Telegram-Id": "555"},
    )
    assert attach_student.status_code == 200
    assert attach_student.json()["event_type"] == "individual"
    assert attach_student.json()["student_id"] is not None

    # 5. Тренер 555 НЕ может повысить себя до админа -> 403 Forbidden
    promote_attempt = await client.patch(
        "/v1/crm/calendars/Test 2/members/555",
        json={"role": "admin"},
        headers={"X-Telegram-Id": "555"},
    )
    assert promote_attempt.status_code == 403


async def test_financial_privacy_and_payments(client: AsyncClient):
    # Создаем тренера-арендатора (employment_type='rent', telegram_id=777)
    await client.post(
        "/v1/calendars/create_user",
        json={"telegram_id": 777, "username": "rent_coach", "full_name": "Тренер Арендатор"},
    )
    await client.post(
        "/v1/calendars/calendar_members",
        json={
            "calendar_id": "Test 2",
            "telegram_id": 777,
            "role": "trainer",
            "can_edit_schedule": True,
            "employment_type": "rent",
        },
    )

    today_str = datetime.now(tashkent_tz).date().isoformat()
    due_in_5d = (datetime.now(tashkent_tz).date() + timedelta(days=5)).isoformat()

    # Тренер-арендатор 777 создает своего личного ученика с оплатой через 5 дней
    st_rent_resp = await client.post(
        "/v1/crm/students",
        json={
            "full_name": "Личный Ученик Арендатора",
            "next_payment_date": due_in_5d,
            "payment_amount": 1000000,
        },
        headers={"X-Telegram-Id": "777"},
    )
    assert st_rent_resp.status_code == 201
    st_rent_id = st_rent_resp.json()["id"]

    # 1. Тренер 777 видит этого ученика в списке предстоящих оплат на неделю
    upcoming_777 = await client.get(
        "/v1/crm/payments/upcoming",
        headers={"X-Telegram-Id": "777"},
    )
    assert upcoming_777.status_code == 200
    assert any(s["id"] == st_rent_id for s in upcoming_777.json())

    # 2. Админ корта (123) НЕ ВИДИТ личного ученика тренера-арендатора 777!
    upcoming_admin = await client.get(
        "/v1/crm/payments/upcoming?calendar_id=Test 2",
        headers={"X-Telegram-Id": "123"},
    )
    assert upcoming_admin.status_code == 200
    assert not any(s["id"] == st_rent_id for s in upcoming_admin.json())

    # 3. Тренер 777 отмечает оплату ученика и ставит новую дату (или потом убирает её)
    next_month = (datetime.now(tashkent_tz).date() + timedelta(days=30)).isoformat()
    pay_resp = await client.patch(
        f"/v1/crm/students/{st_rent_id}/payment",
        json={
            "payment_made": True,
            "next_payment_date": next_month,
            "amount": 1000000,
            "paid_at": today_str,
        },
        headers={"X-Telegram-Id": "777"},
    )
    assert pay_resp.status_code == 200
    assert pay_resp.json()["next_payment_date"] == next_month

    # Тренер 777 видит свой заработок (1 000 000)
    earnings_777 = await client.get(
        f"/v1/crm/trainers/me/earnings?from_date={today_str}&to_date={today_str}",
        headers={"X-Telegram-Id": "777"},
    )
    assert earnings_777.status_code == 200
    assert earnings_777.json()["total_earned"] == 1000000

    # А в статистику владельца корта (123) этот 1 000 000 НЕ попадает!
    stats_admin = await client.get(
        f"/v1/calendars/Test 2/stats?from_date={today_str}&to_date={today_str}",
        headers={"X-Telegram-Id": "123"},
    )
    assert stats_admin.status_code == 200
    assert stats_admin.json()["club_students_earned"] == 0

    # Зато когда тренер-арендатор 777 дает владельцу корта 400 000 за аренду — владелец корта это фиксирует и видит!
    rent_pay = await client.post(
        "/v1/crm/calendars/Test 2/trainer-rent-payments",
        json={"trainer_id": 777, "amount": 400000, "paid_at": today_str, "comment": "Аренда за неделю"},
        headers={"X-Telegram-Id": "123"},
    )
    assert rent_pay.status_code == 201

    stats_admin_after = await client.get(
        f"/v1/calendars/Test 2/stats?from_date={today_str}&to_date={today_str}",
        headers={"X-Telegram-Id": "123"},
    )
    assert stats_admin_after.json()["trainer_rent_earned"] == 400000
    assert stats_admin_after.json()["total_earned"] == 400000

    # 4. Убираем дату следующей оплаты (next_payment_date=None), если ученик больше не ходит
    clear_date_resp = await client.patch(
        f"/v1/crm/students/{st_rent_id}/payment",
        json={"payment_made": False, "next_payment_date": None},
        headers={"X-Telegram-Id": "777"},
    )
    assert clear_date_resp.status_code == 200
    assert clear_date_resp.json()["next_payment_date"] is None


async def test_deeplink_and_student_panel(client: AsyncClient):
    # 1. Посторонний пользователь (например, обычный бронирующий tg_id=88888) заходит в Панель ученика
    non_student_resp = await client.get(
        "/v1/crm/student-panel",
        headers={"X-Telegram-Id": "88888"},
    )
    assert non_student_resp.status_code == 200
    ns_data = non_student_resp.json()
    assert ns_data["is_student"] is False
    assert ns_data["student_data"] is None
    assert isinstance(ns_data["trainers"], list)

    # 2. Создаем ученика с навыками и заметкой от тренера, затем привязываем через диплинк-токен к tg_id=99999
    create_resp = await client.post(
        "/v1/crm/students",
        json={
            "full_name": "Сардор Ученик",
            "next_payment_date": "2026-12-01",
            "coach_comment": "Отличный форхенд, держим концентрацию на подаче!",
            "skill_level": "Юниор",
            "skills": {"forehand": 8, "backhand": 7, "serve": 6, "footwork": 9, "endurance": 8},
        },
        headers={"X-Telegram-Id": "123"},
    )
    st = create_resp.json()
    token = st["invite_token"]

    await db.insert_into(99999, "Сардор", "sardor_tg", "+998901112233", "ru")
    link_res = await db.link_crm_student_by_token(token, 99999)
    assert link_res["status"] == "linked"
    assert link_res["student"]["phone"] == "+998901112233"

    # 3. Теперь ученик 99999 открывает Панель ученика
    panel_resp = await client.get(
        "/v1/crm/student-panel",
        headers={"X-Telegram-Id": "99999"},
    )
    assert panel_resp.status_code == 200
    p_data = panel_resp.json()
    assert p_data["is_student"] is True
    sd = p_data["student_data"]
    assert sd["full_name"] == "Сардор Ученик"
    assert sd["coach_comment"] == "Отличный форхенд, держим концентрацию на подаче!"
    assert sd["player_card"]["skill_level"] == "Юниор"
    assert sd["player_card"]["skills"]["forehand"] == 8
    assert sd["payment_info"]["next_payment_date"] == "2026-12-01"

    # 4. Ученик отключает для себя опросы в настройках Панели ученика
    settings_resp = await client.patch(
        "/v1/crm/student-panel/settings",
        json={"polls_enabled": False},
        headers={"X-Telegram-Id": "99999"},
    )
    assert settings_resp.status_code == 200
    assert settings_resp.json()["polls_enabled"] is False


async def test_polls_and_payment_notifications_scheduler(client: AsyncClient):
    # Создаем ученика с включенными опросами и датой оплаты через 3 дня
    base_date = date(2026, 11, 20)
    pay_in_3d = (base_date + timedelta(days=3)).isoformat()

    st_resp = await client.post(
        "/v1/crm/students",
        json={
            "full_name": "Алексей Тест Опросов",
            "next_payment_date": pay_in_3d,
            "polls_enabled": True,
        },
        headers={"X-Telegram-Id": "123"},
    )
    st = st_resp.json()
    await db.insert_into(70001, "Алексей", "alex_tg", "+998907770011", "ru")
    await db.link_crm_student_by_token(st["invite_token"], 70001)

    # Создаем утреннюю тренировку на завтра (2026-11-21 в 07:00) -> опрос должен уйти 2026-11-20 в 20:00
    ev_resp = await client.post(
        "/v1/calendars/Test 2/single-events",
        json={
            "title": "Утренняя группа 07:00",
            "start_datetime": "2026-11-21T07:00:00Z",
            "end_datetime": "2026-11-21T08:00:00Z",
            "status": "confirmed",
            "event_type": "individual",
            "student_id": st["id"],
            "send_poll": True,
        },
        headers={"X-Telegram-Id": "123"},
    )
    assert ev_resp.status_code == 201

    with patch("telegram_bot.bot_app.bot.send_message", new_callable=AsyncMock) as mock_send:
        # 1. Проверяем отправку опроса в 20:05 накануне (2026-11-20 20:05)
        now_evening = datetime(2026, 11, 20, 20, 5, tzinfo=tashkent_tz)
        sent_polls = await process_due_polls(now_evening)
        assert sent_polls >= 1
        assert mock_send.called

        # Повторный вызов в 20:06 НЕ отправляет дубль опроса
        sent_again = await process_due_polls(now_evening)
        assert sent_again == 0

        # 2. Проверяем уведомление об оплате за 3 дня (переводит статус в waiting_for_payment)
        notif_count = await process_payment_notifications(base_date)
        assert notif_count >= 1

        updated_st = await db.get_crm_student(st["id"])
        assert updated_st["status"] == "waiting_for_payment"
