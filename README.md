# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee attendance tracking and analytics.

## Requirements

- Python 3.12+
- MongoDB 6.0+

## Run on Windows

Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as a template, then configure `MONGO_URI` and `MONGO_DB`.

Start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000/docs` to explore the API.

Indexes are created at startup. `GET /health` returns HTTP 200 when MongoDB is reachable.

## Notes

- All application code is in `app/main.py`.
- All endpoints defined in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
- `moving_avg_7d` is the mean of the already-rounded daily rates (4 decimal places).
- Concurrent PATCH requests on the same record use compare-and-set checks; a conflicting update returns HTTP 409.
- Analytics validation script: `python tools/check_analytics.py`.
