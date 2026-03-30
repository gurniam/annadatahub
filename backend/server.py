from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import bcrypt
import jwt
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="AnnadataHub API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB
mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'annadatahub')]

JWT_SECRET = os.environ.get('JWT_SECRET_KEY', 'annadatahub-secret')

class UserRegister(BaseModel):
    email: str
    password: str
    full_name: str
    phone: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str

@app.get("/")
async def root():
    return {"message": "AnnadataHub API is running!", "status": "ok"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "message": "AnnadataHub is live!"}

@app.post("/api/auth/register")
async def register(user: UserRegister):
    existing = await db.users.find_one({"email": user.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt())
    user_id = str(uuid.uuid4())
    
    await db.users.insert_one({
        "_id": user_id,
        "email": user.email,
        "password": hashed.decode(),
        "full_name": user.full_name,
        "phone": user.phone,
        "plan": "free",
        "created_at": datetime.utcnow().isoformat()
    })
    
    token = jwt.encode({"user_id": user_id, "email": user.email}, JWT_SECRET, algorithm="HS256")
    return {"token": token, "user": {"id": user_id, "email": user.email, "full_name": user.full_name, "plan": "free"}}

@app.post("/api/auth/login")
async def login(user: UserLogin):
    db_user = await db.users.find_one({"email": user.email})
    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not bcrypt.checkpw(user.password.encode(), db_user["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = jwt.encode({"user_id": db_user["_id"], "email": user.email}, JWT_SECRET, algorithm="HS256")
    return {"token": token, "user": {"id": db_user["_id"], "email": user.email, "full_name": db_user["full_name"], "plan": db_user.get("plan", "free")}}

@app.get("/api/mandi-prices")
async def mandi_prices(crop: str = "wheat", state: str = "Punjab"):
    return {
        "crop": crop,
        "state": state,
        "prices": [
            {"market": "Ludhiana Mandi", "price": 2150, "unit": "per quintal"},
            {"market": "Amritsar Mandi", "price": 2180, "unit": "per quintal"},
            {"market": "Patiala Mandi", "price": 2140, "unit": "per quintal"},
        ],
        "msp": 2275,
        "date": datetime.utcnow().strftime("%d %b %Y")
    }

@app.get("/api/weather")
async def weather(location: str = "Punjab"):
    return {
        "location": location,
        "temperature": 28,
        "humidity": 65,
        "forecast": "Sunny",
        "advice": "Good day for spraying. Avoid afternoon hours.",
        "rain_chance": 20
    }
