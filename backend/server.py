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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

async def call_claude(prompt: str, system: str = "") -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "max_tokens": 1024, "messages": [
                    {"role": "system", "content": system or "You are AnnadataHub AI for Indian farmers. Always respond with valid JSON only."},
                    {"role": "user", "content": prompt}
                ]}
            )
            data = r.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
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
    WEATHER_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"https://api.openweathermap.org/data/2.5/weather",
                params={"q": f"{location},IN", "appid": WEATHER_KEY, "units": "metric"}
            )
            w = r.json()
            if w.get("cod") != 200:
                raise Exception("Bad response")
            temp = w["main"]["temp"]
            humidity = w["main"]["humidity"]
            rain = w.get("rain", {}).get("1h", 0)
            desc = w["weather"][0]["description"]
            spray = humidity < 80 and rain == 0
            result = json.dumps({
                "temperature": round(temp),
                "humidity": humidity,
                "rainfall_chance": min(int(rain * 100), 100),
                "description": desc,
                "spray_suitable": spray,
                "farming_advice": "Good conditions for farming." if spray else "High humidity or rain. Avoid spraying.",
                "best_time_to_work": "Early morning 6-10am",
                "alert": "Rain detected - protect harvested crops!" if rain > 0 else None
            })
    except:
        result = get_weather_fallback(location)
    return {"success": True, "data": result, "location": location}
@app.get("/api/schemes")
async def govt_schemes(state: str = "Punjab"):
    schemes = [
        {
            "name": "PM-KISAN",
            "emoji": "💰",
            "color": "#2e7d32",
            "tagline": "₹6,000/year direct to bank",
            "amount": "₹6,000",
            "amount_label": "per year",
            "eligible": True,
            "description": "Direct income support of Rs.6000 per year to all farmer families with cultivable land.",
            "benefits": ["₹2,000 every 4 months", "Direct bank transfer", "No middlemen"],
            "documents": ["Aadhar card", "Land records", "Bank passbook"],
            "how_to_apply": "Visit pmkisan.gov.in or nearest CSC center",
            "apply_url": "https://pmkisan.gov.in"
        },
        {
            "name": "PM Fasal Bima Yojana",
            "emoji": "🌾",
            "color": "#f57c00",
            "tagline": "Crop insurance at lowest premium",
            "amount": "1.5-2%",
            "amount_label": "premium only",
            "eligible": True,
            "description": "Comprehensive crop insurance against flood, drought, pest and disease at very low premium.",
            "benefits": ["Full compensation for crop loss", "Kharif premium only 2%", "Rabi premium only 1.5%"],
            "documents": ["Aadhar card", "Land records", "Bank passbook", "Sowing certificate"],
            "how_to_apply": "Contact nearest bank before sowing season",
            "apply_url": "https://pmfby.gov.in"
        },
        {
            "name": "Kisan Credit Card",
            "emoji": "💳",
            "color": "#1565c0",
            "tagline": "Crop loan at 4% interest",
            "amount": "₹3 lakh",
            "amount_label": "at 4% interest",
            "eligible": True,
            "description": "Easy credit for crop production, post-harvest expenses and allied activities.",
            "benefits": ["Loan up to Rs.3 lakh", "Interest rate only 4%", "Flexible repayment"],
            "documents": ["Aadhar card", "Land records", "Bank passbook", "Passport photo"],
            "how_to_apply": "Apply at nearest SBI, PNB or cooperative bank",
            "apply_url": "https://www.sbi.co.in/web/agri-rural/agriculture-banking/crop-loan/kisan-credit-card"
        },
        {
            "name": "PM Kisan Maan Dhan Yojana",
            "emoji": "👴",
            "color": "#6a1b9a",
            "tagline": "₹3,000/month pension after 60",
            "amount": "₹3,000",
            "amount_label": "per month after 60",
            "eligible": True,
            "description": "Pension scheme for small and marginal farmers to secure their old age.",
            "benefits": ["₹3,000 monthly pension", "Contribute only ₹55-200/month", "Government matches contribution"],
            "documents": ["Aadhar card", "Land records", "Bank passbook", "Age proof"],
            "how_to_apply": "Visit nearest CSC center with documents",
            "apply_url": "https://maandhan.in"
        },
        {
            "name": "Soil Health Card",
            "emoji": "🌱",
            "color": "#558b2f",
            "tagline": "Free soil testing + advice",
            "amount": "Free",
            "amount_label": "no cost",
            "eligible": True,
            "description": "Free soil testing to get crop-wise recommendations for fertilizers and nutrients.",
            "benefits": ["Free soil testing", "Fertilizer recommendations", "Reduce input costs by 20%"],
            "documents": ["Aadhar card", "Land records"],
            "how_to_apply": "Contact nearest Krishi Vigyan Kendra or agriculture office",
            "apply_url": "https://soilhealth.dac.gov.in"
        }
    ]
    data = {"summary": f"You are eligible for {len(schemes)} central government schemes. Apply today to get maximum benefits.", "schemes": schemes}
    return {"success": True, "data": json.dumps(data)}
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
