import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Query

from database.database import db
from config.crm import (
    AttendanceUpdateRequest,
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberRoleUpdateRequest,
    PollRuleItem,
    StudentCreateRequest,
    StudentPanelAttendanceRequest,
    StudentPanelSettingsRequest,
    StudentPaymentUpdateRequest,
    StudentUpdateRequest,
    TrainerRentPaymentRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/crm", tags=["CRM"])


async def notify_trainer_student_declined(
    student_name: str,
    event_date: str,
    single_event_id: Optional[int] = None,
    recurring_event_id: Optional[int] = None,
):
    """Отправляет уведомление тренеру в Telegram, если ученик отметил 'Не приду'."""
    try:
        from telegram_bot.bot_app import bot

        ev = None
        if single_event_id is not None:
            ev = await db.get_single_event(single_event_id)
        elif recurring_event_id is not None:
            ev = await db.get_recurring_event(recurring_event_id)

        if not ev:
            return

        trainer_tg_id = ev.get("trainer_id") or ev.get("created_by")
        if not trainer_tg_id:
            return

        title = ev.get("title", "Тренировка")
        time_str = (
            ev["start_datetime"][11:16]
            if "start_datetime" in ev and ev["start_datetime"]
            else (ev.get("start_time", "")[:5])
        )
        is_group = ev.get("event_type") == "group" or ev.get("group_id") is not None
        prefix = "⚠️ [Группа]" if is_group else "⚠️"
        msg = (
            f"{prefix} Ученик <b>{student_name}</b> отметил, что <b>не придет</b> "
            f"на тренировку «{title}» ({event_date} в {time_str})."
        )
        await bot.send_message(chat_id=trainer_tg_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to send declined notification to trainer: %s", e)


# ─── 1. УЧЕНИКИ И ПОИСК ──────────────────────────────────────────────────────

@router.get("/students/search")
async def search_students(
    q: str = Query("", description="Текст поиска (имя или номер телефона)"),
    calendar_id: Optional[str] = Query(None, description="ID календаря (опционально)"),
    limit: int = Query(30, ge=1, le=100),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Умный поиск учеников по имени или номеру телефона для автодополнения на фронте.
    Возвращает только тех учеников, к которым у запрашивающего есть доступ.
    """
    return await db.search_crm_students(
        query=q,
        requester_telegram_id=telegram_id,
        calendar_id=calendar_id,
        limit=limit,
    )


@router.post("/students", status_code=201)
async def create_student(
    body: StudentCreateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Создать ученика в CRM.
    Обязательное поле только full_name.
    Автоматически генерирует invite_token и возвращает готовую диплинк-ссылку invite_link.
    """
    student = await db.create_crm_student(
        full_name=body.full_name,
        phone=body.phone,
        status=body.status,
        next_payment_date=body.next_payment_date.isoformat() if body.next_payment_date else None,
        payment_amount=body.payment_amount,
        trainer_id=body.trainer_id or telegram_id,
        calendar_id=body.calendar_id,
        notes=body.notes,
        coach_comment=body.coach_comment,
        skill_level=body.skill_level,
        skills=body.skills,
        polls_enabled=body.polls_enabled,
        created_by=telegram_id,
    )
    if not student:
        raise HTTPException(status_code=500, detail="Failed to create student")
    return student


@router.get("/students/{student_id}")
async def get_student(
    student_id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    student = await db.get_crm_student(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await db.can_access_student_finances(telegram_id, student):
        raise HTTPException(status_code=403, detail="Access denied to this student")
    return student


@router.patch("/students/{student_id}")
async def update_student(
    student_id: int,
    body: StudentUpdateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Обновить данные ученика (включая навыки, заметку тренера, дату следующей оплаты)."""
    student = await db.get_crm_student(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await db.can_access_student_finances(telegram_id, student):
        raise HTTPException(status_code=403, detail="Access denied to this student")

    fs = body.model_fields_set
    next_pay_arg = (
        (body.next_payment_date.isoformat() if body.next_payment_date else None)
        if "next_payment_date" in fs
        else ...
    )

    updated = await db.update_crm_student(
        student_id=student_id,
        full_name=body.full_name,
        phone=body.phone if "phone" in fs else ...,
        status=body.status if "status" in fs else ...,
        next_payment_date=next_pay_arg,
        payment_amount=body.payment_amount if "payment_amount" in fs else ...,
        trainer_id=body.trainer_id if "trainer_id" in fs else ...,
        calendar_id=body.calendar_id if "calendar_id" in fs else ...,
        notes=body.notes if "notes" in fs else ...,
        coach_comment=body.coach_comment if "coach_comment" in fs else ...,
        skill_level=body.skill_level if "skill_level" in fs else ...,
        skills=body.skills if "skills" in fs else ...,
        polls_enabled=body.polls_enabled,
    )
    return updated


@router.delete("/students/{student_id}")
async def delete_student(
    student_id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    student = await db.get_crm_student(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await db.can_access_student_finances(telegram_id, student):
        raise HTTPException(status_code=403, detail="Access denied to this student")

    await db.delete_crm_student(student_id)
    return {"message": "Student deleted successfully", "id": student_id}


# ─── 2. ГРУППЫ ───────────────────────────────────────────────────────────────

@router.get("/groups")
async def list_groups(
    calendar_id: Optional[str] = Query(None),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Список групп с участниками, доступных тренеру/админу."""
    return await db.list_crm_groups(requester_telegram_id=telegram_id, calendar_id=calendar_id)


@router.post("/groups", status_code=201)
async def create_group(
    body: GroupCreateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Создать объект группы с готовым списком учеников."""
    group = await db.create_crm_group(
        name=body.name,
        trainer_id=body.trainer_id or telegram_id,
        calendar_id=body.calendar_id,
        notes=body.notes,
        student_ids=body.student_ids,
    )
    if not group:
        raise HTTPException(status_code=500, detail="Failed to create group")
    return group


@router.get("/groups/{group_id}")
async def get_group(
    group_id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    group = await db.get_crm_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: int,
    body: GroupUpdateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    group = await db.get_crm_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    fs = body.model_fields_set
    updated = await db.update_crm_group(
        group_id=group_id,
        name=body.name,
        trainer_id=body.trainer_id if "trainer_id" in fs else ...,
        calendar_id=body.calendar_id if "calendar_id" in fs else ...,
        notes=body.notes if "notes" in fs else ...,
        student_ids=body.student_ids,
    )
    return updated


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    group = await db.get_crm_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete_crm_group(group_id)
    return {"message": "Group deleted successfully", "id": group_id}


# ─── 3. ОПЛАТЫ И ФИНАНСЫ (С ФИНАНСОВОЙ ПРИВАТНОСТЬЮ) ─────────────────────────

@router.get("/payments/upcoming")
async def get_upcoming_payments(
    calendar_id: Optional[str] = Query(None),
    days_ahead: int = Query(7, ge=0, le=60, description="За сколько дней вперед показывать оплаты"),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Возвращает учеников, у которых оплата должна быть в течение недели (или просрочена / waiting_for_payment).
    Доходы и ученики тренеров-арендаторов ('rent') скрыты от владельца корта (админа).
    """
    return await db.get_upcoming_student_payments(
        requester_telegram_id=telegram_id,
        calendar_id=calendar_id,
        days_ahead=days_ahead,
    )


@router.patch("/students/{student_id}/payment")
async def update_student_payment(
    student_id: int,
    body: StudentPaymentUpdateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Отметить проведение оплаты учеником или изменить/убрать дату следующей оплаты.
    Если передать next_payment_date: null — дата оплаты убирается (например, ученик больше не ходит).
    """
    student = await db.get_crm_student(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    if not await db.can_access_student_finances(telegram_id, student):
        raise HTTPException(status_code=403, detail="Access denied to this student's payments")

    updated = await db.record_student_payment(
        student_id=student_id,
        payment_made=body.payment_made,
        next_payment_date=body.next_payment_date.isoformat() if body.next_payment_date else None,
        amount=body.amount,
        status=body.status,
        comment=body.comment,
        paid_at=body.paid_at.isoformat() if body.paid_at else None,
    )
    return updated


@router.get("/trainers/me/earnings")
async def get_my_trainer_earnings(
    from_date: date = Query(..., description="Начало периода (YYYY-MM-DD)"),
    to_date: date = Query(..., description="Конец периода (YYYY-MM-DD)"),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Личная статистика заработка тренера со своих учеников.
    Видна ТОЛЬКО самому тренеру (по X-Telegram-Id).
    """
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
    return await db.get_trainer_personal_earnings(
        trainer_id=telegram_id,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )


@router.post("/calendars/{calendar_id}/trainer-rent-payments", status_code=201)
async def create_trainer_rent_payment(
    calendar_id: str,
    body: TrainerRentPaymentRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Зафиксировать сумму, которую тренер-арендатор заплатил клубу/владельцу корта.
    Доступно только для роли 'admin' календаря.
    """
    role = await db.get_member_role(calendar_id, telegram_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Access denied: admin role required")

    return await db.record_trainer_rent_payment(
        calendar_id=calendar_id,
        trainer_id=body.trainer_id,
        amount=body.amount,
        paid_at=body.paid_at.isoformat() if body.paid_at else None,
        next_payment_date=body.next_payment_date.isoformat() if body.next_payment_date else None,
        comment=body.comment,
        created_by=telegram_id,
    )


@router.get("/calendars/{calendar_id}/trainer-rent-payments")
async def list_trainer_rent_payments(
    calendar_id: str,
    from_date: date = Query(...),
    to_date: date = Query(...),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Посмотреть, сколько владелец корта (admin) заработал со взносов тренеров-арендаторов за период.
    """
    role = await db.get_member_role(calendar_id, telegram_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Access denied: admin role required")
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")

    return await db.get_trainer_rent_payments(
        calendar_id=calendar_id,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
    )


# ─── 4. УПРАВЛЕНИЕ ПРАВАМИ ТРЕНЕРОВ В КАЛЕНДАРЕ ──────────────────────────────

@router.patch("/calendars/{calendar_id}/members/{member_telegram_id}")
async def update_calendar_member_permissions(
    calendar_id: str,
    member_telegram_id: int,
    body: MemberRoleUpdateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Позволяет админу календаря повысить тренера до админа, разрешить/запретить менять расписание
    (can_edit_schedule) или указать тип работы ('club' vs 'rent').
    Тренер НЕ может менять роли или повышать себя сам.
    """
    requester_role = await db.get_member_role(calendar_id, telegram_id)
    if requester_role != "admin":
        raise HTTPException(status_code=403, detail="Access denied: only admin can update member permissions")

    current = await db.get_member_info(calendar_id, member_telegram_id)
    new_role = body.role if body.role is not None else (current["role"] if current else "trainer")
    new_can_edit = (
        body.can_edit_schedule
        if body.can_edit_schedule is not None
        else (current["can_edit_schedule"] if current else True)
    )
    new_emp_type = (
        body.employment_type
        if body.employment_type is not None
        else (current["employment_type"] if current else "club")
    )

    updated = await db.add_member(
        calendar_id=calendar_id,
        telegram_id=member_telegram_id,
        role=new_role,
        can_edit_schedule=new_can_edit,
        employment_type=new_emp_type,
    )
    return updated


# ─── 5. ПОСЕЩАЕМОСТЬ И ПРАВИЛА ОТПРАВКИ ОПРОСОВ ──────────────────────────────

@router.get("/poll-rules")
async def get_poll_rules(telegram_id: int = Header(..., alias="X-Telegram-Id")):
    """Возвращает правила времени отправки опросов в зависимости от часа начала тренировки."""
    return await db.get_poll_schedule_rules()


@router.put("/poll-rules")
async def update_poll_rules(
    rules: List[PollRuleItem],
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Обновляет правила времени отправки опросов в БД (например: 7 утра -> 20:00 пред. дня, 16:00 -> 12:00)."""
    updated = []
    for r in rules:
        res = await db.upsert_poll_schedule_rule(r.event_hour, r.send_time, r.days_before)
        updated.append(res)
    return updated


@router.get("/attendance")
async def get_attendance(
    event_date: date = Query(...),
    single_event_id: Optional[int] = Query(None),
    recurring_event_id: Optional[int] = Query(None),
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Список ответов/посещаемости учеников на конкретную тренировку."""
    if single_event_id is None and recurring_event_id is None:
        raise HTTPException(status_code=400, detail="Provide single_event_id or recurring_event_id")
    return await db.get_event_attendance(
        event_date=event_date.isoformat(),
        single_event_id=single_event_id,
        recurring_event_id=recurring_event_id,
    )


@router.post("/attendance")
async def set_attendance(
    body: AttendanceUpdateRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Отметить посещаемость ученика тренером или админом."""
    return await db.upsert_attendance(
        student_id=body.student_id,
        event_date=body.event_date.isoformat(),
        status=body.status,
        single_event_id=body.single_event_id,
        recurring_event_id=body.recurring_event_id,
    )


# ─── 6. ПАНЕЛЬ УЧЕНИКА (В БОТЕ / MINI APP) ───────────────────────────────────

@router.get("/student-panel")
async def get_student_panel(
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Панель ученика. Обязательная проверка по X-Telegram-Id.
    - Если зашел обычный пользователь (не ученик) -> возвращает is_student=False + промо-экран и витрину тренеров.
    - Если зашел ученик -> возвращает is_student=True и все 7 блоков Панели ученика.
    """
    return await db.get_student_panel_data(telegram_id)


@router.patch("/student-panel/settings")
async def update_student_panel_settings(
    body: StudentPanelSettingsRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Позволяет ученику включить или отключить для себя утренние опросы в боте."""
    student = await db.get_crm_student_by_telegram_id(telegram_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    updated = await db.update_crm_student(
        student_id=student["id"],
        polls_enabled=body.polls_enabled,
    )
    return {
        "student_id": updated["id"],
        "polls_enabled": updated["polls_enabled"],
    }


@router.post("/student-panel/attendance")
async def student_panel_mark_attendance(
    body: StudentPanelAttendanceRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """
    Позволяет ученику прямо из Панели ученика отметить 'Приду' (confirmed) или 'Не приду' (declined).
    Если ученик нажал 'Не приду' — бот сразу уведомляет тренера.
    """
    student = await db.get_crm_student_by_telegram_id(telegram_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found")

    date_str = body.event_date.isoformat()
    att = await db.upsert_attendance(
        student_id=student["id"],
        event_date=date_str,
        status=body.status,
        single_event_id=body.single_event_id,
        recurring_event_id=body.recurring_event_id,
    )

    if body.status == "declined":
        await notify_trainer_student_declined(
            student_name=student["full_name"],
            event_date=date_str,
            single_event_id=body.single_event_id,
            recurring_event_id=body.recurring_event_id,
        )

    return att
