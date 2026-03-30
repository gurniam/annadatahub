
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import bcrypt
import jwt
import httpx
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="AnnadataHub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.environ.get('DB_NAME', 'annadatahub')
JWT_SECRET = os.environ.get('JWT_SECRET_KEY', 'annadatahub-secret')
CLAUDE_API_KEY = os.environ.get('EMERGENT_LLM_KEY', '')

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

class UserRegister(BaseModel):
    email: str
    password: str
    full_name: str
    phone: Optional[str] = None
    state: Optional[str] = None
    language: Optional[str] = "en"

class UserLogin(BaseModel):
    email: str
    password: str

class CropScanRequest(BaseModel):
    image_base64: str
    crop_type: Optional[str] = None
    language: Optional[str] = "en"

class AIQuery(BaseModel):
    question: str
    language: Optional[str] = "en"

class FarmGramPost(BaseModel):
    content: str
    crop_type: Optional[str] = None
    location: Optional[str] = None

def create_token(user_id: str, email: str) -> str:
    payload = {"user_id": user_id, "email": email, "exp": datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

async def call_claude(prompt: str, system: str = "") -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "system": system or "You are AnnadataHub AI assistant for Indian farmers.", "messages": [{"role": "user", "content": prompt}]}
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"AI temporarily unavailable. Please try again."

async def call_claude_vision(image_base64: str, prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}}, {"type": "text", "text": prompt}]}]}
            )
            return r.json()["content"][0]["text"]
    except Exception as e:
        return f"Vision AI temporarily unavailable."

@app.get("/")
async def root():
    return {"message": "AnnadataHub API is running! 🌾", "status": "ok"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "message": "AnnadataHub backend is live!"}

@app.post("/api/auth/register")
async def register(user: UserRegister):
    existing = await db.users.find_one({"email": user.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    hashed = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt()).decode()
    user_id = str(uuid.uuid4())
    await db.users.insert_one({"_id": user_id, "email": user.email, "password": hashed, "full_name": user.full_name, "phone": user.phone, "state": user.state, "plan": "free", "scan_count": 0, "created_at": datetime.utcnow().isoformat()})
    return {"token": create_token(user_id, user.email), "user": {"id": user_id, "email": user.email, "full_name": user.full_name, "plan": "free"}}

@app.post("/api/auth/login")
async def login(user: UserLogin):
    db_user = await db.users.find_one({"email": user.email})
    if not db_user or not bcrypt.checkpw(user.password.encode(), db_user["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": create_token(db_user["_id"], user.email), "user": {"id": db_user["_id"], "email": user.email, "full_name": db_user["full_name"], "plan": db_user.get("plan", "free")}}

@app.post("/api/crop/scan")
async def scan_crop(request: CropScanRequest, authorization: str = Header(None)):
    prompt = f"""You are an expert agricultural scientist for Indian farmers.
Analyze this crop image and provide in JSON format:
{{
  "disease": "disease name or Healthy",
  "severity": "Low/Medium/High/None",
  "crop": "crop type",
  "confidence": 85,
  "treatment": "treatment description",
  "medicine": "medicine name",
  "dosage": "dosage per litre",
  "prevention": "prevention tips",
  "urgency": "Immediate/Within 7 days/No action needed"
}}"""
    result = await call_claude_vision(request.image_base64, prompt)
    if authorization:
        try:
            payload = verify_token(authorization.replace("Bearer ", ""))
            await db.scans.insert_one({"_id": str(uuid.uuid4()), "user_id": payload["user_id"], "result": result, "created_at": datetime.utcnow().isoformat()})
            await db.users.update_one({"_id": payload["user_id"]}, {"$inc": {"scan_count": 1}})
        except:
            pass
    return {"success": True, "result": result}

@app.post("/api/ai/ask")
async def ask_ai(query: AIQuery):
    lang = {"hi": "हिंदी में जवाब दें।", "pa": "ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ।", "mr": "मराठीत उत्तर द्या.", "en": "Reply in English."}.get(query.language, "Reply in English.")
    result = await call_claude(query.question, f"You are AnnadataHub AI for Indian farmers. {lang}")
    return {"success": True, "answer": result}

@app.get("/api/mandi/prices")
async def mandi_prices(crop: str = "wheat", state: str = "Punjab"):
    result = await call_claude(f"Give current mandi prices for {crop} in {state} India as JSON with markets, prices per quintal, MSP, and best selling tip.")
    return {"success": True, "data": result, "crop": crop, "state": state}

@app.get("/api/weather")
async def weather(location: str = "Punjab"):
    result = await call_claude(f"Give today's farming weather advisory for {location} India as JSON with temperature, humidity, rainfall_chance, spray_suitable, farming_advice.")
    return {"success": True, "data": result, "location": location}

@app.get("/api/schemes")
async def govt_schemes(state: str = "Punjab"):
    result = await call_claude(f"List 5 best government schemes for farmers in {state} India as JSON array with name, benefit, eligibility, how_to_apply.")
    return {"success": True, "data": result}

@app.get("/api/msp")
async def msp_info(crop: str = "wheat"):
    result = await call_claude(f"Give MSP information for {crop} India 2024-25 as JSON with msp_price, procurement_centers, documents_needed, how_to_sell.")
    return {"success": True, "data": result, "crop": crop}

@app.get("/api/farmgram/posts")
async def get_posts():
    posts = await db.farmgram.find().sort("created_at", -1).limit(20).to_list(20)
    for p in posts:
        p["id"] = p.pop("_id")
    return {"success": True, "posts": posts}

@app.post("/api/farmgram/post")
async def create_post(post: FarmGramPost, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Login required")
    payload = verify_token(authorization.replace("Bearer ", ""))
    user = await db.users.find_one({"_id": payload["user_id"]})
    post_id = str(uuid.uuid4())
    await db.farmgram.insert_one({"_id": post_id, "user_id": payload["user_id"], "user_name": user["full_name"], "content": post.content, "crop_type": post.crop_type, "location": post.location, "likes": 0, "created_at": datetime.utcnow().isoformat()})
    return {"success": True, "post_id": post_id}

@app.get("/api/user/profile")
async def get_profile(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No token")
    payload = verify_token(authorization.replace("Bearer ", ""))
    user = await db.users.find_one({"_id": payload["user_id"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "user": {"id": user["_id"], "email": user["email"], "full_name": user["full_name"], "plan": user.get("plan", "free"), "scan_count": user.get("scan_count", 0)}}
