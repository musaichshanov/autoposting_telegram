# app/utils.py
from datetime import datetime, time, timedelta
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

def compute_next_run_from_weekday_and_time(start_dt: datetime, weekday:int, t: time):
    days_ahead = (weekday - start_dt.weekday()) % 7
    candidate_date = (start_dt + timedelta(days=days_ahead)).date()
    candidate_dt = datetime.combine(candidate_date, t)
    if candidate_dt <= start_dt:
        candidate_dt = candidate_dt + timedelta(days=7)
    return candidate_dt

def add_month_preserve_weekday(dt: datetime, weekday:int, t: time):
    plus_one = dt + relativedelta(months=+1)
    base = datetime.combine(plus_one.date(), t)
    for shift in range(0, 7):
        cand = base + timedelta(days=shift)
        if cand.weekday() == weekday:
            return cand
    return datetime.combine(plus_one.date(), t)

def compute_next_run_cycle(now: datetime, cycle_weeks: int, cycle_start: datetime, week_in_cycle: int, weekday: int, t: time):
    if cycle_weeks <= 0:
        cycle_weeks = 1
    weeks_from_start = int((now.date() - cycle_start.date()).days // 7)
    for add_weeks in range(0, cycle_weeks * 2):
        target_week_index = (weeks_from_start + add_weeks)
        if target_week_index % cycle_weeks == week_in_cycle:
            base = cycle_start + timedelta(weeks=target_week_index)
            days_ahead = (weekday - base.weekday()) % 7
            candidate = datetime.combine((base + timedelta(days=days_ahead)).date(), t)
            if candidate > now:
                return candidate
    base = cycle_start + timedelta(weeks=((weeks_from_start + cycle_weeks) // cycle_weeks) * cycle_weeks)
    days_ahead = (weekday - base.weekday()) % 7
    return datetime.combine((base + timedelta(days=days_ahead)).date(), t)

def compute_next_run_cycle_tz(now_utc: datetime, cycle_weeks: int, cycle_start_utc: datetime, week_in_cycle: int, weekday: int, t_local: time, tz_name: str = "Europe/Moscow") -> datetime:
    tz = ZoneInfo(tz_name)
    # привести now и start к локальному времени
    now_local = now_utc.astimezone(tz) if now_utc.tzinfo else now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    start_local = cycle_start_utc.astimezone(tz) if cycle_start_utc.tzinfo else cycle_start_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    # вычислить локальный next_run существующей логикой
    local_next = compute_next_run_cycle(now_local.replace(tzinfo=None), cycle_weeks, start_local.replace(tzinfo=None), week_in_cycle, weekday, t_local)
    # сделать aware в локальной зоне и перевести в UTC
    local_next_aware = local_next.replace(tzinfo=tz)
    return local_next_aware.astimezone(ZoneInfo("UTC"))
