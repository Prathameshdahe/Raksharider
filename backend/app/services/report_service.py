import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from app.database.supabase import supabase
from app.schemas.ai_report import AIReport

logger = logging.getLogger(__name__)

import uuid


class ReportService:

    @staticmethod
    def get_or_create_vehicle(number_plate: Optional[str]):
        """
        Get vehicle by number plate or create new one.
        Updates last_seen if vehicle exists.
        Args:
            number_plate: License plate number
        Returns:
            vehicle_id (str)
        Raises:
            RuntimeError: If vehicle operation fails
        """
        try:
            # If no plate detected, create an anonymous vehicle
            if not number_plate or number_plate.strip() == "":
                anonymous_plate = f"UNKNOWN_{str(uuid.uuid4())[:8]}"
                try:
                    response = supabase.table("vehicles").insert({
                        "number_plate": anonymous_plate,
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    return response.data[0]["id"]
                except Exception as e:
                    logger.exception(f"Failed to create anonymous vehicle: {str(e)}")
                    raise RuntimeError(f"Failed to create vehicle record: {str(e)}")

            # Check if vehicle exists
            response = supabase.table("vehicles") \
                .select("*") \
                .eq("number_plate", number_plate) \
                .execute()

            if response.data:
                # Vehicle exists - update last_seen
                vehicle = response.data[0]
                try:
                    supabase.table("vehicles") \
                        .update({
                            "last_seen": datetime.now(timezone.utc).isoformat()
                        }) \
                        .eq("id", vehicle["id"]) \
                        .execute()
                    logger.info(f"Vehicle {number_plate} found, updated last_seen")
                except Exception as e:
                    logger.warning(f"Failed to update last_seen for {number_plate}: {str(e)}")
                    # Don't fail entirely, still return vehicle_id

                return vehicle["id"]

            else:
                # Vehicle doesn't exist - create new
                response = supabase.table("vehicles").insert({
                    "number_plate": number_plate,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }).execute()
                logger.info(f"New vehicle created: {number_plate}")
                return response.data[0]["id"]

        except Exception as e:
            logger.exception(f"Vehicle lookup/create failed for {number_plate}: {str(e)}")
            raise RuntimeError(f"Failed to handle vehicle record: {str(e)}")

    @staticmethod
    def save_report(
        report: AIReport,
        video_filename: str,
        evidence_urls: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Save complete AI report to PostgreSQL with evidence URLs and violations.
        
        Args:
            report: AIReport object from AI service
            video_filename: Filename/path of uploaded video
            evidence_urls: List of uploaded evidence image URLs/paths
            
        Returns:
            dict with report_id, vehicle_id, and success message
            
        Raises:
            RuntimeError: If report save fails
        """
        try:
            # Get or create vehicle
            vehicle_id = ReportService.get_or_create_vehicle(report.number_plate)
            logger.info(f"Vehicle ID: {vehicle_id}")

            # Prepare evidence URLs as JSON (store as-is, let DB handle JSON type)
            evidence_data = evidence_urls if evidence_urls else []
            violations_data = report.violations_detected if report.violations_detected else []

            # Build report data
            report_data = {
                "vehicle_id": vehicle_id,
                "run_id": report.run_id,
                "video_url": video_filename,
                "status": report.status,
                "severity_score": report.severity_score,
                "rider_count": report.rider_count,
                "helmet_status": report.helmet_status,
                "number_plate": report.number_plate,
                "plate_read_confidence": report.plate_read_confidence,
                "evidence_urls": evidence_data,  # JSON array
                "frame_consistency_ratio": report.frame_consistency_ratio,
                "avg_yolo_confidence": report.avg_yolo_confidence,
                "ocr_agreement_ratio": report.ocr_agreement_ratio,
                "notes": report.notes,
                "generated_at": report.generated_at,
                "created_at": datetime.now(timezone.utc).isoformat()
            }

            logger.info(f"Inserting report data: {report_data}")

            # Insert report
            try:
                response = supabase.table("reports").insert(report_data).execute()
                logger.info(f"Insert response data: {response.data}")
                
                if not response.data or len(response.data) == 0:
                    logger.error(f"No data returned after insert. Full response: {response}")
                    raise RuntimeError("Insert succeeded but returned no data")
                
                report_id = response.data[0]["id"]
                logger.info(f"Report saved successfully, ID: {report_id}")
                
                return {
                    "message": "Report saved successfully",
                    "report_id": report_id,
                    "vehicle_id": vehicle_id
                }
            except Exception as insert_error:
                logger.exception(f"Database insert failed: {str(insert_error)}")
                raise RuntimeError(f"Failed to insert report: {str(insert_error)}")

        except Exception as e:
            logger.exception(f"Failed to save report: {str(e)}")
            raise RuntimeError(f"Failed to save report to database: {str(e)}")


