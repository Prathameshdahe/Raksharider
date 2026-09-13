from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.uploads import router as videos_router
from app.routes.vehicles import router as vehicles_router
from app.routes.auth import router as auth_router

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
        # Vercel deployment — update these after deploying frontend
        "https://roadwatch-ai.vercel.app",
        "https://roadwatch-ai-*.vercel.app",    # preview deployments
        # ↑ STEP: replace with your exact Vercel URL after first deploy
        # Then remove the wildcard below and redeploy backend.
        "*",  # TEMP: remove after adding exact Vercel URL above
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth_router)
app.include_router(videos_router)
app.include_router(vehicles_router)

try:
    from app.routes.vehicle_records import router as vehicle_records_router
    app.include_router(vehicle_records_router)
except ImportError:
    pass


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