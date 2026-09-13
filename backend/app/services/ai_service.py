import logging
import requests
from typing import Optional

from app.schemas.ai_report import AIReport

logger = logging.getLogger(__name__)


class AIService:

    AI_SERVICE_URL = "http://127.0.0.1:8001"
    ANALYZE_ENDPOINT = f"{AI_SERVICE_URL}/analyze"

    @staticmethod
    def analyze_video(file, timeout: Optional[int] = None) -> AIReport:
        """
        Send video to AI detection service and get analysis report.
        
        Args:
            file: FastAPI UploadFile object with video
            timeout: Request timeout in seconds (default: 300s)
            
        Returns:
            AIReport object with detection results and evidence frame paths
            
        Raises:
            RuntimeError: If AI service is unavailable or returns invalid response
        """
        if timeout is None:
            timeout = 300  # 5 minutes default for video processing

        try:
            # Ensure file pointer is at start
            try:
                file.file.seek(0)
            except Exception:
                pass

            logger.info(f"Sending video to AI service: {file.filename}")

            # Send to AI service
            response = requests.post(
                AIService.ANALYZE_ENDPOINT,
                files={
                    "file": (
                        file.filename,
                        file.file,
                        file.content_type
                    )
                },
                timeout=timeout
            )

            # Check for HTTP errors
            response.raise_for_status()

            # Parse and validate response
            ai_response_data = response.json()
            logger.info(f"AI service returned response: {ai_response_data.get('run_id', 'unknown')}")

            # Convert to AIReport (validates schema)
            ai_report = AIReport(**ai_response_data)
            return ai_report

        except requests.exceptions.ConnectionError as e:
            logger.exception("Failed to connect to AI service")
            raise RuntimeError(
                f"AI service is unavailable at {AIService.ANALYZE_ENDPOINT}. "
                f"Ensure the AI detection service is running."
            )
        except requests.exceptions.Timeout as e:
            logger.exception("AI service request timed out")
            raise RuntimeError(
                f"AI service processing timed out after {timeout} seconds. "
                f"Video may be too large or service is slow."
            )
        except requests.exceptions.HTTPError as e:
            logger.exception(f"AI service returned HTTP error: {e.response.status_code}")
            raise RuntimeError(
                f"AI service error ({e.response.status_code}): {e.response.text}"
            )
        except ValueError as e:
            logger.exception("AI service response is not valid JSON")
            raise RuntimeError(f"AI service returned invalid response format: {str(e)}")
        except Exception as e:
            logger.exception(f"Unexpected error during AI analysis: {str(e)}")
            raise RuntimeError(f"Unexpected error during AI analysis: {str(e)}")