from app.database.supabase import supabase

# small test upload
filename = 'test_upload_sample.mp4'
file_bytes = b'This is a test file for upload.'
try:
    res = supabase.storage.from_('videos').upload(filename, file_bytes, {'content-type': 'video/mp4'})
    print('Upload response:', res)
except Exception as e:
    print('Upload error:', type(e), e)
