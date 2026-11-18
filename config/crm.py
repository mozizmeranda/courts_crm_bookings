from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Dict, Any
from datetime import date


class StudentCreateRequest(BaseModel):
    full_name: str = Field(..., min_length=1, description="ФИО / Имя ученика (обязательное поле)")
    phone: Optional[str] = None
    status: Optional[str] = None  # None | 'active' | 'waiting_for_payment' | 'archived'
    next_payment_date: Optional[date] = None
    payment_amount: Optional[float] = None
    trainer_id: Optional[int] = None
    calendar_id: Optional[str] = None
    notes: Optional[str] = None
    coach_comment: Optional[str] = None
    skill_level: Optional[str] = None
    skills: Optional[Dict[str, Any]] = None
    polls_enabled: bool = True

    @model_validator(mode="after")
    def validate_status(self) -> "StudentCreateRequest":
        if self.status not in (None, "active", "waiting_for_payment", "archived"):
            raise ValueError("status must be null, 'active', 'waiting_for_payment', or 'archived'")
        return self


class StudentUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1)
    phone: Optional[str] = None
    status: Optional[str] = None
    next_payment_date: Optional[date] = None
    payment_amount: Optional[float] = None
    trainer_id: Optional[int] = None
    calendar_id: Optional[str] = None
    notes: Optional[str] = None
    coach_comment: Optional[str] = None
    skill_level: Optional[str] = None
    skills: Optional[Dict[str, Any]] = None
    polls_enabled: Optional[bool] = None

    @model_validator(mode="after")
    def validate_status(self) -> "StudentUpdateRequest":
        if "status" in self.model_fields_set and self.status not in (
            None,
            "active",
            "waiting_for_payment",
            "archived",
        ):
            raise ValueError("status must be null, 'active', 'waiting_for_payment', or 'archived'")
        return self


class StudentPaymentUpdateRequest(BaseModel):
    payment_made: bool = Field(
        False,
        description="True если ученик провел оплату (запишет платеж в историю и обновит дату)",
    )
    next_payment_date: Optional[date] = Field(
        None,
        description="Новая дата следующей оплаты (или null, чтобы убрать дату оплаты)",
    )
    amount: Optional[float] = None
    status: Optional[str] = None
    comment: Optional[str] = None
    paid_at: Optional[date] = None


class GroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    trainer_id: Optional[int] = None
    calendar_id: Optional[str] = None
    notes: Optional[str] = None
    student_ids: List[int] = Field(default_factory=list)


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    trainer_id: Optional[int] = None
    calendar_id: Optional[str] = None
    notes: Optional[str] = None
    student_ids: Optional[List[int]] = None


class TrainerRentPaymentRequest(BaseModel):
    trainer_id: int
    amount: float = Field(..., gt=0, description="Сумма, которую тренер дал клубу")
    paid_at: Optional[date] = None
    next_payment_date: Optional[date] = None
    comment: Optional[str] = None


class MemberRoleUpdateRequest(BaseModel):
    role: Optional[str] = None  # 'admin' | 'trainer'
    can_edit_schedule: Optional[bool] = None
    employment_type: Optional[str] = None  # 'club' | 'rent'

    @model_validator(mode="after")
    def validate_fields(self) -> "MemberRoleUpdateRequest":
        if self.role is not None and self.role not in ("admin", "trainer"):
            raise ValueError("role must be 'admin' or 'trainer'")
        if self.employment_type is not None and self.employment_type not in ("club", "rent"):
            raise ValueError("employment_type must be 'club' or 'rent'")
        return self


class PollRuleItem(BaseModel):
    event_hour: int = Field(..., ge=0, le=23)
    send_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="Время отправки опроса HH:MM")
    days_before: int = Field(0, ge=0, le=7, description="0 = в день тренировки, 1 = за день до тренировки")


class AttendanceUpdateRequest(BaseModel):
    student_id: int
    event_date: date
    status: str = Field(..., description="'planned' | 'confirmed' | 'declined' | 'attended' | 'missed'")
    single_event_id: Optional[int] = None
    recurring_event_id: Optional[int] = None

    @model_validator(mode="after")
    def validate_attendance(self) -> "AttendanceUpdateRequest":
        if self.status not in ("planned", "confirmed", "declined", "attended", "missed"):
            raise ValueError("Invalid attendance status")
        if self.single_event_id is None and self.recurring_event_id is None:
            raise ValueError("Either single_event_id or recurring_event_id must be provided")
        return self


class StudentPanelSettingsRequest(BaseModel):
    polls_enabled: bool


class StudentPanelAttendanceRequest(BaseModel):
    event_date: date
    status: str = Field(..., description="'confirmed' (приду) или 'declined' (не приду)")
    single_event_id: Optional[int] = None
    recurring_event_id: Optional[int] = None

    @model_validator(mode="after")
    def validate_choice(self) -> "StudentPanelAttendanceRequest":
        if self.status not in ("confirmed", "declined"):
            raise ValueError("status must be 'confirmed' or 'declined'")
        if self.single_event_id is None and self.recurring_event_id is None:
            raise ValueError("Either single_event_id or recurring_event_id must be provided")
        return self
