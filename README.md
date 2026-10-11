# Employee Attendance & Analytics API

FastAPI + MongoDB backend implementation adhering strictly to Contract v2.0 (`openapi.yaml` and `DATA_MODEL.md`).

## How to Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment configuration:**
   The application reads `MONGO_URI` and `MONGO_DB` from environment variables (falls back to `.env` or defaults to `mongodb://localhost:27017` / `attendance_db`).
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the API service:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Key Highlights

- **Indexes**: Created automatically and idempotently on application startup via FastAPI lifespan handler.
- **Concurrency & Races**: Guaranteed race safety on duplicate employee creates and punch-ins via MongoDB unique compound indexes, and optimistic locking on history modifications.
- **Analytics Aggregations**: Non-trivial MongoDB aggregation pipelines for:
  - Department summary with zero-log headcount preservation (`$lookup` across collections)
  - Late leaderboard using standard competition ranking with tie handling (`$rank` in `$setWindowFields`)
  - Department daily trend with calendar gap filling (`$densify`) and 7-day moving averages
- **Explain Endpoint**: `GET /admin/explain/{endpoint}` executes index-backed scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).
- **Audit History**: Append-only regularization history capturing exact changes with BSON datetime boundary conversion.
