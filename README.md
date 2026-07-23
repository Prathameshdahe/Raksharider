# RakshaRide 🛡️

**Campus Two-Wheeler Safety & Violation Reporting System**  
Bharati Vidyapeeth College of Engineering, Pune

---

## Project Structure

```
RakshaRide/
├── backend/          # Python FastAPI — AI pipeline, REST API, SQLite DB
├── frontend/         # Android (Kotlin + Jetpack Compose) — Citizen Reporter App
├── admin-portal/     # Web Dashboard (HTML/React) — Admin Review Console
└── start.ps1         # One-click local launcher (Windows PowerShell)
```

## Three-Tier Architecture

| Tier | Stack | Role |
|------|-------|------|
| **1 — Field** | Android (Kotlin + Compose + Firebase) | Citizen captures & submits violations |
| **2 — AI Processing** | FastAPI + YOLOv8 + EasyOCR stubs | License plate OCR, helmet/triple-riding classification |
| **3 — Control Center** | HTML/JS Admin Portal | Officer review, approve/reject, SMS dispatch |

---

## Quick Start (Local)

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Admin Portal
```bash
cd admin-portal
python -m http.server 8080
# Open http://localhost:8080
```

### One-click launcher (Windows)
```powershell
.\start.ps1
```

---

## Seeded Test Credentials

| Role | Email | Password |
|------|-------|----------|
| Admin | admin@raksharide.edu.in | Admin@1234 |
| Auditor | auditor@raksharide.edu.in | Auditor@1234 |
| Citizen | citizen@raksharide.edu.in | Citizen@1234 |

---

## Android App Setup

1. Open `frontend/` in Android Studio
2. Add your `google-services.json` from Firebase Console into `frontend/app/`
3. Run Gradle sync → Build → Run on emulator or device

> ⚠️ `google-services.json` is excluded from git. Each developer must add their own from the shared Firebase project.

---

## Violation Categories

- 🪖 **No Helmet** — Rider without helmet
- 👥 **Triple Riding** — More than 2 persons on a two-wheeler
- ⚡ **Rash Driving** — Dangerous / aggressive driving behavior

---

## Tech Stack

- **Backend:** Python 3.11, FastAPI, SQLAlchemy, YOLOv8 (stubbed), EasyOCR (stubbed)
- **Mobile:** Kotlin, Jetpack Compose, Material 3, Firebase Auth + Firestore + Storage, Hilt, CameraX
- **Web:** Vanilla HTML/CSS/JS, Chart.js
- **Deployment:** Render (backend), GitHub Pages (admin portal optional)
