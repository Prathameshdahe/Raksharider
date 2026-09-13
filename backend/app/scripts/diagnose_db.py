"""
Database diagnostic script - identify insert failures
"""
import json
import uuid
from datetime import datetime, timezone
from app.database.supabase import supabase

print("\n" + "="*80)
print("DriveTrust - Database Diagnostic")
print("="*80 + "\n")

# Step 1: Check vehicles table structure
print("[STEP 1] Checking 'vehicles' table structure...")
print("-" * 80)
try:
    # Get table schema by attempting a query
    response = supabase.table("vehicles").select("*").limit(1).execute()
    print("✓ Table 'vehicles' exists and is accessible")
    if response.data:
        print(f"  Sample record columns: {list(response.data[0].keys())}")
    else:
        print("  Table is empty (no sample records)")
except Exception as e:
    print(f"✗ Error accessing vehicles table: {str(e)}")

# Step 2: Check reports table structure
print("\n[STEP 2] Checking 'reports' table structure...")
print("-" * 80)
try:
    response = supabase.table("reports").select("*").limit(1).execute()
    print("✓ Table 'reports' exists and is accessible")
    if response.data:
        print(f"  Sample record columns: {list(response.data[0].keys())}")
    else:
        print("  Table is empty (no sample records)")
except Exception as e:
    print(f"✗ Error accessing reports table: {str(e)}")

# Step 3: Try inserting a test vehicle
print("\n[STEP 3] Testing vehicle INSERT...")
print("-" * 80)
try:
    test_plate = f"TEST_DIAG_{int(datetime.now().timestamp())}"
    test_vehicle_data = {
        "number_plate": test_plate,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    print(f"Attempting to insert: {test_vehicle_data}")
    response = supabase.table("vehicles").insert(test_vehicle_data).execute()
    print(f"✓ Vehicle INSERT successful")
    print(f"  Returned vehicle_id: {response.data[0]['id']}")
    test_vehicle_id = response.data[0]['id']
except Exception as e:
    print(f"✗ Vehicle INSERT failed: {str(e)}")
    test_vehicle_id = None

# Step 4: Try inserting a test report
print("\n[STEP 4] Testing report INSERT...")
print("-" * 80)
if test_vehicle_id:
    try:
        test_report_data = {
            "vehicle_id": test_vehicle_id,
            "run_id": f"test_run_{uuid.uuid4()}",
            "video_filename": "test_video.mp4",
            "status": "test",
            "severity_score": 5.0,
            "violations_detected": ["test_violation"],
            "rider_count": 1,
            "helmet_status": "unknown",
            "number_plate": test_plate,
            "plate_read_confidence": 0.5,
            "evidence_urls": ["test_image.jpg"],
            "frame_consistency_ratio": 0.9,
            "avg_yolo_confidence": 0.8,
            "ocr_agreement_ratio": 0.85,
            "notes": "Diagnostic test",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        print(f"Attempting to insert report with columns: {list(test_report_data.keys())}")
        response = supabase.table("reports").insert(test_report_data).execute()
        print(f"✓ Report INSERT successful")
        print(f"  Returned report_id: {response.data[0]['id']}")
    except Exception as e:
        print(f"✗ Report INSERT failed: {str(e)}")
        print(f"\n  Troubleshooting tips:")
        error_str = str(e).lower()
        if "column" in error_str and "not found" in error_str:
            print(f"    → Column name mismatch. Check your table schema.")
        elif "foreign key" in error_str:
            print(f"    → Foreign key constraint failed. Vehicle might not exist.")
        elif "permission" in error_str:
            print(f"    → Permission denied. Service role needs INSERT permission.")
        elif "json" in error_str or "jsonb" in error_str:
            print(f"    → JSON type error. Violation/evidence columns should be JSON/JSONB type.")
        else:
            print(f"    → Check your table schema and column types.")
else:
    print("⊘ Skipped: vehicle insert failed, so cannot test foreign key")

# Step 5: List actual columns in reports table
print("\n[STEP 5] Attempting to query reports table info...")
print("-" * 80)
try:
    response = supabase.table("reports").select("*").limit(0).execute()
    print("✓ Reports table accessible")
except Exception as e:
    print(f"✗ Cannot access reports table: {str(e)}")

print("\n" + "="*80)
print("Diagnostic Complete")
print("="*80 + "\n")

print("NEXT STEPS:")
print("1. If any inserts failed, check the Supabase SQL Editor")
print("2. Verify table schemas match the SETUP_GUIDE.md")
print("3. Ensure service_role has SELECT, INSERT, UPDATE permissions")
print("4. Check that all required columns exist with correct data types")
print("")
