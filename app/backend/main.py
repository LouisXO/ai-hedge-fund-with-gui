from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.backend.routes import api_router

app = FastAPI(title="AI Hedge Fund API", description="Backend API for AI Hedge Fund", version="0.1.0")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    # allow_origins=[
    #     "http://localhost:5173",  # Development
    #     "https://hedge-fund.louisleng.com",  # Production frontend
    #     "https://api.hedge-fund.louisleng.com",  # API domain
    #     # "https://your-custom-domain.com",  # Custom domain
    # ],
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Include all routes
app.include_router(api_router)
