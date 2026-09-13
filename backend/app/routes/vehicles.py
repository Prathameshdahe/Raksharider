import logging

from fastapi import APIRouter, HTTPException, status, Depends

from app.schemas.evidence import EvidenceCreateRequest, EvidenceCreateResponse
from app.services.evidence_service import EvidenceService
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/vehicles",
    tags=["Vehicles"],
)


# ==============================================================
# POST /vehicles/{vehicle_number}/evidence
# Save evidence images against a vehicle number plate
# ==============================================================
@router.post(
    "/{vehicle_number}/evidence",
    response_model=EvidenceCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def save_vehicle_evidence(
    vehicle_number: str,
    body: EvidenceCreateRequest,
    current_user=Depends(get_current_user),
):
    """
    Save an array of evidence images against a vehicle identified
    by its number plate.

    - If the vehicle does not exist, it is created automatically.
    - Each image URL in the array is stored as a separate evidence row.
    - Optionally link to an existing video record via `video_id`.
    """
    vehicle_number = vehicle_number.strip().upper()

    if not vehicle_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": "vehicle_number must not be empty",
            },
        )

    if not body.image_urls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": "image_urls must not be empty",
            },
        )

    try:
        logger.info(
            f"Saving evidence for vehicle '{vehicle_number}' "
            f"— {len(body.image_urls)} image(s)"
        )

        rows = EvidenceService.save_evidence_for_vehicle(
            vehicle_number=vehicle_number,
            image_urls=body.image_urls,
            video_id=body.video_id,
            latitude=body.latitude,
            longitude=body.longitude,
        )

        return EvidenceCreateResponse(
            success=True,
            vehicle_id=rows[0]["vehicle_id"],
            vehicle_number=vehicle_number,
            evidence_count=len(rows),
            saved_urls=[r["image_url"] for r in rows],
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": str(e),
            },
        )

    except Exception as e:
        logger.exception(
            f"Failed to save evidence for '{vehicle_number}': {str(e)}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to save evidence",
                "detail": str(e),
            },
        )


# ==============================================================
# GET /vehicles
# Get all vehicles
# ==============================================================
@router.get("")
async def list_vehicles(
    current_user=Depends(get_current_user),
):
    """
    Return all vehicles on file.
    """
    try:
        vehicles = EvidenceService.get_all_vehicles()

        return {
            "success": True,
            "count": len(vehicles),
            "data": vehicles,
        }

    except Exception as e:
        logger.exception(
            f"Failed to fetch vehicles: {str(e)}"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to fetch vehicles",
                "detail": str(e),
            },
        )


# ==============================================================
# GET /vehicles/{number_plate}/evidence
# Get vehicle information + all evidence
# ==============================================================
@router.get("/{number_plate}/evidence")
async def get_vehicle_evidence(
    number_plate: str,
    current_user=Depends(get_current_user),
):
    """
    Return vehicle information along with all evidence
    associated with that vehicle.
    """
    try:
        result = EvidenceService.get_evidence_for_vehicle(
            number_plate=number_plate
        )

        return {
            "success": True,
            "data": result,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "success": False,
                "error": str(e),
            },
        )

    except Exception as e:
        logger.exception(
            f"Failed to fetch evidence for vehicle "
            f"'{number_plate}': {str(e)}"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to fetch vehicle evidence",
                "detail": str(e),
            },
        )