# Local Docker deployment

This branch runs the web application at <http://localhost:8055> while keeping
`llama-server` on the Windows host at port `8056`.

## Start

1. Copy `.env.example` to `.env` and adjust local credentials if needed.
2. Start `llama-server` on the host and verify
   `http://localhost:8056/v1/models` responds.
3. Build and start the stack:

   ```powershell
   docker compose up -d --build
   ```

4. Open <http://localhost:8055>.

The stack includes PostgreSQL, MinIO, MLflow, the API, and one worker for
each pipeline stage (`normalize`, `ocr`, `llm`, and `postprocess`).

## Useful endpoints

- Application/API: <http://localhost:8055>
- API health: <http://localhost:8055/health>
- MinIO console: <http://localhost:9001>
- MLflow: <http://localhost:5000>

## Test

Run the complete local test folder with the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
```

The Docker image keeps `tests/` in `/app/tests`, so the same folder is available
inside the API container when a test runner is installed there.
