from pydantic import BaseModel
from typing import List, Optional


class CalendarBase(BaseModel):
    name: str
    max_events_per_hour: int


class CalendarResponse(CalendarBase):
    id: str

    class Config:
        from_attributes = True


class CreateCalendar(BaseModel):
    id: str
    name: str
    max_events_per_hour: int = 1


class CreateUser(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    full_name: str
    phone: Optional[str] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    is_independent: bool = False


class CalendarMembers(BaseModel):
    telegram_id: int
    calendar_id: str
    role: str = "trainer"  # 'admin' | 'trainer'
    can_edit_schedule: bool = True
    employment_type: str = "club"  # 'club' | 'rent'


class MigrateGoogleCalendarRequest(BaseModel):
    google_calendar_id: str


