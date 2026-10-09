# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # or export MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```
`sample_seed.py` + `sample_data/` (from the kit) load the nine sample documents; delete them if you want a leaner repo.

Indexes are created at startup (idempotent); `/health` retries them if MongoDB was not ready yet.

## Everything in the contract is implemented
Part A fixes (see `REVIEW.md`), punch-out, PATCH with audit history, the four analytics pipelines, and `/admin/explain/{endpoint}`.

## Interpretations I made where the contract is open
* Trend range limit: 92 **rows** inclusive (`(to - from).days + 1 > 92` -> 422).
* Punch-out looks for the latest record with `punch_in <= punched_at`; if `punched_at` is before every punch-in that is a 404, if equal to the punch-in it is a 422.
* PATCH: an explicit `null` for `punch_in` / `punch_out` is treated as "omitted". Supplying punch times for a record that stays ABSENT/LEAVE is 422. "Changes nothing" is judged over all seven tracked fields (so a stored derived value that disagrees with the recomputed one counts as a change).
* Concurrent PATCH / punch-out use compare-and-set filters; the loser gets 409.
* Moving average is the mean of the already-rounded daily rates, rounded half-up to 4 decimals.

## Testing status
Pure rules R1-R5 were unit-tested (spec examples, overnight, boundaries, half-up). The service was **not** run against a live MongoDB in my environment, so please run the sample seed and exercise the endpoints locally before relying on it.
