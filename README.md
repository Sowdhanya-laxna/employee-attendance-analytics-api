# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # or export MONGO_URI / MONGO_DB (real env vars win)
uvicorn app.main:app --port 8000
```
Indexes are created at startup. `GET /health` returns 200 once MongoDB answers.

## Notes
- Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
- `moving_avg_7d` is the mean of the already-rounded (4 dp) daily rates.
- Concurrent PATCHes on one record: the loser gets 409 (compare-and-set on history length and punch times).
- Check script: `python tools/check_analytics.py` compares the analytics endpoints with a plain-Python calculation on awkward data (see the file header).
