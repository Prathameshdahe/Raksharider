from typing import Literal, Optional 
from datetime import datetime 
from pydantic import BaseModel

class VideoUploadResponse(BaseModel):
    success: bool
    message: str
    video_id: str 
    filename: str 
    status: str


class VideoRecord (BaseModel):
    id: str
    filename: str
    original_name: Optional[str] = None
    local_path: Optional[str] = None
    blob_url: Optional[str] = None
    status: str
    uploaded_by: Optional[str] = None 
    uploaded_at: Optional[str] = None


class VideoStatusUpdate(BaseModel):
    status: Literal["processing", "processed", "failed"]
    error_message: Optional[str] = None 
    
    