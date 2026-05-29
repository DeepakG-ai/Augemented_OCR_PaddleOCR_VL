# Client Agent

This document details the design, operations, and execution lifecycles of the desktop Client Agent, which runs on client Windows machines to automate scheduled document uploads.

---

## What it is

The Client Agent is a standalone Windows executable (`.exe`) compiled using PyInstaller. It runs on client machines and automates scheduled ingestion. The agent synchronizes folder paths and schedules with the main application server, checks local directories for new PDFs at scheduled times, uploads them using the REST API, monitors execution via a Server-Sent Events (SSE) progress stream, and handles local folder organization based on success or failure.

---

## Key States

- **IDLE**: The agent is sleeping and waiting for the next scheduled fire time.
- **SYNCING**: The agent is communicating with the server to fetch folder configurations or active schedule updates.
- **PROCESSING**: The agent is actively scanning folders, uploading a batch of PDFs, and listening to live pipeline streams.
- **OFFLINE**: The agent cannot reach the server URL. It logs errors and enters a reconnection poll state.

---

## How it works

The Client Agent operates as a multi-threaded daemon running locally on the user's desktop.

```
                    [Agent Starts]
                          │
                          ▼
            [Read Config / Input Auth]
            (client_agent.json or CLI)
                          │
                          ▼
             [Establish API Connection]
                          │
         ┌────────────────┴────────────────┐
         ▼ (Thread 1)                      ▼ (Thread 2)
   [Heartbeat Loop]                [Config Sync Loop]
    (POST /heartbeat                (GET /api/config
       every 30s)                    every 60s)
         │                                 │
         │                                 ▼ (Thread 3)
         │                         [Scheduler Loop]
         │                         (Sleep until due)
         │                                 │
         │                                 ▼
         │                         [Scan Input Folder]
         │                                 │
         │                                 ▼
         │                        [Upload to /ingest]
         │                                 │
         │                                 ▼
         │                        [Listen SSE Stream]
         │                                 │
         ▼                                 ▼
    [Server UI updates]          [Move PDF & Write JSON]
```

### 1. Initialisation & Authentication
1. **Config Loading**: On startup, the agent reads its target server URL from the local configuration file `client_agent.json` (located next to the `.exe`). Values can be overridden via CLI flags or environment variables (`AUGOCR_SERVER_URL`).
2. **Credential Capture**: It prompts the user for their email and password via secure terminal prompt (`getpass`). Credentials are kept strictly in-memory.
3. **Login Exchange**: It calls `POST /auth/login` to obtain a JWT (8h TTL).
4. **Auto-Renewal Thread**: A background thread (`_token_refresh_loop`) automatically renews the token every 7 hours to prevent token expiration mid-operation. If any request returns HTTP 401, the agent invalidates the token and forces a re-login.

### 2. Synchronization & Heartbeats (Telemetry)
- **Heartbeat Thread**: Posts to `POST /api/client/heartbeat` every 30 seconds. The server tracks this timestamp to render an `ACTIVE` or `INACTIVE` status indicator on the web settings dashboard.
- **Config Polling Thread**: Fetches directories every 60 seconds from `GET /api/config` (including local paths for `input_folder`, `output_folder`, `success_folder`, and `failed_folder`). Any path updates made on the server take effect locally without restarting the agent executable.

### 3. The Scheduled Execution Loop
1. **Fire Time Computation**: The agent fetches schedules (`GET /api/scheduler`) every 60 seconds, parses active cron expressions, and sleeps until the next scheduled fire time. Waking early only occurs if configurations or schedules change.
2. **Batch Lock**: When a schedule fires, the agent POSTs to `POST /api/scheduler/{id}/running` to signal the server. The server blocks manual web uploads during the scheduled execution.
3. **File Stability Check**: The agent scans the `input_folder` for files ending in `.pdf` or `.PDF`. For each file, it monitors the size for 1 second. If the size remains stable, the file has finished copying and is safe to upload.
4. **Ingestion & SSE Streaming**:
   - The agent POSTs the PDF via `POST /ingest/rest`.
   - It establishes a connection to `GET /jobs/{job_id}/stream`.
   - It processes progress events in real-time, outputting logs to the terminal.
5. **Output Writing & File Routing**:
   - **JSON Output**: The agent formats the final result (using `_format_extraction_for_output()` to strip internal metadata keys like `_page` or `_source` and nest table rows under `line_items`) and saves it as `<file_stem>.json` to `output_folder`. This JSON is written for *every* file, even if the upload failed.
   - **PDF Routing**: On successful extraction (`done`), the original PDF is moved to `success_folder`. On partial extraction, timeouts, or failure, it is moved to `failed_folder`.

---

## Rules & Hard Constraints

- **In-Memory Credentials**: Credentials must never be written to disk. The agent must prompt for password entry on startup if not supplied via temporary environment variables.
- **Stable File Boundary**: To prevent partial uploads of files still being copied/written to the input folder, the file size must remain unchanged for at least 1.0 second before the upload starts.
- **Deterministic Output JSON**: An output JSON containing the execution status and timestamps must always be saved to `output_folder` for every file scanned, ensuring client scripts can monitor progress deterministically.
- **Conflict Prevention**: The agent must lock scheduler status using the `/running` endpoint before scanning the input folder. This prevents web users from initiating conflicting uploads during scheduled batch cycles.

---

## All Scenarios in Plain English

### Scenario 1 — Normal schedule run
- The agent calculates the next run is at 10:00 UTC. It sleeps until 10:00.
- At 10:00, the agent wakes up, locks schedule 1, and scans `C:\OCR\Input\`. It finds `invoice_99.pdf`.
- It verifies size stability, then uploads the file.
- It receives a job ID and streams SSE events. Once the `done` event is received, it writes `invoice_99.json` to the output folder.
- It moves the original PDF to the success folder and calls `/ran` to release the schedule lock.

### Scenario 2 — Network disconnect and token expiration
- The agent is running in the background. The network drops out, causing the config and heartbeat requests to fail.
- Once the network recovers, the next API request receives HTTP 401 (JWT expired while offline).
- The agent's token manager intercepts the 401, calls the login endpoint, obtains a fresh token, and transparently retries the original request.

### Scenario 3 — PDF upload rejected due to quota exhaustion
- The scheduler fires and finds `invoice_heavy.pdf`.
- During upload, the server returns HTTP 402 (Quota Exceeded).
- The agent catches the 402 code, writes `invoice_heavy.json` with status `"failed"` and error `"subscription quota exceeded"`, and moves the PDF to the failed folder.

---

## Error Responses Handled

The client agent wraps API calls and handles these server responses:

| Server Response | Agent Action | Written JSON Status |
|---|---|---|
| HTTP 401 (Unauthorized) | Invalidates JWT and performs immediate re-login. | N/A (Retried) |
| HTTP 402 (Payment Required) | Stops batch, logs quota exhaustion, and moves file to failed. | `"failed"` (error: `"subscription quota exceeded"`) |
| HTTP 409 (Conflict - Vendor) | Logs vendor missing error, skips file, and moves to failed. | `"failed"` (error: `"vendor not registered — add it in the server UI first"`) |
| Network Timeout | Retries upload based on backoff schedule (3s, 10s, 30s) before failing. | `"failed"` (error: `"upload failed after all retries"`) |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_client_agent.py`](../../tests/test_client_agent.py) | `test_app_dir_uses_exe_folder_when_frozen` | Verifies correct path lookup when running frozen as PyInstaller `.exe`. |
| | `test_login_success_stores_token` | Verifies Token Manager saves token and correctly tracks its lifetime. |
| | `test_401_triggers_relogin_and_retry` | Verifies `_call` automatically attempts re-login and resubmission on 401s. |
| | `test_returns_true_when_size_stable` | Verifies stability guard waits for file-write completion. |
| | `test_writes_json_on_success` | Verifies success JSON format and structural layout matching rules. |
| | `test_raw_extraction_is_written_as_output_shape` | Verifies output formatting filters out internal keys and names columns. |
| | `test_parses_data_lines` | Verifies Server-Sent Events parser decodes data packets and drops comments. |
| | `test_missed_schedule_is_skipped_until_tomorrow` | Verifies that schedules missed outside the grace window reschedule for the next day. |

---

## Quick Reference

| Action / Operation | API Path / Local Target | Key Fields | Notes |
|---|---|---|---|
| Sync Directory Mappings | `GET /api/config` | `input_folder`, `output_folder`, etc. | Polled every 60s |
| Sync Cron Schedules | `GET /api/scheduler` | `schedules` array | Polled every 60s |
| Telemetry Heartbeat | `POST /api/client/heartbeat` | n/a | Called every 30s |
| Ingestion Target | `POST /ingest/rest` | `file` multipart | Triggers extraction pipeline |
| Progress Telemetry | `GET /jobs/{job_id}/stream` | SSE events stream | Avoids polling overhead |
