# DriveTrust AI - Backend POC Setup Guide

## ✅ What's Been Built

Complete end-to-end backend workflow with production-like architecture:

### Architecture Overview

```
User Upload (FastAPI)
    ↓
1. StorageService: Upload video to Supabase bucket "videos"
    ↓
2. AIService: Send video to AI detection service (http://127.0.0.1:8001/analyze)
    ↓
3. StorageService: Upload all evidence images to Supabase bucket "evidence"
    ↓
4. ReportService: Save everything to PostgreSQL with proper schema
    - vehicles table (with last_seen deduplication)
    - reports table (with JSON support for arrays)
    ↓
5. Return complete JSON response with all data
```

### Updated Services

#### 1. **StorageService** (`app/services/storage_service.py`)
- `upload_video(file)` - Upload traffic video to "videos" bucket
- `upload_evidence(file_path)` - Upload individual evidence image to "evidence" bucket
- `upload_evidence_batch(file_paths)` - Upload multiple evidence images in batch
- `get_public_url(file_path, bucket)` - Generate public URLs for stored files
- Full error handling and logging

#### 2. **AIService** (`app/services/ai_service.py`)
- `analyze_video(file, timeout)` - Send video to AI service with timeout handling
- Validates AI response against AIReport schema
- Comprehensive error handling for:
  - Connection errors
  - Timeout errors
  - Invalid responses
  - HTTP errors

#### 3. **ReportService** (`app/services/report_service.py`)
- `get_or_create_vehicle(number_plate)` - Handles vehicle deduplication
  - Checks if vehicle exists by number plate
  - Updates `last_seen` if exists
  - Creates new vehicle if doesn't exist
  - Handles anonymous vehicles if no plate detected
- `save_report(report, video_filename, evidence_urls)` - Saves complete report
  - Stores all AI report data
  - Stores evidence image URLs as JSON array
  - Stores violations as JSON array
  - Includes timestamps

#### 4. **Upload Route** (`app/routes/uploads.py`)
- Complete orchestration endpoint: `POST /upload/`
- Implements all 5 workflow steps with proper error handling
- Returns comprehensive response with:
  - Video filename and storage path
  - Complete AI report data
  - Evidence image count and paths
  - Database report_id and vehicle_id
- Detailed error responses with step information

### Database Schema Requirements

Your Supabase PostgreSQL needs these tables:

#### `vehicles` table
```sql
CREATE TABLE public.vehicles (
  id BIGSERIAL PRIMARY KEY,
  number_plate TEXT NOT NULL UNIQUE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
  last_seen TIMESTAMP WITH TIME ZONE
);
```

#### `reports` table
```sql
CREATE TABLE public.reports (
  id BIGSERIAL PRIMARY KEY,
  vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
  run_id TEXT NOT NULL UNIQUE,
  video_filename TEXT NOT NULL,
  
  -- AI Report Data
  status TEXT NOT NULL,
  severity_score FLOAT NOT NULL,
  violations_detected JSONB,  -- JSON array of violations
  
  rider_count INTEGER,
  helmet_status TEXT,
  number_plate TEXT,
  plate_read_confidence FLOAT,
  
  -- Evidence URLs
  evidence_urls JSONB,  -- JSON array of evidence image paths
  
  -- Quality Metrics
  frame_consistency_ratio FLOAT,
  avg_yolo_confidence FLOAT,
  ocr_agreement_ratio FLOAT,
  
  notes TEXT,
  generated_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Create index on vehicle_id for faster lookups
CREATE INDEX idx_reports_vehicle_id ON public.reports(vehicle_id);
```

### Required Supabase Configuration

#### 1. **Storage Buckets**
Create two public storage buckets in Supabase:
- `videos` - for uploaded traffic violation videos
- `evidence` - for AI-detected evidence frame images

#### 2. **Database Permissions (CRITICAL)**

Your service role needs permissions on the `vehicles` and `reports` tables. Run these SQL commands in Supabase SQL Editor:

```sql
-- Grant permissions on vehicles table
GRANT SELECT, INSERT, UPDATE ON public.vehicles TO service_role;
GRANT USAGE ON SEQUENCE public.vehicles_id_seq TO service_role;

-- Grant permissions on reports table
GRANT SELECT, INSERT, UPDATE ON public.reports TO service_role;
GRANT USAGE ON SEQUENCE public.reports_id_seq TO service_role;

-- Optional: Enable RLS (Row Level Security) if needed
ALTER TABLE public.vehicles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;

-- Create policies for service role (if RLS is enabled)
CREATE POLICY "service_role_full_access_vehicles" ON public.vehicles
  FOR ALL USING (true) WITH CHECK (true)
  TO service_role;

CREATE POLICY "service_role_full_access_reports" ON public.reports
  FOR ALL USING (true) WITH CHECK (true)
  TO service_role;
```

---

## 🚀 Running the POC

### Prerequisites

1. **AI Detection Service** running at `http://127.0.0.1:8001/analyze`
   - Must accept POST with `file` parameter
   - Must return JSON matching `AIReport` schema

2. **Supabase Setup**
   - `.env` file with correct credentials (already fixed)
   - Storage buckets created (`videos`, `evidence`)
   - Database tables with permissions granted

### Start the Backend

```powershell
# Activate virtual environment (if not already active)
venv\Scripts\Activate.ps1

# Start FastAPI server
python -m uvicorn app.main:app --reload
```

Server runs at: `http://127.0.0.1:8000`

### Test Upload Endpoint

#### Using Python
```python
import requests

with open('traffic_violation.mp4', 'rb') as f:
    files = {'file': f}
    response = requests.post(
        'http://127.0.0.1:8000/upload/',
        files=files,
        timeout=300
    )
    print(response.json())
```

#### Using cURL
```bash
curl -X POST "http://127.0.0.1:8000/upload/" \
  -F "file=@traffic_violation.mp4"
```

### Expected Success Response

```json
{
  "success": true,
  "message": "Video analysis complete and stored successfully",
  "data": {
    "video": {
      "filename": "abc123-xyz.mp4",
      "path": "videos/abc123-xyz.mp4"
    },
    "ai_report": {
      "run_id": "ai_run_12345",
      "status": "traffic_violation_detected",
      "severity_score": 8.5,
      "violations_detected": [
        "no_helmet",
        "improper_lane_change"
      ],
      "number_plate": "MH-12-AB-1234",
      "plate_read_confidence": 0.95,
      "rider_count": 2,
      "helmet_status": "none_detected",
      "notes": "Flagrant traffic violation observed"
    },
    "evidence": {
      "count": 3,
      "image_paths": [
        "evidence/frame_001.jpg",
        "evidence/frame_002.jpg",
        "evidence/frame_003.jpg"
      ]
    },
    "database": {
      "report_id": 42,
      "vehicle_id": 7
    }
  }
}
```

---

## Admin Login and Google OAuth Setup

The frontend admin gate is configured for:

```text
prathameshdahe1@gmail.com
```

Do not hardcode the admin password in frontend code. Put it in `backend/.env`:

```env
ADMIN_EMAIL=prathameshdahe1@gmail.com
ADMIN_PASSWORD=your_strong_admin_password_here
```

Then run the auth/profile SQL in Supabase SQL Editor:

```text
backend/database/auth_admin_setup.sql
```

After the SQL is applied, create or promote the admin account from the backend service role:

```powershell
cd backend
venv\Scripts\Activate.ps1
python -m app.scripts.create_admin_user
```

In Supabase Dashboard, enable Google under **Authentication > Providers > Google** and add your frontend URL under **Authentication > URL Configuration > Redirect URLs**. For local testing, include `http://localhost:3000` or whichever static-server port you use.

GitHub login and guest access are intentionally removed. Users must sign in with email/password or Google.

---

## 📊 Testing the Complete Flow

Run the end-to-end test to verify architecture (requires Supabase permissions configured):

```powershell
# Activate venv first
venv\Scripts\Activate.ps1

# Run test
python -m app.scripts.test_end_to_end
```

---

## 🔍 Monitoring and Debugging

### View Logs
Logs from each service layer are emitted to stdout:
- `StorageService.upload_video` - Supabase upload progress
- `AIService.analyze_video` - AI service communication
- `ReportService.save_report` - Database operations
- Upload route - Full workflow orchestration

### Common Issues

**Issue: "permission denied for table vehicles"**
- **Cause:** Service role doesn't have permissions
- **Fix:** Run the SQL GRANT commands above in Supabase

**Issue: "AI service is unavailable"**
- **Cause:** AI detection service not running at `http://127.0.0.1:8001`
- **Fix:** Start the AI service first

**Issue: "Failed to upload video"**
- **Cause:** Supabase storage permission or bucket missing
- **Fix:** Ensure buckets exist and service role has write permissions

**Issue: "Invalid JWS Protected Header"**
- **Cause:** Malformed authentication key
- **Fix:** Verify `.env` contains correct `SUPABASE_SERVICE_ROLE_KEY` (no extra characters)

---

## 📋 Architecture Principles

✅ **Separation of Concerns**
- StorageService: Only handles file uploads
- AIService: Only communicates with AI
- ReportService: Only manages database
- Upload route: Orchestrates workflow

✅ **Error Handling**
- Each service catches and logs errors
- Partial failures don't block entire flow (e.g., missing evidence)
- Clear error messages with context

✅ **Database Best Practices**
- Vehicle deduplication by number plate
- `last_seen` updates reduce duplicates
- JSON fields for flexible data (violations, evidence)
- Foreign key constraints (reports → vehicles)

✅ **File Handling**
- Proper stream management (seek when needed)
- Unique filenames with UUID to avoid conflicts
- Support for batch uploads

---

## 🎯 Next Enhancements

1. **Authentication** - Add API key or JWT validation to `/upload/` endpoint
2. **Rate Limiting** - Prevent abuse with rate limiting middleware
3. **Async Processing** - Use Celery/Redis for long-running AI analysis
4. **Webhooks** - Notify external systems when reports complete
5. **Search/Filter** - Add endpoints to query reports by vehicle/date/severity
6. **Dashboard** - Frontend UI to view violations and evidence

---

## ✨ You're all set!

The complete DriveTrust POC backend is ready. Just ensure:
1. ✓ AI service is running
2. ✓ Supabase tables created
3. ✓ Database permissions granted
4. ✓ Storage buckets exist

Then start the server and upload!
