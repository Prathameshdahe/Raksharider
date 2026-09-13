import logging
from typing import Any, Dict, List, Optional

from app.database.supabase import supabase
from app.services.report_service import ReportService

logger = logging.getLogger(__name__)


class EvidenceService:

    @staticmethod
    def save_evidence(evidence) -> Dict[str, Any]:
        """Persist one evidence frame row and return the stored record."""
        try:
            payload = evidence.model_dump(exclude_none=True)
        except AttributeError:
            payload = evidence.dict(exclude_none=True)

        try:
            response = (
                supabase
                .table("evidence_frames")
                .insert(payload)
                .execute()
            )
            if not response.data:
                raise RuntimeError("Evidence frame insert returned no data")

            row = response.data[0]
            if "evidence_id" not in row and "id" in row:
                row = {**row, "evidence_id": row["id"]}
            return row
        except Exception as e:
            logger.exception("Failed to save evidence frame: %s", e)
            raise RuntimeError(f"Failed to save evidence frame: {str(e)}")

    @staticmethod
    def save_evidence_for_vehicle(
        vehicle_number: str,
        image_urls: List[str],
        video_id: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Save evidence image URLs for a vehicle, optionally tagged with location.

        Args:
            vehicle_number: License plate number
            image_urls:     List of evidence image URLs
            video_id:       Optional linked video ID
            latitude:       Optional GPS latitude for this evidence
            longitude:      Optional GPS longitude for this evidence

        Returns:
            List of inserted evidence row dicts
        """
        if not image_urls:
            raise ValueError("image_urls must not be empty")

        # get_or_create_vehicle returns a plain integer vehicle ID
        vehicle_id = ReportService.get_or_create_vehicle(vehicle_number)

        logger.info(
            f"Saving {len(image_urls)} evidence image(s) "
            f"for vehicle '{vehicle_number}' (id={vehicle_id})"
        )

        rows = [
            {
                "vehicle_id": vehicle_id,
                "video_id": video_id,
                "image_url": url,
                "latitude": latitude,
                "longitude": longitude,
            }
            for url in image_urls
        ]

        try:
            response = (
                supabase
                .table("evidence")
                .insert(rows)
                .execute()
            )

            if not response.data:
                raise RuntimeError("Evidence insert returned no data")

            logger.info(
                f"Saved {len(response.data)} evidence row(s) "
                f"for vehicle_id={vehicle_id}"
            )

            return response.data

        except Exception as e:
            logger.exception(f"Failed to save evidence: {str(e)}")
            raise RuntimeError(f"Failed to save evidence: {str(e)}")

    # ==============================================================
    # GET ALL VEHICLES
    # ==============================================================

    @staticmethod
    def get_all_vehicles() -> List[Dict[str, Any]]:
        """
        Return all vehicles on file.
        """
        try:
            response = (
                supabase
                .table("vehicles")
                .select("*")
                .order("last_seen", desc=True)
                .execute()
            )

            return response.data or []

        except Exception as e:
            logger.exception(f"Failed to fetch vehicles: {str(e)}")
            raise RuntimeError(f"Failed to fetch vehicles: {str(e)}")

    # ==============================================================
    # GET VEHICLE + EVIDENCE
    # ==============================================================

    @staticmethod
    def get_evidence_for_vehicle(
        number_plate: str,
    ) -> Dict[str, Any]:
        """
        Return a vehicle's information along with all
        evidence rows associated with that vehicle.

        Args:
            number_plate: Vehicle license plate number.

        Returns:
            Dictionary containing vehicle information
            and associated evidence.
        """
        number_plate = number_plate.strip().upper()

        try:
            vehicle_response = (
                supabase
                .table("vehicles")
                .select("*")
                .eq("number_plate", number_plate)
                .execute()
            )

            if not vehicle_response.data:
                raise ValueError(
                    f"No vehicle found with number_plate: {number_plate}"
                )

            vehicle = vehicle_response.data[0]

            evidence_response = (
                supabase
                .table("evidence")
                .select("*")
                .eq("vehicle_id", vehicle["id"])
                .order("created_at", desc=True)
                .execute()
            )

            return {
                "vehicle": vehicle,
                "evidence": evidence_response.data or [],
            }

        except ValueError:
            raise

        except Exception as e:
            logger.exception(
                f"Failed to fetch evidence for vehicle "
                f"{number_plate}: {str(e)}"
            )
            raise RuntimeError(
                f"Failed to fetch evidence for vehicle: {str(e)}"
            )
