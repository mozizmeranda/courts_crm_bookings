from pydantic import BaseModel, model_validator, Field
from typing import List, Optional, Dict, Any
from datetime import datetime, time, date
from datetime import time as time_type


class InlineStudentCreate(BaseModel):
    full_name: str = Field(..., min_length=1, description="Имя ученика (обязательное поле)")
    phone: Optional[str] = None
    status: Optional[str] = None
    next_payment_date: Optional[str] = None
    payment_amount: Optional[float] = None
    notes: Optional[str] = None
    coach_comment: Optional[str] = None
    skill_level: Optional[str] = None
    skills: Optional[Dict[str, Any]] = None


# Request Schemas
class SingleEventCreate(BaseModel):
    title: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    event_type: Optional[str] = None  # None (бронь), 'individual', 'group'
    student_id: Optional[int] = None
    new_student: Optional[InlineStudentCreate] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: bool = False
    poll_send_time: Optional[str] = None
    poll_days_before: Optional[int] = None

    @model_validator(mode='after')
    def check_dates(self) -> 'SingleEventCreate':
        if self.start_datetime.tzinfo is not None:
            self.start_datetime = self.start_datetime.replace(tzinfo=None)
        if self.end_datetime.tzinfo is not None:
            self.end_datetime = self.end_datetime.replace(tzinfo=None)

        if self.start_datetime >= self.end_datetime:
            raise ValueError("start_datetime must be before end_datetime")
        if self.status not in ("confirmed", "pending_payment", "cancelled"):
            raise ValueError("Invalid status")
        if self.event_type not in (None, "individual", "group"):
            raise ValueError("event_type must be null, 'individual', or 'group'")
        return self


class SingleEventUpdate(BaseModel):
    title: Optional[str] = None
    start_datetime: Optional[datetime] = None
    end_datetime: Optional[datetime] = None
    event_type: Optional[str] = None
    student_id: Optional[int] = None
    new_student: Optional[InlineStudentCreate] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: Optional[bool] = None
    poll_send_time: Optional[str] = None
    poll_days_before: Optional[int] = None

    @model_validator(mode='after')
    def check_dates(self) -> 'SingleEventUpdate':
        if self.start_datetime and self.start_datetime.tzinfo is not None:
            self.start_datetime = self.start_datetime.replace(tzinfo=None)
        if self.end_datetime and self.end_datetime.tzinfo is not None:
            self.end_datetime = self.end_datetime.replace(tzinfo=None)

        if self.start_datetime and self.end_datetime and self.start_datetime >= self.end_datetime:
            raise ValueError("start_datetime must be before end_datetime")
        if self.event_type not in (None, "individual", "group"):
            raise ValueError("event_type must be null, 'individual', or 'group'")
        return self


class RecurringEventUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255, description="Новое название повторяющегося события")
    event_type: Optional[str] = None
    student_id: Optional[int] = None
    new_student: Optional[InlineStudentCreate] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: Optional[bool] = None
    poll_send_time: Optional[str] = None
    poll_days_before: Optional[int] = None


class RecurringEventCreate(BaseModel):
    title: str
    days_of_week: List[int]
    start_time: time
    end_time: time
    event_type: Optional[str] = None  # None (бронь), 'individual', 'group'
    student_id: Optional[int] = None
    new_student: Optional[InlineStudentCreate] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: bool = False
    poll_send_time: Optional[str] = None
    poll_days_before: Optional[int] = None

    @model_validator(mode='after')
    def check_times(self) -> 'RecurringEventCreate':
        
        midnight = time_type(0, 0, 0)
        # end_time == 00:00:00 означает полночь следующего дня — это валидно для слота 23:00-00:00
        if self.end_time != midnight and self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        for day in self.days_of_week:
            if day < 0 or day > 6:
                raise ValueError("days_of_week must be between 0 (Monday) and 6 (Sunday)")
        if self.event_type not in (None, "individual", "group"):
            raise ValueError("event_type must be null, 'individual', or 'group'")
        return self


class CancelInstanceRequest(BaseModel):
    date: date


# Response Schemas
class SingleEventResponse(BaseModel):
    id: int
    calendar_id: str
    created_by: Optional[int]
    title: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    recurring_event_id: Optional[int]
    event_type: Optional[str] = None
    student_id: Optional[int] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: bool = False
    poll_send_time: Optional[str] = None
    poll_days_before: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class RecurringEventResponse(BaseModel):
    id: int
    calendar_id: str
    title: str
    day_of_week: int
    start_time: time
    end_time: time
    event_type: Optional[str] = None
    student_id: Optional[int] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: bool = False
    poll_send_time: Optional[str] = None
    poll_days_before: int = 0

    class Config:
        from_attributes = True



class GridEventResponse(BaseModel):
    id: int
    type: str  # "single" or "recurring_instance"
    title: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    recurring_event_id: Optional[int]
    event_type: Optional[str] = None
    student_id: Optional[int] = None
    group_id: Optional[int] = None
    trainer_id: Optional[int] = None
    send_poll: bool = False
    poll_send_time: Optional[str] = None
    poll_days_before: int = 0

