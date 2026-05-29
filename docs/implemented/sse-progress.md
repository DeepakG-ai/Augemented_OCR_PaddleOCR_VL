# SSE Progress Stream

This document details the design, authentication methods, data slimming rules, and event payloads of the Server-Sent Events (SSE) progress streaming endpoints.

---

## What it is

The Server-Sent Events (SSE) progress system provides real-time updates of the document extraction pipeline. Instead of requiring web browsers or desktop clients to poll the API repeatedly for job status changes, clients open a single, long-lived connection. The server pushes status and progress updates immediately as they occur in the database.

---

## Key States

- **progress**: The job is in-flight (e.g., normalizing, running OCR, or extracting values page-by-page).
- **done**: The extraction pipeline completed successfully. The connection is closed.
- **failed**: The pipeline encountered an unrecoverable error. The connection is closed.
- **partial**: The pipeline had a partial completion. The connection is closed.

---

## How it works

The SSE streaming endpoint acts as an asynchronous bridge between the worker pipeline and the client.

```
 [Client opens connection to /jobs/{id}/stream]
                       │
                       ▼
             [Authenticate User]
        (Header token OR query param)
                       │
                       ▼
             [Run loop in _generate()]
                       │
                       ├─► (Client disconnected?) ──► [Exit generator]
                       │
                       ▼
                [Fetch Job & Extraction]
                       │
             [Compute State Fingerprint]
            (job_status|ext_status|progress)
                       │
                       ▼
           (Has state changed from last iteration?)
                       │
         ┌─────────────┴─────────────┐
         ▼ Yes                       ▼ No
   (Is it terminal?)           [Sleep 1s & loop]
         │
         ├─ No ──► [Yield progress event] (Slim payload: no result/ocr_data)
         │
         └─ Yes ─► [Yield terminal event] (Full payload with extraction result)
                               │
                               ▼
                        [Close connection]
```

### 1. The Handshake & Auth Fallback
- **Route**: `GET /jobs/{job_id}/stream`
- **Authentication**:
  - The endpoint requires validation of the user's role.
  - Because standard web browser `EventSource` APIs do not support setting custom request headers, the authorization middleware (`get_current_user_sse`) accepts the JWT token in two ways:
    1. A standard `Authorization: Bearer <JWT>` header.
    2. A query parameter fallback: `?token=<JWT>`.

### 2. The Fingerprint Loop
The streaming generator (`_generate`) runs on a 1-second interval:
1. **Fetch Latest State**: It queries the database for the current job. If the job is linked to an extraction, it retrieves the extraction status as well. Since the pipeline transitions from OCR to LLM and Postprocess, it automatically resolves and tracks the *latest* active child job for the extraction.
2. **State Fingerprinting**: It builds a simple text fingerprint: `"{job_status}|{ext_status}|{progress_message}"`.
3. **Change Detection**: If the fingerprint matches the previous iteration, the yield is skipped, saving CPU and database bandwidth. If it changes, a payload is compiled.

### 3. Data Slimming & Payload Rules
- **Progress Events**: To minimize network bandwidth for large multi-page uploads, intermediate `progress` events are strictly **slimmed**. Heavy JSON columns—specifically `result`, `page_results`, `ocr_data`, `field_locations`, `corrected_result`, and `correction_meta`—are stripped out of the message.
- **Terminal Events**: When the job reaches a terminal state (`done`, `failed`, `partial`, `cancelled`, or `unverified`), the server yields the *full* payload including the extraction results, writes the event data, and breaks the generator loop to close the HTTP connection.

---

## Rules & Hard Constraints

- **Query Token Authorization**: The token validation logic must support the `?token=` query parameter fallback.
- **Terminal Event Inclusion**: Heavy extraction results must be delivered in the terminal event (`done` / `failed`) to ensure the client receives the extraction output before the connection closes.
- **Intermediate Slimming**: Heavy JSON columns must never be sent in intermediate `progress` events.
- **Connection Disconnect Handling**: The generator must actively poll `request.is_disconnected()` on every cycle and break immediately if the client closes the socket, preventing backend thread/connection leakage.

---

## All Scenarios in Plain English

### Scenario 1 — Smooth multi-stage extraction
- The Client Agent uploads a PDF, gets `job_id = 50`, and opens the SSE stream with `?token=eyJ...`.
- Stage 1: Normalize starts. The server yields: `{"event": "progress", "extraction": {"status": "normalizing", "progress": "Rendering PDF pages"}}`.
- Stage 2: OCR runs. The server yields: `{"event": "progress", "extraction": {"status": "processing_ocr", "progress": "Extracting text geometry"}}`.
- Stage 3: LLM runs. The server yields: `{"event": "progress", "extraction": {"status": "processing_llm", "progress": "Running LLM extractor"}}`.
- Stage 4: Postprocess runs and completes. The server yields: `{"event": "done", "extraction": {"status": "done", "result": {"po_number": "123", ...}}}`.
- The connection is closed.

### Scenario 2 — Connection drops mid-flight
- A user is viewing progress on the browser.
- During the LLM extraction stage, the user closes the browser tab.
- On the next 1-second check, the server's generator detects `await request.is_disconnected()` is True.
- The generator breaks the loop, terminating the stream handler and freeing database connection pool slots.

---

## Error Responses

| Situation | HTTP Code | Event Message |
|---|---|---|
| Invalid token supplied | 401 | Connection rejected |
| Requesting stream for unowned job | 403 | Connection rejected |
| Job ID does not exist | 404 | `data: {"event": "error", "error": "Job not found"}` (and closes) |

---

## Test Coverage

| Test Module | Test Name | What it proves |
|---|---|---|
| [`test_client_agent.py`](../../tests/test_client_agent.py) | `test_parses_data_lines` | Verifies the client agent SSE line parser correctly extracts JSON data. |
| | `test_tolerates_invalid_json` | Verifies the client agent tolerates malformed JSON SSE lines without crashing. |
| [`test_auth.py`](../../tests/test_auth.py) | `test_get_current_user_sse_accepts_query_token` | Verifies authorization middleware accepts query-string token fallbacks. |
| [`test_review_api.py`](../../tests/test_review_api.py) | `test_get_extraction_ocr_returns_200_with_payload` | Verifies geometry endpoint access controls (shares extraction ownership logic). |

---

## Quick Reference

| Endpoint / Operation | HTTP Method | Auth Mode | Payload Output |
|---|---|---|---|
| Job SSE Stream | `GET /jobs/{job_id}/stream` | Bearer OR `?token=` query | Pushes progress/done/failed events |
| Connection Keep-Alive | n/a | n/a | Sends keep-alive messages |
| Config SSE Channel | `GET /api/config/stream` | Bearer OR `?token=` query | Pushes config change notices |
