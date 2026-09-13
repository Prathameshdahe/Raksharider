from io import BytesIO
from types import SimpleNamespace
from app.services.storage_service import StorageService

# Mock UploadFile-like object
file_obj = SimpleNamespace()
file_obj.filename = 'service_test.mp4'
file_obj.content_type = 'video/mp4'
file_obj.file = BytesIO(b'Test bytes for service upload')

res = StorageService.upload_video(file_obj)
print('Service upload result:', res)
