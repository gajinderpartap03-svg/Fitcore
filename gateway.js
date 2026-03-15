/**
 * FITCORE — API Gateway (Middleware Layer)
 * Node.js + Express
 *
 * Responsibilities:
 *   - JWT authentication & role-based access control
 *   - Request routing to Python microservices
 *   - Rate limiting per user/IP
 *   - Request validation & sanitisation
 *   - Response caching via Redis
 *   - Request logging & tracing
 *   - WebSocket proxy for real-time workout sessions
 */

const express      = require('express');
const { createProxyMiddleware } = require('http-proxy-middleware');
const rateLimit    = require('express-rate-limit');
const jwt          = require('jsonwebtoken');
const Joi          = require('joi');
const redis        = require('redis');
const morgan       = require('morgan');
const helmet       = require('helmet');
const cors         = require('cors');
const { v4: uuid } = require('uuid');

// ─────────────────────────────────────────────
//  CONFIG
// ─────────────────────────────────────────────
const CONFIG = {
  port:        process.env.PORT         || 3000,
  jwtSecret:   process.env.JWT_SECRET   || 'fitcore-dev-secret-change-in-prod',
  redisUrl:    process.env.REDIS_URL    || 'redis://localhost:6379',
  services: {
    workout:   process.env.WORKOUT_SVC  || 'http://localhost:8001',
    nutrition: process.env.NUTRITION_SVC|| 'http://localhost:8002',
    ai_coach:  process.env.AI_COACH_SVC || 'http://localhost:8003',
    social:    process.env.SOCIAL_SVC   || 'http://localhost:8004',
    auth:      process.env.AUTH_SVC     || 'http://localhost:8005',
  },
  cache: {
    defaultTTL: 300,       // 5 min
    workoutTTL: 60,        // 1 min (changes often)
    leaderboardTTL: 30,    // 30 sec
    profileTTL: 600,       // 10 min
  },
  rateLimit: {
    standard: { windowMs: 60_000, max: 100 },   // 100 req/min
    auth:     { windowMs: 60_000, max: 10  },   // 10 login attempts/min
    ai:       { windowMs: 60_000, max: 20  },   // 20 AI calls/min (expensive)
  }
};

// ─────────────────────────────────────────────
//  APP SETUP
// ─────────────────────────────────────────────
const app = express();

app.use(helmet());
app.use(cors({ origin: process.env.ALLOWED_ORIGINS?.split(',') || '*', credentials: true }));
app.use(express.json({ limit: '2mb' }));
app.use(express.urlencoded({ extended: true }));
app.use(morgan(':method :url :status :response-time ms — :res[content-length]'));

// Attach unique trace ID to every request
app.use((req, _res, next) => {
  req.traceId = req.headers['x-trace-id'] || uuid();
  req.startTime = Date.now();
  next();
});

// ─────────────────────────────────────────────
//  REDIS CLIENT
// ─────────────────────────────────────────────
const redisClient = redis.createClient({ url: CONFIG.redisUrl });
redisClient.on('error', err => console.error('[Redis]', err));
redisClient.connect().catch(console.error);

// ─────────────────────────────────────────────
//  RATE LIMITERS
// ─────────────────────────────────────────────
const limiters = {
  standard: rateLimit({
    ...CONFIG.rateLimit.standard,
    keyGenerator: req => req.user?.id || req.ip,
    message: { error: 'rate_limit_exceeded', retryAfter: 60 },
  }),
  auth: rateLimit({
    ...CONFIG.rateLimit.auth,
    keyGenerator: req => req.ip,
    message: { error: 'too_many_auth_attempts', retryAfter: 60 },
  }),
  ai: rateLimit({
    ...CONFIG.rateLimit.ai,
    keyGenerator: req => req.user?.id || req.ip,
    message: { error: 'ai_rate_limit_exceeded', retryAfter: 60 },
  }),
};

// ─────────────────────────────────────────────
//  JWT MIDDLEWARE
// ─────────────────────────────────────────────
function requireAuth(req, res, next) {
  const header = req.headers.authorization;
  if (!header?.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'missing_token' });
  }
  try {
    req.user = jwt.verify(header.slice(7), CONFIG.jwtSecret);
    req.headers['x-user-id']   = req.user.id;
    req.headers['x-user-role'] = req.user.role;
    next();
  } catch (e) {
    res.status(401).json({ error: 'invalid_token', detail: e.message });
  }
}

function requireRole(...roles) {
  return (req, res, next) => {
    if (!roles.includes(req.user?.role)) {
      return res.status(403).json({ error: 'forbidden', required: roles });
    }
    next();
  };
}

// ─────────────────────────────────────────────
//  RESPONSE CACHE MIDDLEWARE
// ─────────────────────────────────────────────
function cache(ttl) {
  return async (req, res, next) => {
    if (req.method !== 'GET') return next();
    const key = `cache:${req.user?.id || 'anon'}:${req.originalUrl}`;
    try {
      const cached = await redisClient.get(key);
      if (cached) {
        res.setHeader('X-Cache', 'HIT');
        return res.json(JSON.parse(cached));
      }
    } catch { /* redis miss — continue */ }

    // Intercept json() to store in cache
    const origJson = res.json.bind(res);
    res.json = (data) => {
      if (res.statusCode === 200) {
        redisClient.setEx(key, ttl, JSON.stringify(data)).catch(() => {});
      }
      res.setHeader('X-Cache', 'MISS');
      return origJson(data);
    };
    next();
  };
}

// ─────────────────────────────────────────────
//  REQUEST VALIDATION HELPERS
// ─────────────────────────────────────────────
function validate(schema) {
  return (req, res, next) => {
    const { error } = schema.validate(req.body, { abortEarly: false });
    if (error) {
      return res.status(400).json({
        error: 'validation_failed',
        fields: error.details.map(d => ({ field: d.path.join('.'), message: d.message })),
      });
    }
    next();
  };
}

// Validation schemas
const schemas = {
  register: Joi.object({
    email:    Joi.string().email().required(),
    password: Joi.string().min(8).required(),
    name:     Joi.string().min(2).max(60).required(),
    dob:      Joi.date().max('now').required(),
    unit:     Joi.string().valid('metric', 'imperial').default('metric'),
  }),
  logWorkout: Joi.object({
    workout_id: Joi.string().uuid().required(),
    exercises: Joi.array().items(Joi.object({
      exercise_id: Joi.string().uuid().required(),
      sets: Joi.array().items(Joi.object({
        reps:   Joi.number().integer().min(1).max(100).required(),
        weight: Joi.number().min(0).max(2000).required(),
        unit:   Joi.string().valid('kg', 'lbs').required(),
      })).min(1).required(),
      notes: Joi.string().max(500).optional(),
    })).min(1).required(),
    duration_seconds: Joi.number().integer().min(60).required(),
    notes: Joi.string().max(1000).optional(),
  }),
  logMeal: Joi.object({
    meal_type:   Joi.string().valid('breakfast','lunch','dinner','snack').required(),
    food_items: Joi.array().items(Joi.object({
      food_id:  Joi.string().required(),
      grams:    Joi.number().min(1).max(5000).required(),
    })).min(1).required(),
    eaten_at: Joi.date().default(() => new Date()),
  }),
};

// ─────────────────────────────────────────────
//  PROXY FACTORY
// ─────────────────────────────────────────────
function proxy(target, pathRewrite) {
  return createProxyMiddleware({
    target,
    changeOrigin: true,
    pathRewrite,
    on: {
      proxyReq: (proxyReq, req) => {
        proxyReq.setHeader('X-Trace-Id', req.traceId);
        proxyReq.setHeader('X-Gateway', 'fitcore-gateway/1.0');
      },
      error: (err, _req, res) => {
        console.error('[Proxy Error]', err.message);
        res.status(502).json({ error: 'upstream_unavailable', message: err.message });
      },
    },
  });
}

// ─────────────────────────────────────────────
//  HEALTH CHECK
// ─────────────────────────────────────────────
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', uptime: process.uptime(), ts: new Date().toISOString() });
});

// ─────────────────────────────────────────────
//  AUTH ROUTES  (→ Auth Service)
// ─────────────────────────────────────────────
app.post('/api/auth/register',
  limiters.auth,
  validate(schemas.register),
  proxy(CONFIG.services.auth, { '^/api/auth': '' })
);
app.post('/api/auth/login',
  limiters.auth,
  proxy(CONFIG.services.auth, { '^/api/auth': '' })
);
app.post('/api/auth/refresh',
  proxy(CONFIG.services.auth, { '^/api/auth': '' })
);
app.post('/api/auth/logout',
  requireAuth,
  proxy(CONFIG.services.auth, { '^/api/auth': '' })
);

// ─────────────────────────────────────────────
//  WORKOUT ROUTES  (→ Workout Service)
// ─────────────────────────────────────────────
app.get('/api/workouts',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.workout, { '^/api/workouts': '/workouts' })
);
app.get('/api/workouts/:id',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.workout, { '^/api/workouts': '/workouts' })
);
app.post('/api/workouts/log',
  requireAuth, limiters.standard,
  validate(schemas.logWorkout),
  proxy(CONFIG.services.workout, { '^/api/workouts': '/workouts' })
);
app.get('/api/workouts/history',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.workout, { '^/api/workouts': '/workouts' })
);
app.get('/api/exercises',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.profileTTL),
  proxy(CONFIG.services.workout, { '^/api/exercises': '/exercises' })
);
app.get('/api/progress/strength',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.workout, { '^/api/progress': '/progress' })
);

// ─────────────────────────────────────────────
//  NUTRITION ROUTES  (→ Nutrition Service)
// ─────────────────────────────────────────────
app.get('/api/nutrition/today',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.nutrition, { '^/api/nutrition': '' })
);
app.post('/api/nutrition/meals',
  requireAuth, limiters.standard,
  validate(schemas.logMeal),
  proxy(CONFIG.services.nutrition, { '^/api/nutrition': '' })
);
app.get('/api/nutrition/foods/search',
  requireAuth, limiters.standard,
  proxy(CONFIG.services.nutrition, { '^/api/nutrition': '' })
);
app.get('/api/nutrition/goals',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.profileTTL),
  proxy(CONFIG.services.nutrition, { '^/api/nutrition': '' })
);

// ─────────────────────────────────────────────
//  AI COACH ROUTES  (→ AI Service, stricter limits)
// ─────────────────────────────────────────────
app.get('/api/coach/plan',
  requireAuth, limiters.ai,
  cache(CONFIG.cache.profileTTL),
  proxy(CONFIG.services.ai_coach, { '^/api/coach': '' })
);
app.post('/api/coach/analyze',
  requireAuth, limiters.ai,
  proxy(CONFIG.services.ai_coach, { '^/api/coach': '' })
);
app.get('/api/coach/insights',
  requireAuth, limiters.ai,
  cache(CONFIG.cache.workoutTTL),
  proxy(CONFIG.services.ai_coach, { '^/api/coach': '' })
);
app.post('/api/coach/form-check',
  requireAuth, limiters.ai,
  proxy(CONFIG.services.ai_coach, { '^/api/coach': '' })
);

// ─────────────────────────────────────────────
//  SOCIAL ROUTES  (→ Social Service)
// ─────────────────────────────────────────────
app.get('/api/social/feed',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.leaderboardTTL),
  proxy(CONFIG.services.social, { '^/api/social': '' })
);
app.get('/api/social/leaderboard',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.leaderboardTTL),
  proxy(CONFIG.services.social, { '^/api/social': '' })
);
app.post('/api/social/posts',
  requireAuth, limiters.standard,
  proxy(CONFIG.services.social, { '^/api/social': '' })
);
app.get('/api/social/challenges',
  requireAuth, limiters.standard,
  cache(CONFIG.cache.defaultTTL),
  proxy(CONFIG.services.social, { '^/api/social': '' })
);

// ─────────────────────────────────────────────
//  ADMIN ROUTES (protected)
// ─────────────────────────────────────────────
app.get('/api/admin/metrics',
  requireAuth, requireRole('admin'),
  async (_req, res) => {
    try {
      const info = await redisClient.info('memory');
      res.json({ redis: info, uptime: process.uptime(), nodeVersion: process.version });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  }
);
app.delete('/api/admin/cache',
  requireAuth, requireRole('admin'),
  async (_req, res) => {
    try {
      const keys = await redisClient.keys('cache:*');
      if (keys.length) await redisClient.del(keys);
      res.json({ cleared: keys.length });
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  }
);

// ─────────────────────────────────────────────
//  GLOBAL ERROR HANDLER
// ─────────────────────────────────────────────
app.use((err, req, res, _next) => {
  console.error(`[Error] ${req.method} ${req.path}`, err);
  res.status(err.status || 500).json({
    error: 'internal_error',
    traceId: req.traceId,
    message: process.env.NODE_ENV === 'production' ? 'Something went wrong' : err.message,
  });
});

app.use((_req, res) => res.status(404).json({ error: 'not_found' }));

// ─────────────────────────────────────────────
//  START
// ─────────────────────────────────────────────
app.listen(CONFIG.port, () => {
  console.log(`
  ╔══════════════════════════════════════╗
  ║  FITCORE API Gateway                 ║
  ║  Port   : ${CONFIG.port}                      ║
  ║  Env    : ${(process.env.NODE_ENV || 'development').padEnd(12)}            ║
  ║  Redis  : ${CONFIG.redisUrl.slice(0,24).padEnd(24)}    ║
  ╚══════════════════════════════════════╝
  `);
});

module.exports = app;
