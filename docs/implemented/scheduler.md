# Scheduler — Plain English Guide

This document explains every part of the scheduler 

---

## What the Two States Mean

There are exactly two runtime states for a schedule. Do not confuse them.

**Active** means the schedule is switched on and waiting for its configured time. Nothing is happening yet. The user can still upload PDFs from the browser. Think of it as a timer that has been set but not gone off yet.

**Running** means the client agent is currently scanning the input folder and sending PDFs to the server. This is the brief window when work is actually happening. While running, the browser upload is blocked for that user.

Active and running are not the same thing. A schedule can be active for hours without ever being running. It is only running for the short period when the client agent is actually uploading files.

---

## The Start and Stop Buttons

**Start** creates a new schedule or re-enables a disabled one at the time the user sets (for example, 10:20 AM). It does not trigger anything immediately. The schedule is now active and will fire at the configured time.

**Stop** disables the schedule. It does not delete it. The schedule record stays in the database with `enabled = false`. It will not fire again until the user presses Start.

A user can have at most **3 schedules** at a time. Trying to create a fourth returns an error that says "Maximum 3".

---

## What the Client Agent Does

The client agent is a desktop program (.exe) that runs on the user's Windows machine. Here is what it does, step by step:

1. It starts up and asks for email and password (typed in the terminal). Credentials are kept in memory only, never saved to disk.
2. It logs in to the server and gets a token. It quietly re-logs in every 7 hours so the token never expires.
3. It reads the four folder paths (input, output, success, failed) from the server's config page. These folders are set by the user in the Settings page on the server.
4. It sends a heartbeat to the server every 30 seconds so the Settings page shows "ACTIVE".
5. It checks for enabled schedules from the server every time the scheduler loop runs. It sleeps until the next configured fire time.
6. At the exact scheduled time it wakes up (within a 60-second grace window).
7. It tells the server "I am now running" by calling `/api/scheduler/{id}/running`. The server sets `is_executing = TRUE` for that user. The browser upload is now blocked for that user.
8. It scans the input folder for PDF files.
9. For each PDF, it waits for the file to be fully written (size stable for 1 second), then uploads it via `/ingest/rest`. It watches the progress via a live stream and waits until the pipeline finishes.
10. After the PDF is processed, it moves the file to the success folder or the failed folder depending on the result. A JSON result file is always written to the output folder.
11. After all PDFs are done, it tells the server "I am finished" by calling `/api/scheduler/{id}/ran`. The server sets `is_executing = FALSE`. The browser upload is now allowed again.

---

## When UI Upload Is Blocked

The browser upload button is blocked **only** when the scheduler is running (step 7–11 above). If the user tries to upload a file from the browser during this window, the server returns a 409 error with a message that says "Scheduler" so the user knows why.

The block is **per user**. Blocking Client A's upload does not affect Client B at all.

The client agent always uses the `/ingest/rest` endpoint, not the UI endpoint. The `/ingest/rest` endpoint is **never** blocked by the executing check. Only `/ingest/ui` is checked.

---

## Missed Schedule Rule

If the configured time has already passed by more than 60 seconds when the agent checks, the slot is skipped. It will not run late.

Example: schedule is set for 10:20. The agent comes online at 10:40. That 10:20 run is skipped. The next run is tomorrow at 10:20.

The 60-second grace window exists to absorb normal thread wake-up delays (a few seconds of jitter). If the agent woke up at 10:20:05 it will still fire. If it woke up at 10:21:05 it will skip.

---

## All Scenarios in Plain English

### Scenario 1 — Normal run

- Time is 10:18. The user set a schedule for 10:20. The schedule is active.
- UI upload is allowed.
- At 10:20 the client agent wakes up, marks itself running, scans the folder, uploads PDFs.
- UI upload is blocked while it uploads.
- After all PDFs finish, the agent marks itself done. UI upload is allowed again.

### Scenario 2 — No PDFs in the folder

- The scheduler fires at 10:20.
- The client agent marks itself running, scans the folder, finds zero PDFs.
- It still marks itself done immediately. The running flag is cleared.
- UI upload is allowed again. Nothing is uploaded.

### Scenario 3 — User tries to upload from browser while scheduler is running

- It is 10:20. The scheduler is currently running.
- The user opens the browser and tries to upload a PDF.
- The server checks whether the scheduler is executing for that user and finds it is.
- The server returns HTTP 409 with an error message mentioning "Scheduler".
- The browser shows this message to the user.
- When the scheduler finishes uploading, the block is lifted automatically.

### Scenario 4 — Agent is offline at the scheduled time

- Schedule is set for 10:20. The machine is off or the agent is not running.
- At 10:40 the user starts the agent.
- The agent checks whether the 10:20 slot is still within the 60-second grace window. It is not (20 minutes have passed).
- The agent skips the 10:20 run and marks it as skipped.
- The next run is tomorrow at 10:20.

### Scenario 5 — User stops the schedule mid-day

- It is 2:00 PM. The user presses Stop on a schedule that was set for 6:00 PM.
- The schedule is now disabled. It will not fire at 6:00 PM today or any future day.
- UI upload is not affected; it was never blocked.
- If the user presses Start again later, the schedule is re-enabled.

### Scenario 6 — User tries to create a fourth schedule

- The user already has 3 active schedules.
- They click Start to add another at a different time.
- The server checks the count before doing anything else.
- The server returns HTTP 400 with a message saying "Maximum 3".
- Nothing is created.

### Scenario 7 — User updates an existing schedule time

- The user has 3 schedules. They want to change schedule #2 from 9:00 AM to 11:00 AM.
- They call Start with the schedule ID of #2 in the request.
- The server skips the "do you already have 3?" check because this is an update, not a new creation.
- Schedule #2 is updated to 11:00 AM.

### Scenario 8 — Two of the same user's schedules fire at the same time

- A user has two schedules both set for 10:20.
- The client agent detects both are due at the same time.
- The agent calls `/running` for both schedule IDs before scanning.
- It scans the input folder once and uploads all PDFs in a single batch.
- After the batch it calls `/ran` for both schedule IDs.
- Both are marked done. The `is_executing` flag is cleared.

---

## Multi-Client Isolation

Each client (each user account) is completely independent. One client's scheduler never interferes with another client's scheduler.

### How isolation works

Every user has their own `is_executing` flag in the database, tracked by their user ID. When the server checks whether to block a browser upload, it looks up `is_executing` for **that specific user only**. It has no knowledge of what any other user is doing.

The client agent always authenticates with its own email and password. Its token carries its own user ID. Every call it makes — marking running, marking done, uploading files — is scoped to that user's account.

### Scenario 9 — Two clients trigger at the same time

- Client A is logged in as `alice@company.com`. Their schedule fires at 11:30.
- Client B is logged in as `bob@company.com`. Their schedule also fires at 11:30.
- Both agents wake up and call `/api/scheduler/{id}/running` at the same moment.
- The server sets `is_executing = TRUE` for Alice's schedules and `is_executing = TRUE` for Bob's schedules. These are two separate rows in the database.
- Alice's browser upload is blocked because Alice's `is_executing` is TRUE.
- Bob's browser upload is blocked because Bob's `is_executing` is TRUE.
- Alice's blocked state does **not** affect Bob. Bob's blocked state does **not** affect Alice.
- Both agents scan their own input folders (which are configured separately in Settings).
- Both upload their own PDFs to the server. The server processes each independently as separate extraction jobs.
- When Alice's agent finishes, it clears Alice's `is_executing`. Alice's browser upload is allowed again. Bob is still running.
- When Bob's agent finishes, it clears Bob's `is_executing`. Bob's browser upload is allowed again.
- Neither agent ever knew about the other.

### Scenario 10 — Client A is running, Client B uploads from the browser

- Alice's scheduler is currently running (11:30 batch in progress).
- Bob opens the browser and tries to upload a PDF.
- The server checks Bob's `is_executing` flag. It is FALSE. Bob is not blocked.
- Bob's upload goes through normally.
- Alice's running batch continues unaffected.

---

## Server-Side Scheduler vs Client-Side Scheduler

The **server** does not fire schedules itself. It only:
- Stores schedule records in the database (cron expression, timezone, enabled flag, last_ran_at).
- Computes and returns the "next run" time for display in the UI.
- Accepts the `/running` and `/ran` signals from the client agent.
- Blocks browser uploads when `is_executing` is TRUE for that user.

The **client agent** is the one that actually wakes up and fires at the right time. If the client agent is not running on the user's machine, schedules will not fire. The server has no way to trigger the upload on its own.

This means: schedules are configured on the server, but executed by the agent on the user's machine. Both must be running for the scheduler to work.

---

## Quick Reference Table

| Situation | UI upload allowed? | Notes |
|---|---|---|
| Schedule is active, waiting for fire time | Yes | Nothing is happening yet |
| Schedule is running (agent uploading) | No — shows 409 "Scheduler" | Blocked until agent finishes |
| Schedule is stopped (disabled) | Yes | Schedule still exists, just won't fire |
| No schedule configured at all | Yes | Open uploading |
| Client A running, Client B trying to upload | Yes for B | Each user is fully isolated |
| Agent offline at scheduled time (>60s late) | Yes | Slot is skipped until tomorrow |
| Agent online, fires within 60s of configured time | n/a | Agent fires normally |
