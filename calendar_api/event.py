# routers/events.py
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from database.database import db
from calendar_api.helpers import require_calendar_access, require_schedule_edit_permission, _check_capacity
from config.event import CancelInstanceRequest, SingleEventUpdate, RecurringEventUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Events"])


# ─── endpoints ────────────────────────────────────────────────────────────────

@router.delete("/single-events/{id}")
async def delete_single_event(
    id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Удалить одиночное событие."""
    event = await db.get_single_event(id)
    if not event:
        raise HTTPException(status_code=404, detail="Single event not found")

    if await db.check_member_access(event["calendar_id"], telegram_id):
        await require_schedule_edit_permission(event["calendar_id"], telegram_id)

    deleted = await db.delete_single_event(id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete event")

    return {"message": "Single event successfully deleted", "id": id}


@router.patch("/single-events/{id}")
async def update_single_event(
    id: int,
    event_update: SingleEventUpdate,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Обновить время, название или CRM-данные (ученик, группа, опрос) одиночного события."""
    event = await db.get_single_event(id)
    if not event:
        raise HTTPException(status_code=404, detail="Single event not found")

    calendar = await require_calendar_access(event["calendar_id"], telegram_id)
    member_info = await db.get_member_info(event["calendar_id"], telegram_id)
    can_edit_schedule = member_info["can_edit_schedule"] if member_info else True

    new_start_str = (
        event_update.start_datetime.strftime("%Y-%m-%d %H:%M:%S")
        if event_update.start_datetime
        else event["start_datetime"]
    )
    new_end_str = (
        event_update.end_datetime.strftime("%Y-%m-%d %H:%M:%S")
        if event_update.end_datetime
        else event["end_datetime"]
    )

    time_changed = (new_start_str != event["start_datetime"]) or (new_end_str != event["end_datetime"])

    if not can_edit_schedule:
        if time_changed:
            raise HTTPException(
                status_code=403,
                detail="Access denied: you do not have permission to change the event schedule time",
            )
        if event.get("trainer_id") not in (None, telegram_id) and event.get("created_by") not in (None, telegram_id):
            raise HTTPException(
                status_code=403,
                detail="Access denied: you can only modify CRM data for your own slots",
            )

    if time_changed:
        await _check_capacity(
            event["calendar_id"], new_start_str, new_end_str,
            calendar["max_events_per_hour"], exclude_event_id=id
        )

    fields_set = event_update.model_fields_set
    student_id_arg = event_update.student_id if "student_id" in fields_set else ...
    created_student = None

    if event_update.new_student:
        ns = event_update.new_student
        eff_trainer = event_update.trainer_id or event.get("trainer_id") or telegram_id
        created_student = await db.create_crm_student(
            full_name=ns.full_name,
            phone=ns.phone,
            status=ns.status,
            next_payment_date=ns.next_payment_date,
            payment_amount=ns.payment_amount,
            trainer_id=eff_trainer,
            calendar_id=event["calendar_id"],
            notes=ns.notes,
            coach_comment=ns.coach_comment,
            skill_level=ns.skill_level,
            skills=ns.skills,
            created_by=telegram_id,
        )
        if created_student:
            student_id_arg = created_student["id"]

    event_type_arg = event_update.event_type if "event_type" in fields_set else ...
    if event_type_arg is ... and created_student is not None and not event.get("event_type"):
        event_type_arg = "individual"

    updated = await db.update_single_event(
        event_id=id,
        start_datetime=new_start_str,
        end_datetime=new_end_str,
        title=event_update.title,
        event_type=event_type_arg,
        student_id=student_id_arg,
        group_id=event_update.group_id if "group_id" in fields_set else ...,
        trainer_id=event_update.trainer_id if "trainer_id" in fields_set else ...,
        send_poll=event_update.send_poll,
        poll_send_time=event_update.poll_send_time if "poll_send_time" in fields_set else ...,
        poll_days_before=event_update.poll_days_before,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update event")
    if created_student:
        updated["created_student"] = created_student
    return updated


@router.patch("/recurring-events/{id}")
async def update_recurring_event(
    id: int,
    event_update: RecurringEventUpdate,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Обновить название или CRM-данные (ученик, группа, опрос) повторяющегося события."""
    event = await db.get_recurring_event(id)
    if not event:
        raise HTTPException(status_code=404, detail="Recurring event not found")

    await require_calendar_access(event["calendar_id"], telegram_id)
    member_info = await db.get_member_info(event["calendar_id"], telegram_id)
    if member_info and not member_info["can_edit_schedule"]:
        if event.get("trainer_id") not in (None, telegram_id) and event.get("created_by") not in (None, telegram_id):
            raise HTTPException(
                status_code=403,
                detail="Access denied: you can only modify CRM data for your own slots",
            )

    fields_set = event_update.model_fields_set
    student_id_arg = event_update.student_id if "student_id" in fields_set else ...
    created_student = None

    if event_update.new_student:
        ns = event_update.new_student
        eff_trainer = event_update.trainer_id or event.get("trainer_id") or telegram_id
        created_student = await db.create_crm_student(
            full_name=ns.full_name,
            phone=ns.phone,
            status=ns.status,
            next_payment_date=ns.next_payment_date,
            payment_amount=ns.payment_amount,
            trainer_id=eff_trainer,
            calendar_id=event["calendar_id"],
            notes=ns.notes,
            coach_comment=ns.coach_comment,
            skill_level=ns.skill_level,
            skills=ns.skills,
            created_by=telegram_id,
        )
        if created_student:
            student_id_arg = created_student["id"]

    event_type_arg = event_update.event_type if "event_type" in fields_set else ...
    if event_type_arg is ... and created_student is not None and not event.get("event_type"):
        event_type_arg = "individual"

    updated = await db.update_recurring_event_title(
        event_id=id,
        title=event_update.title,
        event_type=event_type_arg,
        student_id=student_id_arg,
        group_id=event_update.group_id if "group_id" in fields_set else ...,
        trainer_id=event_update.trainer_id if "trainer_id" in fields_set else ...,
        send_poll=event_update.send_poll,
        poll_send_time=event_update.poll_send_time if "poll_send_time" in fields_set else ...,
        poll_days_before=event_update.poll_days_before,
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update recurring event")
    if created_student:
        updated["created_student"] = created_student
    return updated


@router.delete("/recurring-events/{id}")
async def delete_recurring_event(
    id: int,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Удалить повторяющееся событие (и все его связанные экземпляры)."""
    event = await db.get_recurring_event(id)
    if not event:
        raise HTTPException(status_code=404, detail="Recurring event not found")

    if await db.check_member_access(event["calendar_id"], telegram_id):
        await require_schedule_edit_permission(event["calendar_id"], telegram_id)

    deleted = await db.delete_recurring_event(id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete recurring event")

    return {"message": "Recurring event series successfully deleted", "id": id}


@router.post("/recurring-events/{id}/cancel-instance")
async def cancel_recurring_instance(
    id: int,
    request: CancelInstanceRequest,
    telegram_id: int = Header(..., alias="X-Telegram-Id"),
):
    """Отменить конкретный экземпляр повторяющегося события."""
    rec_event = await db.get_recurring_event(id)
    if not rec_event:
        raise HTTPException(status_code=404, detail="Recurring event not found")

    if await db.check_member_access(rec_event["calendar_id"], telegram_id):
        await require_schedule_edit_permission(rec_event["calendar_id"], telegram_id)

    cancel_date_str = request.date.strftime("%Y-%m-%d")

    result = await db.cancel_recurring_instance(
        recurring_event_id=id,
        cancel_date=cancel_date_str,
        created_by=telegram_id,
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail=f"Date {request.date} does not match the recurring event's day of the week"
        )

    return {
        "message": f"Event instance successfully cancelled for {request.date}",
        "recurring_event_id": id,
        "cancelled_date": request.date.isoformat(),
    }
