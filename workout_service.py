"""
FITCORE — Workout Service (Backend)
Python + FastAPI

Responsibilities:
  - Exercise library CRUD
  - Workout plan management (PPL, 5/3/1, custom)
  - Workout session logging & validation
  - Strength progression tracking & PRs
  - Volume calculations & analytics
  - Background tasks: PR detection, streak updates, badge awards
  - Celery async tasks for heavy analytics
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from sqlalchemy import (
    Column, Date, DateTime, Float, ForeignKey,
    Integer, String, Text, Boolean, Enum as SAEnum,
    func, select, desc,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, relationship, selectinload

# ─────────────────────────────────────────────
#  CONFIG & DB
# ─────────────────────────────────────────────
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://fitcore:fitcore@localhost:5432/fitcore"
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)
Base   = declarative_base()


async def get_db():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session


# ─────────────────────────────────────────────
#  DATABASE MODELS
# ─────────────────────────────────────────────
class MuscleGroup(str, Enum):
    CHEST    = "chest"
    BACK     = "back"
    SHOULDERS= "shoulders"
    BICEPS   = "biceps"
    TRICEPS  = "triceps"
    FOREARMS = "forearms"
    QUADS    = "quads"
    HAMSTRINGS="hamstrings"
    GLUTES   = "glutes"
    CALVES   = "calves"
    ABS      = "abs"
    OBLIQUES = "obliques"

class MovementType(str, Enum):
    PUSH  = "push"
    PULL  = "pull"
    LEGS  = "legs"
    CORE  = "core"
    CARDIO= "cardio"

class ExerciseDB(Base):
    __tablename__ = "exercises"
    id            = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name          = Column(String(120), nullable=False, unique=True)
    slug          = Column(String(120), nullable=False, unique=True)
    primary_muscle= Column(SAEnum(MuscleGroup), nullable=False)
    secondary_muscles = Column(String(200), default="")   # comma-separated
    movement_type = Column(SAEnum(MovementType), nullable=False)
    equipment     = Column(String(80), default="barbell")
    instructions  = Column(Text, default="")
    is_compound   = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

class WorkoutPlanDB(Base):
    __tablename__ = "workout_plans"
    id         = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = Column(PGUUID(as_uuid=True), nullable=False, index=True)
    name       = Column(String(120), nullable=False)
    description= Column(Text, default="")
    days_per_week = Column(Integer, nullable=False)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    days       = relationship("WorkoutDayDB", back_populates="plan", lazy="select")

class WorkoutDayDB(Base):
    __tablename__ = "workout_days"
    id         = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id    = Column(PGUUID(as_uuid=True), ForeignKey("workout_plans.id"), nullable=False)
    day_number = Column(Integer, nullable=False)   # 1-7
    name       = Column(String(80), nullable=False) # e.g. "Push A"
    focus      = Column(SAEnum(MovementType), nullable=False)
    plan       = relationship("WorkoutPlanDB", back_populates="days")
    exercises  = relationship("PlanExerciseDB", back_populates="day", lazy="select")

class PlanExerciseDB(Base):
    __tablename__ = "plan_exercises"
    id          = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day_id      = Column(PGUUID(as_uuid=True), ForeignKey("workout_days.id"), nullable=False)
    exercise_id = Column(PGUUID(as_uuid=True), ForeignKey("exercises.id"), nullable=False)
    order_idx   = Column(Integer, nullable=False)
    sets        = Column(Integer, nullable=False)
    reps_target = Column(String(20), nullable=False)  # e.g. "5", "8-12", "AMRAP"
    rpe_target  = Column(Float, nullable=True)        # Rate of Perceived Exertion
    rest_seconds= Column(Integer, default=90)
    day         = relationship("WorkoutDayDB", back_populates="exercises")

class WorkoutSessionDB(Base):
    __tablename__ = "workout_sessions"
    id              = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id         = Column(PGUUID(as_uuid=True), nullable=False, index=True)
    plan_id         = Column(PGUUID(as_uuid=True), nullable=True)
    day_id          = Column(PGUUID(as_uuid=True), nullable=True)
    started_at      = Column(DateTime, nullable=False)
    ended_at        = Column(DateTime, nullable=True)
    duration_seconds= Column(Integer, nullable=True)
    total_volume_lbs= Column(Float, default=0)
    notes           = Column(Text, default="")
    created_at      = Column(DateTime, default=datetime.utcnow)
    sets            = relationship("WorkoutSetDB", back_populates="session", lazy="select")

class WorkoutSetDB(Base):
    __tablename__ = "workout_sets"
    id          = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id  = Column(PGUUID(as_uuid=True), ForeignKey("workout_sessions.id"), nullable=False)
    exercise_id = Column(PGUUID(as_uuid=True), ForeignKey("exercises.id"), nullable=False)
    set_number  = Column(Integer, nullable=False)
    reps        = Column(Integer, nullable=False)
    weight_lbs  = Column(Float, nullable=False)
    is_pr       = Column(Boolean, default=False)
    rpe         = Column(Float, nullable=True)
    notes       = Column(Text, default="")
    logged_at   = Column(DateTime, default=datetime.utcnow)
    session     = relationship("WorkoutSessionDB", back_populates="sets")

class PersonalRecordDB(Base):
    __tablename__ = "personal_records"
    id          = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(PGUUID(as_uuid=True), nullable=False, index=True)
    exercise_id = Column(PGUUID(as_uuid=True), ForeignKey("exercises.id"), nullable=False)
    pr_type     = Column(String(20), nullable=False)  # "1rm", "3rm", "5rm", "volume"
    value       = Column(Float, nullable=False)
    set_on      = Column(Date, nullable=False)
    session_id  = Column(PGUUID(as_uuid=True), nullable=True)


# ─────────────────────────────────────────────
#  PYDANTIC SCHEMAS
# ─────────────────────────────────────────────
class SetIn(BaseModel):
    exercise_id: uuid.UUID
    set_number:  int = Field(ge=1, le=20)
    reps:        int = Field(ge=1, le=100)
    weight_lbs:  float = Field(ge=0, le=2000)
    rpe:         Optional[float] = Field(None, ge=1, le=10)
    notes:       Optional[str]  = Field(None, max_length=300)

class LogWorkoutIn(BaseModel):
    plan_id:          Optional[uuid.UUID] = None
    day_id:           Optional[uuid.UUID] = None
    started_at:       datetime
    ended_at:         Optional[datetime]  = None
    duration_seconds: int = Field(ge=60, le=86400)
    sets:             list[SetIn] = Field(min_items=1)
    notes:            Optional[str] = Field(None, max_length=1000)

    @validator("ended_at")
    def end_after_start(cls, v, values):
        if v and "started_at" in values and v <= values["started_at"]:
            raise ValueError("ended_at must be after started_at")
        return v

class SetOut(BaseModel):
    id:         uuid.UUID
    exercise_id:uuid.UUID
    set_number: int
    reps:       int
    weight_lbs: float
    is_pr:      bool
    rpe:        Optional[float]
    logged_at:  datetime
    class Config: orm_mode = True

class SessionOut(BaseModel):
    id:               uuid.UUID
    user_id:          uuid.UUID
    started_at:       datetime
    duration_seconds: Optional[int]
    total_volume_lbs: float
    notes:            str
    sets:             list[SetOut] = []
    pr_count:         int = 0
    class Config: orm_mode = True

class ExerciseOut(BaseModel):
    id:             uuid.UUID
    name:           str
    slug:           str
    primary_muscle: MuscleGroup
    movement_type:  MovementType
    equipment:      str
    is_compound:    bool
    class Config: orm_mode = True

class StrengthDataPoint(BaseModel):
    date:      date
    weight_lbs:float
    reps:      int
    estimated_1rm: float

class ProgressOut(BaseModel):
    exercise_id:  uuid.UUID
    exercise_name:str
    history:      list[StrengthDataPoint]
    current_pr:   Optional[float]
    improvement_pct: float


# ─────────────────────────────────────────────
#  BUSINESS LOGIC HELPERS
# ─────────────────────────────────────────────
def epley_1rm(weight: float, reps: int) -> float:
    """Epley formula for estimated 1-rep max."""
    if reps == 1:
        return weight
    return round(weight * (1 + reps / 30), 2)

def calculate_volume(sets: list[SetIn]) -> float:
    """Total volume in lbs = sum(weight × reps) across all sets."""
    return round(sum(s.weight_lbs * s.reps for s in sets), 2)

async def detect_prs(
    user_id: uuid.UUID,
    sets: list[SetIn],
    session_id: uuid.UUID,
    db: AsyncSession,
) -> dict[uuid.UUID, bool]:
    """
    Compare each set's estimated 1RM against the user's existing PR.
    Returns mapping of set index → is_pr.
    """
    pr_flags: dict[uuid.UUID, bool] = {}

    # Group sets by exercise
    by_exercise: dict[uuid.UUID, list[SetIn]] = {}
    for s in sets:
        by_exercise.setdefault(s.exercise_id, []).append(s)

    for ex_id, ex_sets in by_exercise.items():
        # Fetch current 1RM PR
        result = await db.execute(
            select(PersonalRecordDB)
            .where(
                PersonalRecordDB.user_id    == user_id,
                PersonalRecordDB.exercise_id== ex_id,
                PersonalRecordDB.pr_type    == "1rm",
            )
            .order_by(desc(PersonalRecordDB.value))
            .limit(1)
        )
        current_pr = result.scalar_one_or_none()
        best_1rm   = current_pr.value if current_pr else 0.0

        # Find best 1RM from today's sets
        session_best = max(epley_1rm(s.weight_lbs, s.reps) for s in ex_sets)

        if session_best > best_1rm:
            # New PR — mark the set that achieved it, upsert record
            best_set = max(ex_sets, key=lambda s: epley_1rm(s.weight_lbs, s.reps))
            pr_flags[id(best_set)] = True

            if current_pr:
                current_pr.value     = session_best
                current_pr.set_on    = date.today()
                current_pr.session_id= session_id
            else:
                db.add(PersonalRecordDB(
                    user_id    = user_id,
                    exercise_id= ex_id,
                    pr_type    = "1rm",
                    value      = session_best,
                    set_on     = date.today(),
                    session_id = session_id,
                ))

    return pr_flags


async def update_streak(user_id: uuid.UUID):
    """
    Background task — recalculate workout streak for user.
    In production this would write to Redis and/or users table.
    """
    await asyncio.sleep(0)   # Simulate async DB call
    print(f"[BG] Streak updated for user {user_id}")


async def award_badges(user_id: uuid.UUID, session: WorkoutSessionDB):
    """
    Background task — check badge criteria after a session.
    E.g. 'First Workout', '100 Sessions', 'Century Club' (bench 100kg).
    """
    await asyncio.sleep(0)
    print(f"[BG] Badge check for user {user_id}, volume={session.total_volume_lbs:.0f}lbs")


# ─────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI(
    title="FITCORE Workout Service",
    version="1.0.0",
    description="Core workout logging, exercise library, and strength analytics.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_user_id(x_user_id: str | None = None) -> uuid.UUID:
    """Extract validated user ID injected by the gateway."""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid user ID format")


# ─────────────────────────────────────────────
#  EXERCISE LIBRARY
# ─────────────────────────────────────────────
@app.get("/exercises", response_model=list[ExerciseOut], tags=["exercises"])
async def list_exercises(
    muscle:    Optional[MuscleGroup]  = Query(None),
    movement:  Optional[MovementType] = Query(None),
    compound:  Optional[bool]         = Query(None),
    search:    Optional[str]          = Query(None, max_length=80),
    limit:     int = Query(40, le=200),
    offset:    int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    q = select(ExerciseDB)
    if muscle:    q = q.where(ExerciseDB.primary_muscle == muscle)
    if movement:  q = q.where(ExerciseDB.movement_type  == movement)
    if compound is not None: q = q.where(ExerciseDB.is_compound == compound)
    if search:    q = q.where(ExerciseDB.name.ilike(f"%{search}%"))
    q = q.order_by(ExerciseDB.name).offset(offset).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@app.get("/exercises/{exercise_id}", response_model=ExerciseOut, tags=["exercises"])
async def get_exercise(exercise_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    ex = await db.get(ExerciseDB, exercise_id)
    if not ex:
        raise HTTPException(status_code=404, detail="Exercise not found")
    return ex


# ─────────────────────────────────────────────
#  WORKOUT PLANS
# ─────────────────────────────────────────────
@app.get("/workouts/plans", tags=["plans"])
async def get_user_plans(
    x_user_id: str  = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user_id(x_user_id)
    result  = await db.execute(
        select(WorkoutPlanDB)
        .where(WorkoutPlanDB.user_id == user_id, WorkoutPlanDB.is_active == True)
        .options(selectinload(WorkoutPlanDB.days))
        .order_by(desc(WorkoutPlanDB.created_at))
    )
    return result.scalars().all()


# ─────────────────────────────────────────────
#  SESSION LOGGING
# ─────────────────────────────────────────────
@app.post("/workouts/log", response_model=SessionOut, status_code=status.HTTP_201_CREATED, tags=["sessions"])
async def log_workout(
    body:     LogWorkoutIn,
    bg:       BackgroundTasks,
    x_user_id:str  = None,
    db: AsyncSession = Depends(get_db),
):
    user_id    = current_user_id(x_user_id)
    session_id = uuid.uuid4()

    # 1. Verify all exercise IDs exist
    ex_ids = list({s.exercise_id for s in body.sets})
    for ex_id in ex_ids:
        if not await db.get(ExerciseDB, ex_id):
            raise HTTPException(status_code=400, detail=f"Exercise {ex_id} not found")

    # 2. Calculate total volume
    total_volume = calculate_volume(body.sets)

    # 3. Create session
    session = WorkoutSessionDB(
        id               = session_id,
        user_id          = user_id,
        plan_id          = body.plan_id,
        day_id           = body.day_id,
        started_at       = body.started_at,
        ended_at         = body.ended_at,
        duration_seconds = body.duration_seconds,
        total_volume_lbs = total_volume,
        notes            = body.notes or "",
    )
    db.add(session)

    # 4. Detect PRs before flushing sets
    pr_flags = await detect_prs(user_id, body.sets, session_id, db)

    # 5. Persist sets
    for s in body.sets:
        db.add(WorkoutSetDB(
            session_id  = session_id,
            exercise_id = s.exercise_id,
            set_number  = s.set_number,
            reps        = s.reps,
            weight_lbs  = s.weight_lbs,
            is_pr       = pr_flags.get(id(s), False),
            rpe         = s.rpe,
            notes       = s.notes or "",
        ))

    await db.commit()
    await db.refresh(session)

    pr_count = sum(1 for f in pr_flags.values() if f)

    # 6. Background tasks (non-blocking)
    bg.add_task(update_streak, user_id)
    bg.add_task(award_badges, user_id, session)

    result = SessionOut.from_orm(session)
    result.pr_count = pr_count
    return result


@app.get("/workouts/sessions", response_model=list[SessionOut], tags=["sessions"])
async def get_sessions(
    limit:     int = Query(20, le=100),
    offset:    int = Query(0, ge=0),
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    x_user_id: str = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user_id(x_user_id)
    q = (
        select(WorkoutSessionDB)
        .where(WorkoutSessionDB.user_id == user_id)
        .options(selectinload(WorkoutSessionDB.sets))
        .order_by(desc(WorkoutSessionDB.started_at))
        .offset(offset).limit(limit)
    )
    if from_date:
        q = q.where(WorkoutSessionDB.started_at >= datetime.combine(from_date, datetime.min.time()))
    if to_date:
        q = q.where(WorkoutSessionDB.started_at <= datetime.combine(to_date, datetime.max.time()))
    result = await db.execute(q)
    return result.scalars().all()


# ─────────────────────────────────────────────
#  STRENGTH PROGRESS & ANALYTICS
# ─────────────────────────────────────────────
@app.get("/progress/strength", response_model=ProgressOut, tags=["analytics"])
async def strength_progress(
    exercise_id: uuid.UUID,
    weeks:       int = Query(12, ge=1, le=104),
    x_user_id:   str = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user_id(x_user_id)
    since   = date.today() - timedelta(weeks=weeks)

    # Fetch all sets for this exercise in the window
    result = await db.execute(
        select(WorkoutSetDB, WorkoutSessionDB.started_at)
        .join(WorkoutSessionDB, WorkoutSetDB.session_id == WorkoutSessionDB.id)
        .where(
            WorkoutSessionDB.user_id == user_id,
            WorkoutSetDB.exercise_id  == exercise_id,
            WorkoutSessionDB.started_at >= datetime.combine(since, datetime.min.time()),
        )
        .order_by(WorkoutSessionDB.started_at)
    )
    rows = result.all()

    if not rows:
        raise HTTPException(status_code=404, detail="No data found for this exercise")

    # Group by date, take best estimated 1RM per day
    by_date: dict[date, tuple[float, int]] = {}
    for s, started_at in rows:
        d      = started_at.date()
        e1rm   = epley_1rm(s.weight_lbs, s.reps)
        if d not in by_date or e1rm > epley_1rm(*by_date[d]):
            by_date[d] = (s.weight_lbs, s.reps)

    history = [
        StrengthDataPoint(
            date=d,
            weight_lbs=w,
            reps=r,
            estimated_1rm=epley_1rm(w, r),
        )
        for d, (w, r) in sorted(by_date.items())
    ]

    current_pr = max(p.estimated_1rm for p in history) if history else None
    first_e1rm = history[0].estimated_1rm if history else 0
    improvement = (
        round((current_pr - first_e1rm) / first_e1rm * 100, 1)
        if first_e1rm > 0 and current_pr else 0
    )

    ex = await db.get(ExerciseDB, exercise_id)

    return ProgressOut(
        exercise_id      = exercise_id,
        exercise_name    = ex.name if ex else "Unknown",
        history          = history,
        current_pr       = current_pr,
        improvement_pct  = improvement,
    )


@app.get("/progress/volume-by-muscle", tags=["analytics"])
async def volume_by_muscle(
    weeks:     int = Query(4, ge=1, le=52),
    x_user_id: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Return total volume per muscle group over N weeks."""
    user_id = current_user_id(x_user_id)
    since   = datetime.utcnow() - timedelta(weeks=weeks)

    result = await db.execute(
        select(
            ExerciseDB.primary_muscle,
            func.sum(WorkoutSetDB.weight_lbs * WorkoutSetDB.reps).label("volume"),
        )
        .join(WorkoutSetDB, WorkoutSetDB.exercise_id == ExerciseDB.id)
        .join(WorkoutSessionDB, WorkoutSetDB.session_id == WorkoutSessionDB.id)
        .where(
            WorkoutSessionDB.user_id    == user_id,
            WorkoutSessionDB.started_at >= since,
        )
        .group_by(ExerciseDB.primary_muscle)
        .order_by(desc("volume"))
    )
    return [{"muscle": row[0], "volume_lbs": round(row[1], 1)} for row in result.all()]


@app.get("/progress/weekly-summary", tags=["analytics"])
async def weekly_summary(
    weeks:     int = Query(8, ge=1, le=52),
    x_user_id: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Volume, session count, and avg duration per week."""
    user_id = current_user_id(x_user_id)
    since   = datetime.utcnow() - timedelta(weeks=weeks)

    result = await db.execute(
        select(
            func.date_trunc("week", WorkoutSessionDB.started_at).label("week"),
            func.count(WorkoutSessionDB.id).label("sessions"),
            func.sum(WorkoutSessionDB.total_volume_lbs).label("volume"),
            func.avg(WorkoutSessionDB.duration_seconds).label("avg_duration"),
        )
        .where(
            WorkoutSessionDB.user_id    == user_id,
            WorkoutSessionDB.started_at >= since,
        )
        .group_by("week")
        .order_by("week")
    )
    return [
        {
            "week_start":   row.week.date().isoformat(),
            "sessions":     row.sessions,
            "volume_lbs":   round(row.volume or 0, 1),
            "avg_duration_min": round((row.avg_duration or 0) / 60, 1),
        }
        for row in result.all()
    ]


# ─────────────────────────────────────────────
#  PERSONAL RECORDS
# ─────────────────────────────────────────────
@app.get("/prs", tags=["records"])
async def get_all_prs(
    x_user_id: str = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = current_user_id(x_user_id)
    result  = await db.execute(
        select(PersonalRecordDB, ExerciseDB.name)
        .join(ExerciseDB, PersonalRecordDB.exercise_id == ExerciseDB.id)
        .where(PersonalRecordDB.user_id == user_id)
        .order_by(desc(PersonalRecordDB.set_on))
    )
    return [
        {
            "exercise": row[1],
            "type":     row[0].pr_type,
            "value":    row[0].value,
            "date":     row[0].set_on.isoformat(),
        }
        for row in result.all()
    ]


# ─────────────────────────────────────────────
#  HEALTH
# ─────────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "service": "workout", "ts": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────────
#  DB INIT (dev only)
# ─────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✔  FITCORE Workout Service ready")
