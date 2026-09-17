from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.uploads import router as videos_router
from app.routes.vehicles import router as vehicles_router
from app.routes.auth import router as auth_router
from app.routes.review import router as review_router
from app.routes.admin import router as admin_router
from app.routes.cases import router as cases_router
from app.routes.plates import router as plates_router
from app.routes.evidence import router as evidence_router

app = FastAPI(
    title="DriveTrust Backend",
    version="1.0.0"
)

# Allow the PWA (any localhost port, and the deployed Vercel URL) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Local development
        "http://localhost:5051",
        "http://127.0.0.1:5051",
        "http://localhost:3000",
        # Vercel production + preview deployments
        "https://raksharider.vercel.app",
        "https://raksharider-*.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth_router)
app.include_router(videos_router)
app.include_router(vehicles_router)
app.include_router(review_router)
app.include_router(admin_router)
app.include_router(cases_router)
app.include_router(plates_router)
app.include_router(evidence_router)


@app.get("/")
def root():
    return {
        "message": "DriveTrust Backend Running 🚗"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "message": "Backend is running"
    }