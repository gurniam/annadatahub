from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import bcrypt
import jwt
import httpx
import json
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

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "annadatahub")
JWT_SECRET = os.environ.get("JWT_SECRET_KEY", "annadatahub-secret")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

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

async def call_claude(prompt: str, system: str = "") -> Optional[str]:
    if not CLAUDE_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "system": system or "You are AnnadataHub AI for Indian farmers. Always respond with valid JSON only.", "messages": [{"role": "user", "content": prompt}]}
            )
            data = r.json()
            if "content" in data and len(data["content"]) > 0:
                return data["content"][0]["text"]
            return None
    except:
        return None

async def call_claude_vision(image_base64: str, prompt: str) -> Optional[str]:
    if not CLAUDE_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}}, {"type": "text", "text": prompt}]}]}
            )
            data = r.json()
            if "content" in data and len(data["content"]) > 0:
                return data["content"][0]["text"]
            return None
    except:
        return None

def get_mandi_fallback(crop: str, state: str) -> str:
    prices = {"wheat": 2275, "rice": 2183, "maize": 1870, "cotton": 6620, "sugarcane": 315, "soybean": 4600}
    msp = prices.get(crop.lower(), 2000)
    return json.dumps({"markets": [{"market": f"{state} Main Mandi", "price": msp - 50, "unit": "per quintal"}, {"market": f"{state} Secondary Mandi", "price": msp - 80, "unit": "per quintal"}, {"market": f"{state} Local Market", "price": msp - 120, "unit": "per quintal"}], "msp": msp, "best_selling_tip": f"MSP for {crop} is Rs.{msp}/quintal. Compare prices before selling.", "date": datetime.utcnow().strftime("%d %b %Y")})

def get_weather_fallback(location: str) -> str:
    return json.dumps({"temperature": 28, "humidity": 65, "rainfall_chance": 20, "spray_suitable": True, "farming_advice": "Good conditions for farming. Avoid spraying during afternoon (12-3pm).", "best_time_to_work": "Early morning 6-10am", "alert": None})

def get_schemes_fallback(state: str) -> str:
    return json.dumps([{"name": "PM-KISAN", "benefit": "Rs.6,000 per year in your bank account", "eligibility": "All farmers with land records", "how_to_apply": "Visit pmkisan.gov.in or nearest CSC center", "deadline": "Ongoing"}, {"name": "Fasal Bima Yojana", "benefit": "Crop insurance at 1.5-2% premium", "eligibility": "All farmers growing notified crops", "how_to_apply": "Contact nearest bank before sowing", "deadline": "Before sowing season"}, {"name": "Kisan Credit Card", "benefit": "Crop loan up to Rs.3 lakh at 4% interest", "eligibility": "All farmers with land records", "how_to_apply": "Apply at nearest bank with Aadhar + land records", "deadline": "Ongoing"}])

def get_msp_fallback(crop: str) -> str:
    msp_data = {"wheat": 2275, "rice": 2183, "maize": 1870, "cotton": 6620, "soybean": 4600}
    price = msp_data.get(crop.lower(), 2000)
    return json.dumps({"crop": crop, "msp_price": price, "procurement_agency": "FCI / State agencies", "documents_needed": ["Aadhar card", "Bank passbook", "Land records"], "how_to_sell": "Register on state portal > Get token > Bring crop > Quality check > Payment in 3-5 days", "payment_timeline": "3-5 working days to bank account", "helpline": "Kisan Call Center: 1800-180-1551 (Free)"})

@app.get("/")
async def root():
    return {"message": "AnnadataHub API is running!", "status": "ok"}

@app.get("/api/health")
async def health():
    return {"status": "healthy", "message": "AnnadataHub backend is live!", "ai_enabled": bool(CLAUDE_API_KEY)}

@app.post("/api/auth/register")
async def register(user: UserRegister):
    try:
        existing = await db.users.find_one({"email": user.email})
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        hashed = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt()).decode()
        user_id = str(uuid.uuid4())
        await db.users.insert_one({"_id": user_id, "email": user.email, "password": hashed, "full_name": user.full_name, "phone": user.phone, "state": user.state, "plan": "free", "scan_count": 0, "created_at": datetime.utcnow().isoformat()})
        return {"token": create_token(user_id, user.email), "user": {"id": user_id, "email": user.email, "full_name": user.full_name, "plan": "free"}}
    except HTTPException:
        raise
    except:
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.")

@app.post("/api/auth/login")
async def login(user: UserLogin):
    try:
        db_user = await db.users.find_one({"email": user.email})
        if not db_user or not bcrypt.checkpw(user.password.encode(), db_user["password"].encode()):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        return {"token": create_token(db_user["_id"], user.email), "user": {"id": db_user["_id"], "email": user.email, "full_name": db_user["full_name"], "plan": db_user.get("plan", "free")}}
    except HTTPException:
        raise
    except:
        raise HTTPException(status_code=500, detail="Login failed. Please try again.")

@app.post("/api/crop/scan")
async def scan_crop(request: CropScanRequest, authorization: str = Header(None)):
    prompt = 'You are an expert agricultural scientist. Analyze this crop image and respond ONLY with this JSON: {"disease": "name or Healthy", "severity": "Low/Medium/High/None", "crop": "crop type", "confidence": 85, "treatment": "steps", "medicine": "medicine name in India", "dosage": "per litre", "prevention": "tips", "urgency": "Immediate/Within 7 days/No action needed"}'
    result = await call_claude_vision(request.image_base64, prompt)
    if not result:
        result = json.dumps({"disease": "Analysis unavailable", "severity": "Unknown", "crop": request.crop_type or "Unknown", "confidence": 0, "treatment": "Please try again or consult Krishi Vigyan Kendra", "medicine": "N/A", "dosage": "N/A", "prevention": "Consult local agricultural officer", "urgency": "No action needed"})
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
    lang_map = {"hi": "हिंदी में जवाब दें।", "pa": "ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ।", "mr": "मराठीत उत्तर द्या.", "te": "తెలుగులో సమాధానం.", "ta": "தமிழில் பதில்.", "gu": "ગુજરાતીમાં જવાબ.", "bn": "বাংলায় উত্তর দিন।", "kn": "ಕನ್ನಡದಲ್ಲಿ ಉತ್ತರ.", "ml": "മലയാളത്തിൽ.", "ur": "اردو میں جواب۔", "en": "Reply in English."}
    lang = lang_map.get(query.language, "Reply in English.")
    result = await call_claude(query.question, f"You are AnnadataHub AI for Indian farmers. Give practical advice. {lang}")
    if not result:
        result = "AI temporarily unavailable. Call Kisan Helpline: 1800-180-1551 (Free)"
    return {"success": True, "answer": result}

@app.get("/api/mandi/prices")
async def mandi_prices(crop: str = "wheat", state: str = "Punjab"):
    prompt = f'Give mandi prices for {crop} in {state} India today. JSON only: {{"markets": [{{"market": "name", "price": 2150, "unit": "per quintal"}}], "msp": 2275, "best_selling_tip": "tip", "date": "{datetime.utcnow().strftime("%d %b %Y")}"}}'
    result = await call_claude(prompt)
    if not result:
        result = get_mandi_fallback(crop, state)
    return {"success": True, "data": result, "crop": crop, "state": state}

@app.get("/api/weather")
async def weather(location: str = "Punjab"):
    prompt = f'Farming weather for {location} India today. JSON only: {{"temperature": 28, "humidity": 65, "rainfall_chance": 20, "spray_suitable": true, "farming_advice": "advice", "best_time_to_work": "hours", "alert": null}}'
    result = await call_claude(prompt)
    if not result:
        result = get_weather_fallback(location)
    return {"success": True, "data": result, "location": location}

@app.get("/api/schemes")
async def govt_schemes(state: str = "Punjab"):
    prompt = f'List 5 government schemes for farmers in {state} India. JSON array only: [{{"name": "name", "benefit": "benefit", "eligibility": "who", "how_to_apply": "steps", "deadline": "date"}}]'
    result = await call_claude(prompt)
    if not result:
        result = get_schemes_fallback(state)
    return {"success": True, "data": result}

@app.get("/api/msp")
async def msp_info(crop: str = "wheat"):
    prompt = f'MSP info for {crop} India 2024-25. JSON only: {{"crop": "{crop}", "msp_price": 2275, "procurement_agency": "agency", "documents_needed": ["doc1"], "how_to_sell": "steps", "payment_timeline": "days", "helpline": "number"}}'
    result = await call_claude(prompt)
    if not result:
        result = get_msp_fallback(crop)
    return {"success": True, "data": result, "crop": crop}

@app.get("/api/farmgram/posts")
async def get_posts():
    try:
        posts = await db.farmgram.find().sort("created_at", -1).limit(20).to_list(20)
        for p in posts:
            p["id"] = p.pop("_id")
        return {"success": True, "posts": posts}
    except:
        return {"success": True, "posts": []}

@app.post("/api/farmgram/post")
async def create_post(post: FarmGramPost, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Login required")
    payload = verify_token(authorization.replace("Bearer ", ""))
    try:
        user = await db.users.find_one({"_id": payload["user_id"]})
        post_id = str(uuid.uuid4())
        await db.farmgram.insert_one({"_id": post_id, "user_id": payload["user_id"], "user_name": user["full_name"] if user else "Farmer", "content": post.content, "crop_type": post.crop_type, "location": post.location, "likes": 0, "created_at": datetime.utcnow().isoformat()})
        return {"success": True, "post_id": post_id}
    except:
        raise HTTPException(status_code=500, detail="Could not create post")

@app.get("/api/user/profile")
async def get_profile(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No token")
    payload = verify_token(authorization.replace("Bearer ", ""))
    try:
        user = await db.users.find_one({"_id": payload["user_id"]})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, "user": {"id": user["_id"], "email": user["email"], "full_name": user["full_name"], "plan": user.get("plan", "free"), "scan_count": user.get("scan_count", 0)}}
    except HTTPException:
        raise
    except:
        raise HTTPException(status_code=500, detail="Could not fetch profile")
