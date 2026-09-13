import uuid
import logging
import os
from pathlib import Path

from fastapi import UploadFile

from app.database.supabase import supabase

logger = logging.getLogger(__name__)

# Local directory where videos will be saved until cloud storage is available
LOCAL_UPLOAD_DIR = Path("uploads/videos")


class StorageService:

    @staticmethod
    def save_locally(file: UploadFile) -> dict:
        """
        Save an uploaded video to the local filesystem.
        Used as a placeholder until Azure/AWS cloud storage is configured.

        Args:
            file: FastAPI UploadFile object

        Returns:
            dict with filename and local_path
        """
        try:
            LOCAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

            extension = (
                file.filename.rsplit(".", 1)[-1]
                if "." in file.filename
                else "mp4"
            )

            filename = f"{uuid.uuid4()}.{extension}"
            local_path = LOCAL_UPLOAD_DIR / filename

            try:
                file.file.seek(0)
            except Exception:
                pass

            file_bytes = file.file.read()

            with open(local_path, "wb") as f:
                f.write(file_bytes)

            logger.info(f"Video saved locally: {local_path}")

            return {
                "filename": filename,
                "local_path": str(local_path),
            }

        except Exception as e:
            logger.exception(
                f"Local video save failed: {str(e)}"
            )
            raise RuntimeError(
                f"Failed to save video locally: {str(e)}"
            )

    @staticmethod
    def upload_video(file: UploadFile) -> dict:
        """
        Upload a video to Supabase Storage 'videos' bucket.

        Args:
            file: FastAPI UploadFile object

        Returns:
            dict with filename and full storage path
        """
        try:
            extension = (
                file.filename.rsplit(".", 1)[-1]
                if "." in file.filename
                else "mp4"
            )

            filename = f"{uuid.uuid4()}.{extension}"

            try:
                file.file.seek(0)
            except Exception:
                pass

            file_bytes = file.file.read()

            res = supabase.storage.from_("videos").upload(
                filename,
                file_bytes,
                {
                    "content-type": file.content_type
                },
            )

            logger.info(
                f"Video uploaded successfully: {filename}"
            )

            return {
                "filename": filename,
                "video_path": f"videos/{filename}",
                "full_path": f"videos/{filename}",
            }

        except Exception as e:
            logger.exception(
                f"Video upload failed: {str(e)}"
            )
            raise RuntimeError(
                f"Failed to upload video: {str(e)}"
            )

    @staticmethod
    def upload_evidence(
        evidence_file_path: str,
        evidence_bucket: str = "evidence",
    ):
        """
        Upload evidence image file to Supabase Storage.

        Args:
            evidence_file_path: Local file path to evidence image
            evidence_bucket: Target bucket name

        Returns:
            dict with filename and storage path,
            or None if file doesn't exist
        """
        try:
            if not os.path.exists(evidence_file_path):
                logger.warning(
                    f"Evidence file not found: {evidence_file_path}"
                )
                return None

            original_name = Path(evidence_file_path).name

            extension = (
                original_name.rsplit(".", 1)[-1]
                if "." in original_name
                else "jpg"
            )

            filename = f"{uuid.uuid4()}.{extension}"

            with open(evidence_file_path, "rb") as f:
                file_bytes = f.read()

            content_type = "image/jpeg"

            if extension.lower() == "png":
                content_type = "image/png"
            elif extension.lower() == "gif":
                content_type = "image/gif"
            elif extension.lower() == "webp":
                content_type = "image/webp"

            res = supabase.storage.from_(evidence_bucket).upload(
                filename,
                file_bytes,
                {
                    "content-type": content_type
                },
            )

            logger.info(
                f"Evidence uploaded successfully: {filename}"
            )

            return {
                "filename": filename,
                "evidence_path": (
                    f"{evidence_bucket}/{filename}"
                ),
                "full_path": (
                    f"{evidence_bucket}/{filename}"
                ),
            }

        except Exception as e:
            logger.exception(
                f"Evidence upload failed for "
                f"{evidence_file_path}: {str(e)}"
            )

            # Keep existing behavior:
            # one failed evidence upload doesn't break the batch.
            return None

    @staticmethod
    def upload_evidence_batch(
        evidence_file_paths: list,
        evidence_bucket: str = "evidence",
    ):
        """
        Upload multiple evidence files to Supabase Storage.

        Args:
            evidence_file_paths: List of local file paths
            evidence_bucket: Target bucket name

        Returns:
            List of successfully uploaded evidence files
        """
        uploaded_evidence = []

        for path in evidence_file_paths:
            result = StorageService.upload_evidence(
                path,
                evidence_bucket,
            )

            if result:
                uploaded_evidence.append(result)

        return uploaded_evidence

    @staticmethod
    def get_public_url(
        file_path: str,
        bucket: str = "videos",
    ):
        """
        Generate a public URL for a file in Supabase Storage.

        Args:
            file_path: Path to file
            bucket: Storage bucket

        Returns:
            Public URL string, or None on failure
        """
        try:
            if file_path.startswith(f"{bucket}/"):
                file_path = file_path[
                    len(bucket) + 1:
                ]

            url = (
                supabase
                .storage
                .from_(bucket)
                .get_public_url(file_path)
            )

            return url

        except Exception as e:
            logger.exception(
                f"Failed to generate public URL "
                f"for {file_path}: {str(e)}"
            )
            return None

    @staticmethod
    def create_signed_upload_url(
        filename: str,
    ) -> dict:
        """
        Generate a UNIQUE Supabase signed upload URL
        for direct client upload to the 'videos' bucket.

        The original filename is NOT used as the storage
        filename. Only its extension is preserved.

        Example:

            Input:
                test2.mp4

            Storage path:
                7f8c2e91-4a12-4c7e-9d31-abc123456789.mp4

        This prevents duplicate-file conflicts when the
        same filename is uploaded multiple times.

        Args:
            filename: Original filename from the client.

        Returns:
            dict containing:
                - storage_path
                - upload_url
                - token
        """
        try:
            # Extract original extension.
            extension = Path(filename).suffix.lower()

            # Fallback to .mp4 if no extension is provided.
            if not extension:
                extension = ".mp4"

            # Generate a unique storage filename.
            unique_filename = (
                f"{uuid.uuid4()}{extension}"
            )

            logger.info(
                f"Creating signed upload URL for "
                f"storage path: {unique_filename}"
            )

            response = (
                supabase
                .storage
                .from_("videos")
                .create_signed_upload_url(
                    unique_filename
                )
            )

            return {
                "storage_path": unique_filename,
                "upload_url": response["signed_url"],
                "token": response.get("token"),
            }

        except Exception as e:
            logger.exception(
                "Failed to create signed upload URL: "
                f"{str(e)}"
            )

            raise RuntimeError(
                "Failed to create signed upload URL: "
                f"{str(e)}"
            )