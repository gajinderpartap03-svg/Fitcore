# FITCORE — Full-Stack Fitness Platform

A production-ready fitness platform built with a 3-tier architecture.

```
fitcore/
├── frontend/          # HTML/CSS/JS + Three.js dashboard
│   └── index.html
├── middleware/        # Node.js API Gateway
│   ├── gateway.js
│   └── package.json
└── backend/           # Python FastAPI workout service
    ├── workout_service.py
    └── requirements.txt
```

---

## Tier 1 — Frontend (`/frontend`)

**Stack:** Vanilla HTML/CSS/JS + Three.js

**Features:**
- Live 3D anatomical body model (Three.js) with muscle group highlighting
- Workout session tracker with exercise checklist
- Weekly volume bar chart
- Macro & nutrition progress bars
- Activity heatmap (GitHub-style, 13 weeks)
- Goals ring chart
- Community leaderboard
- AI Coach insight panel

**Run:**
```bash
# Any static server works:
npx serve frontend/
# or just open frontend/index.html in a browser
```

---

## Tier 2 — Middleware: API Gateway (`/middleware`)

**Stack:** Node.js + Express

**Responsibilities:**
- JWT authentication (`Authorization: Bearer <token>`)
- Role-based access control (`user`, `admin`, `coach`)
- Per-user rate limiting (100 req/min standard, 10 auth, 20 AI)
- Redis response caching with TTL per route type
- Request validation with Joi schemas
- Reverse proxy to all Python microservices
- Distributed tracing via `X-Trace-Id` header
- Request/response logging

**Routes:**
| Method | Path | Service | Auth |
|--------|------|---------|------|
| POST | /api/auth/register | Auth | No |
| POST | /api/auth/login | Auth | No |
| GET | /api/workouts | Workout | Yes |
| POST | /api/workouts/log | Workout | Yes |
| GET | /api/progress/strength | Workout | Yes |
| GET | /api/nutrition/today | Nutrition | Yes |
| POST | /api/nutrition/meals | Nutrition | Yes |
| GET | /api/coach/plan | AI Coach | Yes |
| POST | /api/coach/analyze | AI Coach | Yes |
| GET | /api/social/leaderboard | Social | Yes |
| GET | /api/admin/metrics | Internal | Admin |

**Run:**
```bash
cd middleware
npm install
# Set env vars (see below)
npm run dev
```

**Environment Variables:**
```env
PORT=3000
JWT_SECRET=your-secret-here
REDIS_URL=redis://localhost:6379
WORKOUT_SVC=http://localhost:8001
NUTRITION_SVC=http://localhost:8002
AI_COACH_SVC=http://localhost:8003
SOCIAL_SVC=http://localhost:8004
AUTH_SVC=http://localhost:8005
```

---

## Tier 3 — Backend: Workout Service (`/backend`)

**Stack:** Python + FastAPI + SQLAlchemy (async) + PostgreSQL

**Database Models:**
- `exercises` — exercise library (500+ exercises)
- `workout_plans` — user training programs (PPL, 5/3/1, etc.)
- `workout_days` — days within a plan (Push A, Pull B, Legs C)
- `plan_exercises` — exercises within a day with sets/reps/RPE targets
- `workout_sessions` — completed workout sessions
- `workout_sets` — individual sets with weight, reps, RPE
- `personal_records` — PRs per exercise (1RM, 3RM, 5RM, volume)

**Business Logic:**
- **Epley formula** for estimated 1RM: `weight × (1 + reps/30)`
- **PR detection** on every logged session — auto-updates personal records
- **Volume calculation** — total tonnage per session and per muscle group
- **Background tasks** — streak updates, badge awards (non-blocking)

**API Endpoints:**
```
GET  /exercises                    — list/search exercise library
GET  /exercises/:id                — exercise detail
GET  /workouts/plans               — user's training plans
POST /workouts/log                 — log a completed session
GET  /workouts/sessions            — session history with filters
GET  /progress/strength            — strength curve for an exercise
GET  /progress/volume-by-muscle    — muscle group volume breakdown
GET  /progress/weekly-summary      — week-by-week volume & frequency
GET  /prs                          — all personal records
GET  /health                       — service health check
```

**Run:**
```bash
cd backend
pip install -r requirements.txt

# Set env:
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost/fitcore"

# Start:
uvicorn workout_service:app --host 0.0.0.0 --port 8001 --reload

# API docs auto-generated at:
# http://localhost:8001/docs
```

---

## Local Development (all services)

```bash
# Start PostgreSQL + Redis
docker-compose up -d postgres redis

# Terminal 1: Backend
cd backend && uvicorn workout_service:app --port 8001 --reload

# Terminal 2: Gateway
cd middleware && npm run dev

# Terminal 3: Frontend
npx serve frontend -p 3001

# Dashboard → http://localhost:3001
# API Docs  → http://localhost:8001/docs
# Gateway   → http://localhost:3000
```

---

## Architecture Diagram

```
Browser / Mobile
      │
      ▼
┌─────────────────────────────────┐
│  API Gateway  (Node.js :3000)   │
│  Auth · Cache · Rate Limit       │
└────┬──────┬──────┬──────┬───────┘
     │      │      │      │
     ▼      ▼      ▼      ▼
  Workout Nutrition AI    Social
  :8001   :8002   :8003  :8004
  Python  Python  Python Node.js
     │      │      │      │
     └──────┴──────┴──────┘
              │
       ┌──────┴──────┐
       │             │
   PostgreSQL      Redis
   (primary DB)  (cache+queue)
```

---

## Next Steps

- [ ] Auth service (JWT issue, refresh tokens, OAuth2)
- [ ] Nutrition service (food database, macro tracking)
- [ ] AI Coach service (workout personalisation, form analysis)
- [ ] Social service (feed, challenges, leaderboard)
- [ ] Celery workers for async analytics
- [ ] Docker Compose for full local stack
- [ ] Kubernetes manifests for production deploy
- [ ] Alembic migrations
