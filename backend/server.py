from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import bcrypt
import jwt
import httpx
import json
import logging
import hashlib
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("annadatahub")

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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
AGMARKNET_API_KEY = os.environ.get("AGMARKNET_API_KEY", "")

JWT_SECRET = os.environ.get("JWT_SECRET_KEY")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET_KEY environment variable is not set.")

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

_cache = {}
_gemini_model_cache = None


# ─── CACHE ────────────────────────────────────────────────────────────────────

def cache_get(key: str):
    if key in _cache:
        item = _cache[key]
        if datetime.utcnow() < item["expires"]:
            return item["value"]
        del _cache[key]
    return None

def cache_set(key: str, value, hours: int = 6):
    _cache[key] = {"value": value, "expires": datetime.utcnow() + timedelta(hours=hours)}


# ─── MODELS ───────────────────────────────────────────────────────────────────

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
    system_prompt: Optional[str] = None
    max_tokens: Optional[int] = 1024

class FarmGramPost(BaseModel):
    content: str
    crop_type: Optional[str] = None
    location: Optional[str] = None
    image_base64: Optional[str] = None

class CalendarRequest(BaseModel):
    crop: str
    variety: Optional[str] = None
    state: str
    sowing_month: Optional[str] = None
    land_size: Optional[str] = None
    language: Optional[str] = "en"

class FertilizerRequest(BaseModel):
    crop: str
    variety: Optional[str] = None
    land_size: str
    land_unit: Optional[str] = "acre"
    growth_stage: Optional[str] = None
    soil_type: Optional[str] = None
    state: Optional[str] = None
    language: Optional[str] = "en"

class WhatToGrowRequest(BaseModel):
    state: str
    season: str
    land_size: str
    land_unit: Optional[str] = "acre"
    water_source: Optional[str] = None
    soil_type: Optional[str] = None
    budget: Optional[str] = None
    goal: Optional[str] = None
    previous_crop: Optional[str] = None
    language: Optional[str] = "en"


# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────

def create_token(user_id: str, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=30)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token. Please login again.")

def get_user_from_header(authorization: str) -> Optional[dict]:
    if not authorization:
        return None
    token = authorization.replace("Bearer ", "").strip()
    try:
        return verify_token(token)
    except:
        return None


# ─── AI HELPERS ───────────────────────────────────────────────────────────────

async def call_ai(prompt: str, system: str = "", max_tokens: int = 1024) -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system or "You are AnnadataHub AI assistant for Indian farmers. Be accurate, practical and helpful."},
                        {"role": "user", "content": prompt}
                    ]
                }
            )
            if r.status_code in [429, 401]:
                return None
            r.raise_for_status()
            data = r.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
            return None
    except Exception as e:
        logger.error("Groq error: %s", e)
        return None

def clean_json_response(text: str) -> Optional[dict]:
    try:
        clean = text.replace("```json", "").replace("```", "").strip()
        idx = clean.find("{")
        if idx > 0:
            clean = clean[idx:]
        last = clean.rfind("}")
        if last >= 0:
            clean = clean[:last+1]
        return json.loads(clean)
    except:
        return None


# ─── GEMINI VISION ────────────────────────────────────────────────────────────

async def get_available_gemini_models() -> list:
    if not GEMINI_API_KEY:
        return []
    try:
        for api_version in ["v1beta", "v1"]:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"https://generativelanguage.googleapis.com/{api_version}/models?key={GEMINI_API_KEY}"
                )
                if r.status_code == 200:
                    data = r.json()
                    models = data.get("models", [])
                    vision_models = []
                    for m in models:
                        name = m.get("name", "")
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in methods:
                            model_id = name.replace("models/", "")
                            if "flash" in model_id or "pro" in model_id or "vision" in model_id:
                                vision_models.append((api_version, model_id))
                    return vision_models
    except Exception as e:
        logger.error("Could not list Gemini models: %s", e)
    return []

async def call_gemini_vision(image_base64: str, prompt: str) -> Optional[str]:
    global _gemini_model_cache
    if not GEMINI_API_KEY:
        return None

    request_body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}},
            {"text": prompt}
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}
    }

    models_to_try = []
    if _gemini_model_cache:
        models_to_try = [_gemini_model_cache]
    else:
        models_to_try = [
            ("v1beta", "gemini-2.0-flash"),
            ("v1", "gemini-2.0-flash"),
            ("v1beta", "gemini-2.0-flash-exp"),
            ("v1beta", "gemini-1.5-flash"),
            ("v1", "gemini-1.5-flash"),
            ("v1beta", "gemini-1.5-flash-001"),
            ("v1beta", "gemini-1.5-pro"),
            ("v1", "gemini-1.5-pro"),
            ("v1beta", "gemini-pro-vision"),
        ]
        try:
            dynamic_models = await get_available_gemini_models()
            for m in dynamic_models:
                if m not in models_to_try:
                    models_to_try.insert(0, m)
        except Exception as e:
            logger.warning("Could not get dynamic models: %s", e)

    for api_version, model_id in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(url, headers={"Content-Type": "application/json"}, json=request_body)
                if r.status_code == 200:
                    data = r.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and parts[0].get("text"):
                            _gemini_model_cache = (api_version, model_id)
                            return parts[0]["text"]
                elif r.status_code == 404:
                    continue
                elif r.status_code == 403:
                    if "API_KEY_INVALID" in r.text:
                        return None
                    continue
                elif r.status_code == 400:
                    continue
        except Exception as e:
            continue

    return None

async def call_groq_vision(image_base64: str, prompt: str) -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.2-11b-vision-preview",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                        {"type": "text", "text": prompt}
                    ]}]
                }
            )
            if r.status_code == 200:
                data = r.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
        return None
    except Exception as e:
        logger.error("Groq vision error: %s", e)
        return None


# ─── FALLBACKS ────────────────────────────────────────────────────────────────

STATIC_MSP = {
    "wheat": 2275, "rice": 2300, "paddy": 2300, "maize": 2090,
    "cotton": 6620, "sugarcane": 340, "soybean": 4892,
    "mustard": 5650, "groundnut": 6783, "onion": 1800,
    "potato": 1400, "tomato": 2000, "gram": 5440, "lentil": 6425,
    "moong": 8558, "urad": 7400, "jowar": 3180, "bajra": 2625,
    "ragi": 3846, "sunflower": 6760, "sesame": 8635
}

def get_mandi_fallback(crop: str, state: str) -> dict:
    msp = STATIC_MSP.get(crop.lower(), 2000)
    return {
        "source": "fallback",
        "is_live": False,
        "badge": "AI Estimate",
        "crop": crop,
        "state": state,
        "markets": [
            {"market": f"{state} Main Mandi", "min_price": msp - 100, "max_price": msp + 200, "modal_price": msp + 50, "unit": "per quintal"},
            {"market": f"{state} Secondary Mandi", "min_price": msp - 150, "max_price": msp + 150, "modal_price": msp, "unit": "per quintal"},
            {"market": f"{state} Local Market", "min_price": msp - 200, "max_price": msp + 100, "modal_price": msp - 50, "unit": "per quintal"}
        ],
        "msp": msp,
        "best_selling_tip": f"Government MSP for {crop} is ₹{msp}/quintal. Always compare 3 mandis before selling.",
        "date": datetime.utcnow().strftime("%d %b %Y"),
        "note": "Add AGMARKNET_API_KEY to Railway for live prices"
    }

FALLBACK_NEWS = [
    {"category": "price", "title": "Wheat MSP ₹2,275/quintal for 2024-25", "summary": "Government MSP for wheat set at ₹2,275/quintal.", "detail": "The MSP for wheat is ₹2,275 per quintal for the 2024-25 Rabi season.", "impact": "Sell wheat at minimum ₹2,275/quintal.", "action": "Register on your state mandi portal before selling.", "time_ago": "Today"},
    {"category": "scheme", "title": "PM-KISAN — ₹2,000 installment coming soon", "summary": "Check your PM-KISAN status.", "detail": "Over 9 crore farmers receive ₹6,000 per year in 3 installments.", "impact": "₹2,000 will be credited to your bank account.", "action": "Check at pmkisan.gov.in or call 155261.", "time_ago": "Recently"},
    {"category": "scheme", "title": "Kisan Credit Card — 4% interest crop loan", "summary": "KCC provides easy credit for farming needs.", "detail": "Kisan Credit Card provides credit up to ₹3 lakh at 4% interest.", "impact": "Save money on farming loans.", "action": "Apply at nearest SBI, PNB or cooperative bank.", "time_ago": "This week"},
    {"category": "alert", "title": "Use certified seeds for 20-30% higher yield", "summary": "KVK recommends certified seeds.", "detail": "Certified seeds ensure better germination and disease resistance.", "impact": "20-30% higher yield with certified seeds.", "action": "Buy seeds only from registered dealers.", "time_ago": "This week"},
    {"category": "scheme", "title": "PM-KUSUM solar pump — 90% subsidy", "summary": "Solar pumps at 10% cost under PM-KUSUM.", "detail": "90% government subsidy on solar pumps. Save ₹20,000-50,000/year on electricity.", "impact": "Free irrigation electricity forever.", "action": "Apply at pmkusum.mnre.gov.in.", "time_ago": "This month"},
]

async def fetch_rss_news(state: str) -> list:
    news_items = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get("https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", headers={"User-Agent": "AnnadataHub/1.0"})
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                channel = root.find("channel")
                if channel:
                    for item in channel.findall("item")[:3]:
                        title = item.findtext("title", "")
                        desc = item.findtext("description", "")
                        pub = item.findtext("pubDate", "")
                        if title:
                            news_items.append({
                                "category": "general", "title": title[:120],
                                "summary": desc[:200] if desc else title,
                                "detail": desc[:400] if desc else title,
                                "impact": "Stay informed about government agriculture policies.",
                                "action": "Read full article on official government website.",
                                "time_ago": pub[:20] if pub else "Recently"
                            })
    except Exception as e:
        logger.warning("RSS fetch failed: %s", e)
    return news_items


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "AnnadataHub API is running!", "status": "ok"}

@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "message": "AnnadataHub backend is live!",
        "ai_enabled": bool(GROQ_API_KEY),
        "vision_provider": "Google Gemini + Groq fallback",
        "gemini_enabled": bool(GEMINI_API_KEY),
        "mandi_live": bool(AGMARKNET_API_KEY)
    }

@app.get("/api/gemini/models")
async def list_gemini_models():
    models = await get_available_gemini_models()
    return {"models": models, "count": len(models)}


# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(user: UserRegister):
    try:
        existing = await db.users.find_one({"email": user.email})
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        hashed = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt()).decode()
        user_id = str(uuid.uuid4())
        await db.users.insert_one({
            "_id": user_id, "email": user.email, "password": hashed,
            "full_name": user.full_name, "phone": user.phone, "state": user.state,
            "plan": "free", "scan_count": 0, "language": user.language,
            "created_at": datetime.utcnow().isoformat()
        })
        await db.feature_logs.insert_one({"feature": "register", "timestamp": datetime.utcnow().isoformat()})
        return {"token": create_token(user_id, user.email), "user": {"id": user_id, "email": user.email, "full_name": user.full_name, "plan": "free"}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Registration error: %s", e)
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
    except Exception as e:
        raise HTTPException(status_code=500, detail="Login failed. Please try again.")


# ─── CROP SCAN ────────────────────────────────────────────────────────────────

@app.post("/api/crop/scan")
async def scan_crop(request: CropScanRequest, authorization: str = Header(None)):
    lang_map = {
        "hi": "Respond in Hindi.", "pa": "Respond in Punjabi.",
        "mr": "Respond in Marathi.", "te": "Respond in Telugu.",
        "ta": "Respond in Tamil.", "en": "Respond in English.",
        "kn": "Respond in Kannada.", "ml": "Respond in Malayalam.",
        "gu": "Respond in Gujarati.", "bn": "Respond in Bengali."
    }
    lang_instruction = lang_map.get(request.language, "Respond in English.")
    vision_prompt = (
        f"You are an expert agricultural scientist. {lang_instruction} "
        f"Analyze this crop/plant image carefully. Identify disease, pest damage, nutrient deficiency, or if healthy. "
        f"Return ONLY valid JSON with no extra text: "
        f'{{\"disease\": \"disease name or Healthy\", \"severity\": \"Low/Medium/High/None\", '
        f'\"crop\": \"crop type\", \"confidence\": 85, '
        f'\"treatment\": \"specific treatment steps\", '
        f'\"medicine\": \"medicine name available in India\", '
        f'\"dosage\": \"dosage per litre\", '
        f'\"prevention\": \"prevention tips\", '
        f'\"urgency\": \"Immediate/Within 7 days/No action needed\"}}'
    )

    result = None
    result = await call_gemini_vision(request.image_base64, vision_prompt)

    if not result:
        result = await call_groq_vision(request.image_base64, vision_prompt)

    if not result:
        crop = request.crop_type or "unknown crop"
        text_prompt = (
            f"An Indian farmer's {crop} crop shows disease/pest symptoms. {lang_instruction} "
            f"Give the most common diagnosis for {crop} in India. "
            f"Return ONLY valid JSON: "
            f'{{\"disease\": \"most likely disease\", \"severity\": \"Medium\", '
            f'\"crop\": \"{crop}\", \"confidence\": 65, '
            f'\"treatment\": \"treatment steps\", '
            f'\"medicine\": \"medicine available in India\", '
            f'\"dosage\": \"standard dosage\", '
            f'\"prevention\": \"prevention tips\", '
            f'\"urgency\": \"Within 7 days\"}}'
        )
        result = await call_ai(text_prompt)

    if not result:
        result = json.dumps({
            "disease": "Please select crop type and try again",
            "severity": "Unknown", "crop": request.crop_type or "Unknown",
            "confidence": 0,
            "treatment": "Select your crop type from the dropdown, then scan again.",
            "medicine": "N/A", "dosage": "N/A",
            "prevention": "For accurate diagnosis visit your nearest KVK — free service.",
            "urgency": "Within 7 days"
        })

    try:
        clean = result.replace("```json", "").replace("```", "").strip()
        idx = clean.find("{")
        if idx > 0:
            clean = clean[idx:]
        last = clean.rfind("}")
        if last >= 0:
            clean = clean[:last+1]
        parsed = json.loads(clean)
        await db.feature_logs.insert_one({"feature": "crop_scan", "timestamp": datetime.utcnow().isoformat()})
        return parsed
    except:
        return json.loads(result) if isinstance(result, str) and result.startswith("{") else {
            "disease": "Scan failed", "severity": "Unknown", "crop": "Unknown",
            "confidence": 0, "treatment": "Please try again.",
            "medicine": "N/A", "dosage": "N/A", "prevention": "Visit KVK.", "urgency": "Within 7 days"
        }


# ─── MANDI PRICES (FIXED) ─────────────────────────────────────────────────────

@app.get("/api/mandi/prices")
async def get_mandi_prices(crop: str = "wheat", state: str = "Uttar Pradesh", language: str = "en"):
    cache_key = f"mandi_{crop.lower()}_{state.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    # 1. Try AGMARKNET live data
    if AGMARKNET_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.get(
                    "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070",
                    params={
                        "api-key": AGMARKNET_API_KEY,
                        "format": "json",
                        "limit": 10,
                        "filters[State]": state,
                        "filters[Commodity]": crop.capitalize()
                    },
                    headers={"User-Agent": "AnnadataHub/1.0"}
                )
                logger.info("AGMARKNET status: %s", r.status_code)
                if r.status_code == 200:
                    data = r.json()
                    records = data.get("records", [])
                    if records:
                        markets = []
                        for rec in records[:5]:
                            markets.append({
                                "market": rec.get("Market", rec.get("market", "")),
                                "district": rec.get("District", ""),
                                "min_price": int(rec.get("Min Price", rec.get("min_price", 0))),
                                "max_price": int(rec.get("Max Price", rec.get("max_price", 0))),
                                "modal_price": int(rec.get("Modal Price", rec.get("modal_price", 0))),
                                "unit": "per quintal",
                                "date": rec.get("Arrival Date", rec.get("arrival_date", ""))
                            })
                        result = {
                            "source": "live",
                            "is_live": True,
                            "badge": "LIVE",
                            "crop": crop,
                            "state": state,
                            "markets": markets,
                            "msp": STATIC_MSP.get(crop.lower(), 0),
                            "best_selling_tip": f"Sell at the highest paying mandi. Always compare prices.",
                            "date": datetime.utcnow().strftime("%d %b %Y")
                        }
                        cache_set(cache_key, result, hours=6)
                        await db.feature_logs.insert_one({"feature": "mandi_live", "crop": crop, "state": state, "timestamp": datetime.utcnow().isoformat()})
                        return result
        except Exception as e:
            logger.error("AGMARKNET error: %s", e)

    # 2. AI-generated realistic estimate
    if GROQ_API_KEY:
        try:
            season = "Kharif (monsoon)" if datetime.utcnow().month in [6,7,8,9,10] else "Rabi (winter)"
            prompt = (
                f"Give realistic current mandi prices for {crop} in {state}, India for {season} season {datetime.utcnow().year}. "
                f"Based on actual market trends, MSP of ₹{STATIC_MSP.get(crop.lower(), 2000)}/quintal. "
                f"Return ONLY valid JSON, no extra text: "
                f'{{"source":"ai_estimate","is_live":false,"badge":"AI Estimate","crop":"{crop}","state":"{state}",'
                f'"markets":['
                f'{{"market":"[Main mandi name] Mandi","district":"[district]","min_price":0,"max_price":0,"modal_price":0,"unit":"per quintal","date":"{datetime.utcnow().strftime("%d %b %Y")}"}},'
                f'{{"market":"[Second mandi] Mandi","district":"[district]","min_price":0,"max_price":0,"modal_price":0,"unit":"per quintal","date":"{datetime.utcnow().strftime("%d %b %Y")}"}},'
                f'{{"market":"[Third mandi] Mandi","district":"[district]","min_price":0,"max_price":0,"modal_price":0,"unit":"per quintal","date":"{datetime.utcnow().strftime("%d %b %Y")}"}}],'
                f'"msp":{STATIC_MSP.get(crop.lower(), 0)},'
                f'"best_selling_tip":"[practical tip for selling {crop} in {state}]",'
                f'"date":"{datetime.utcnow().strftime("%d %b %Y")}",'
                f'"note":"AI estimate — add AGMARKNET_API_KEY to Railway for live prices"}}'
            )
            ai_result = await call_ai(prompt, max_tokens=800)
            if ai_result:
                parsed = clean_json_response(ai_result)
                if parsed and parsed.get("markets"):
                    cache_set(cache_key, parsed, hours=3)
                    await db.feature_logs.insert_one({"feature": "mandi_ai", "crop": crop, "state": state, "timestamp": datetime.utcnow().isoformat()})
                    return parsed
        except Exception as e:
            logger.error("Mandi AI error: %s", e)

    # 3. Static fallback
    fallback = get_mandi_fallback(crop, state)
    cache_set(cache_key, fallback, hours=1)
    return fallback


# ─── WEATHER ──────────────────────────────────────────────────────────────────

@app.get("/api/weather")
async def get_weather(location: str = "Pithoragarh", state: str = "Uttarakhand", language: str = "en"):
    cache_key = f"weather_{location.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    prompt = (
        f"Give farming weather advice for {location}, {state}, India for today {datetime.utcnow().strftime('%B %Y')}. "
        f"Return ONLY valid JSON: "
        f'{{"temperature":28,"humidity":65,"rainfall_chance":20,"conditions":"Partly Cloudy",'
        f'"spray_suitable":true,"farming_advice":"practical advice",'
        f'"best_time_to_work":"Early morning 6-10am","alert":null,'
        f'"weekly_outlook":"next 7 days outlook"}}'
    )
    result = await call_ai(prompt, max_tokens=500)
    if result:
        parsed = clean_json_response(result)
        if parsed:
            cache_set(cache_key, parsed, hours=3)
            return parsed

    fallback = {
        "temperature": 28, "humidity": 65, "rainfall_chance": 20,
        "conditions": "Partly Cloudy", "spray_suitable": True,
        "farming_advice": "Good conditions for farming. Avoid spraying during afternoon 12-3pm.",
        "best_time_to_work": "Early morning 6-10am",
        "alert": None, "weekly_outlook": "Normal weather expected this week."
    }
    return fallback


# ─── GOVERNMENT SCHEMES ───────────────────────────────────────────────────────

@app.get("/api/schemes")
async def get_schemes(state: str = "Uttarakhand", crop: str = "", land_size: str = "", language: str = "en"):
    cache_key = f"schemes_{state.lower()}_{crop.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    lang_map = {"hi": "Hindi", "pa": "Punjabi", "mr": "Marathi", "en": "English"}
    lang = lang_map.get(language, "English")

    prompt = (
        f"You are an Indian government schemes expert. List relevant agricultural schemes for a farmer in {state}"
        f"{' growing ' + crop if crop else ''}{' with ' + land_size + ' land' if land_size else ''}. "
        f"Include both central and {state} state schemes. Respond in {lang}. "
        f"Return ONLY valid JSON: "
        f'{{"schemes":['
        f'{{"name":"PM-KISAN","type":"central","benefit":"₹6000/year","eligibility":"All farmers","how_to_apply":"pmkisan.gov.in","documents":["Aadhaar","Bank passbook","Land record"]}},'
        f'... 8 more relevant schemes],'
        f'"total_schemes":9,"state":"{state}","personalized_tip":"most important scheme for this farmer"}}'
    )
    result = await call_ai(prompt, max_tokens=3000)
    if result:
        parsed = clean_json_response(result)
        if parsed:
            cache_set(cache_key, parsed, hours=24)
            await db.feature_logs.insert_one({"feature": "schemes", "state": state, "timestamp": datetime.utcnow().isoformat()})
            return parsed

    return {"schemes": [], "total_schemes": 0, "state": state, "error": "Could not load schemes. Please try again."}


# ─── WHAT TO GROW ─────────────────────────────────────────────────────────────

@app.post("/api/what-to-grow")
async def what_to_grow(request: WhatToGrowRequest):
    prompt = (
        f"You are an expert Indian agricultural advisor. Recommend top 3 crops for a farmer with these details: "
        f"State: {request.state}, Season: {request.season}, Land: {request.land_size} {request.land_unit}, "
        f"Water: {request.water_source or 'Not specified'}, Soil: {request.soil_type or 'Not specified'}, "
        f"Budget: {request.budget or 'Not specified'}, Goal: {request.goal or 'Income'}, "
        f"Previous crop: {request.previous_crop or 'Not specified'}. "
        f"Return ONLY valid JSON: "
        f'{{"recommendations":['
        f'{{"rank":1,"crop":"Crop Name","variety":"Best variety for {request.state}",'
        f'"expected_income":"₹X per {request.land_unit}","duration":"X months",'
        f'"investment":"₹X total","risk":"Low/Medium/High",'
        f'"why_recommended":"specific reason for {request.state} and {request.season}",'
        f'"varieties":["var1","var2"],'
        f'"state_specific_tips":"tips for {request.state} farmers",'
        f'"where_to_sell":"nearest mandi/FPO/buyer",'
        f'"government_support":"relevant scheme name"}}],'
        f'"season":"{request.season}","state":"{request.state}",'
        f'"best_choice":"Name of rank 1 crop and why in one line"}}'
    )
    result = await call_ai(prompt, max_tokens=2500)
    if result:
        parsed = clean_json_response(result)
        if parsed:
            await db.feature_logs.insert_one({"feature": "what_to_grow", "state": request.state, "timestamp": datetime.utcnow().isoformat()})
            return parsed

    raise HTTPException(status_code=500, detail="Could not generate recommendations. Please try again.")


# ─── AI ASK ───────────────────────────────────────────────────────────────────

@app.post("/api/ai/ask")
async def ai_ask(query: AIQuery):
    lang_map = {
        "hi": "Hindi", "pa": "Punjabi", "mr": "Marathi",
        "te": "Telugu", "ta": "Tamil", "en": "English",
        "kn": "Kannada", "ml": "Malayalam", "gu": "Gujarati", "bn": "Bengali"
    }
    lang = lang_map.get(query.language, "English")

    system = query.system_prompt or (
        f"You are AnnadataHub AI — an expert farming advisor for Indian farmers. "
        f"Always respond in {lang}. Give practical, actionable advice with: "
        f"exact costs in rupees, product names available in India, step-by-step methods, "
        f"government schemes if relevant, and realistic income estimates. "
        f"Structure your answer with clear sections."
    )

    result = await call_ai(query.question, system=system, max_tokens=2000)
    if result:
        await db.feature_logs.insert_one({"feature": "ai_ask", "timestamp": datetime.utcnow().isoformat()})
        await db.questions_log.insert_one({
            "question": query.question[:500],
            "language": query.language,
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"answer": result, "language": query.language}

    raise HTTPException(status_code=500, detail="AI unavailable. Please try again.")


# ─── CROP CALENDAR ────────────────────────────────────────────────────────────

@app.post("/api/calendar")
async def crop_calendar(request: CalendarRequest):
    prompt = (
        f"Create a complete crop calendar for {request.crop}"
        f"{' (' + request.variety + ')' if request.variety else ''} "
        f"in {request.state}, India"
        f"{', sowing in ' + request.sowing_month if request.sowing_month else ''}. "
        f"Return ONLY valid JSON: "
        f'{{"crop":"{request.crop}","state":"{request.state}","variety":"{request.variety or "Standard"}",'
        f'"total_duration":"X months",'
        f'"stages":['
        f'{{"stage":"Land Preparation","week":"Week 1-2","tasks":["task1","task2"],"inputs":"fertilizer/seeds needed","cost":"₹X","warning":"what to avoid"}},'
        f'... all stages until harvest],'
        f'"harvest_time":"Month Year estimate",'
        f'"expected_yield":"X quintal per acre",'
        f'"expected_income":"₹X per acre",'
        f'"key_tips":["tip1","tip2","tip3"]}}'
    )
    result = await call_ai(prompt, max_tokens=2000)
    if result:
        parsed = clean_json_response(result)
        if parsed:
            await db.feature_logs.insert_one({"feature": "calendar", "crop": request.crop, "timestamp": datetime.utcnow().isoformat()})
            return parsed
    raise HTTPException(status_code=500, detail="Could not generate calendar. Please try again.")


# ─── FERTILIZER CALCULATOR ────────────────────────────────────────────────────

@app.post("/api/fertilizer")
async def fertilizer_calc(request: FertilizerRequest):
    prompt = (
        f"Calculate exact fertilizer doses for {request.crop}"
        f"{' (' + request.variety + ')' if request.variety else ''} "
        f"on {request.land_size} {request.land_unit}"
        f"{' in ' + request.state if request.state else ''}"
        f"{', soil type: ' + request.soil_type if request.soil_type else ''}"
        f"{', growth stage: ' + request.growth_stage if request.growth_stage else ''}. "
        f"Return ONLY valid JSON: "
        f'{{"crop":"{request.crop}","land":"{request.land_size} {request.land_unit}",'
        f'"fertilizers":['
        f'{{"name":"Urea","quantity":"X kg","timing":"when to apply","method":"how to apply","cost":"₹X","brand_example":"available brand in India"}},'
        f'... all required fertilizers],'
        f'"micronutrients":["if any"],'
        f'"total_cost":"₹X",'
        f'"schedule":"application schedule",'
        f'"warnings":["what not to mix","overdose risks"],'
        f'"organic_alternative":"if any"}}'
    )
    result = await call_ai(prompt, max_tokens=1500)
    if result:
        parsed = clean_json_response(result)
        if parsed:
            await db.feature_logs.insert_one({"feature": "fertilizer", "crop": request.crop, "timestamp": datetime.utcnow().isoformat()})
            return parsed
    raise HTTPException(status_code=500, detail="Could not calculate. Please try again.")


# ─── MSP ──────────────────────────────────────────────────────────────────────

@app.get("/api/msp")
async def get_msp(crop: str = "", language: str = "en"):
    cache_key = "msp_all"
    cached = cache_get(cache_key)
    if cached:
        if crop:
            msp_val = STATIC_MSP.get(crop.lower())
            if msp_val:
                return {"crop": crop, "msp": msp_val, "unit": "per quintal", "year": "2024-25", "all_crops": cached}
        return cached

    msp_data = {
        "year": "2024-25",
        "source": "Government of India",
        "prices": [{"crop": k.capitalize(), "msp": v, "unit": "per quintal"} for k, v in STATIC_MSP.items()]
    }
    cache_set(cache_key, msp_data, hours=168)

    if crop:
        msp_val = STATIC_MSP.get(crop.lower())
        return {"crop": crop, "msp": msp_val, "unit": "per quintal", "year": "2024-25", "all_crops": msp_data}

    return msp_data


# ─── NEWS ─────────────────────────────────────────────────────────────────────

@app.get("/api/news")
async def get_news(state: str = "Uttarakhand", language: str = "en"):
    cache_key = f"news_{state.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    rss_news = await fetch_rss_news(state)
    all_news = rss_news + FALLBACK_NEWS
    result = {"news": all_news[:8], "state": state, "date": datetime.utcnow().strftime("%d %b %Y")}
    cache_set(cache_key, result, hours=2)
    return result


# ─── FARMGRAM ─────────────────────────────────────────────────────────────────

@app.get("/api/farmgram/posts")
async def get_posts(limit: int = 20, skip: int = 0):
    try:
        posts = []
        cursor = db.farmgram_posts.find({}).sort("created_at", -1).skip(skip).limit(limit)
        async for post in cursor:
            post["_id"] = str(post["_id"]) if "_id" in post else post.get("post_id", "")
            posts.append(post)
        return {"posts": posts, "count": len(posts)}
    except Exception as e:
        logger.error("FarmGram fetch error: %s", e)
        return {"posts": [], "count": 0}

@app.post("/api/farmgram/posts")
async def create_post(post: FarmGramPost, authorization: str = Header(None)):
    user = get_user_from_header(authorization)
    try:
        post_id = str(uuid.uuid4())
        post_doc = {
            "post_id": post_id,
            "content": post.content,
            "crop_type": post.crop_type,
            "location": post.location,
            "has_image": bool(post.image_base64),
            "user_id": user["user_id"] if user else "anonymous",
            "user_name": "Farmer",
            "likes": 0,
            "comments": [],
            "created_at": datetime.utcnow().isoformat()
        }
        await db.farmgram_posts.insert_one(post_doc)
        await db.feature_logs.insert_one({"feature": "farmgram_post", "timestamp": datetime.utcnow().isoformat()})
        return {"success": True, "post_id": post_id}
    except Exception as e:
        logger.error("FarmGram post error: %s", e)
        raise HTTPException(status_code=500, detail="Could not create post.")

@app.post("/api/farmgram/posts/{post_id}/like")
async def like_post(post_id: str):
    try:
        await db.farmgram_posts.update_one({"post_id": post_id}, {"$inc": {"likes": 1}})
        return {"success": True}
    except:
        raise HTTPException(status_code=500, detail="Could not like post.")


# ─── ADMIN ────────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Anmol2002")

def verify_admin(authorization: str):
    if not authorization:
        raise HTTPException(status_code=401, detail="Admin access required")
    token = authorization.replace("Bearer ", "").strip()
    if token != ADMIN_PASSWORD and token != hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if payload.get("role") != "admin":
                raise HTTPException(status_code=403, detail="Not an admin")
        except:
            raise HTTPException(status_code=401, detail="Invalid admin token")

@app.get("/api/admin/stats")
async def admin_stats(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        total_users = await db.users.count_documents({})
        total_posts = await db.farmgram_posts.count_documents({})
        total_questions = await db.questions_log.count_documents({})
        today = datetime.utcnow().strftime("%Y-%m-%d")
        today_logs = await db.feature_logs.count_documents({"timestamp": {"$gte": today}})
        return {
            "total_users": total_users,
            "total_posts": total_posts,
            "total_questions": total_questions,
            "today_activity": today_logs,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/farmers")
async def admin_farmers(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        farmers = []
        cursor = db.users.find({}, {"password": 0}).sort("created_at", -1).limit(100)
        async for u in cursor:
            u["_id"] = str(u["_id"]) if "_id" in u else u.get("id", "")
            farmers.append(u)
        return {"farmers": farmers, "count": len(farmers)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/questions")
async def admin_questions(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        questions = []
        cursor = db.questions_log.find({}).sort("timestamp", -1).limit(100)
        async for q in cursor:
            q["_id"] = str(q["_id"])
            questions.append(q)
        return {"questions": questions, "count": len(questions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/posts")
async def admin_posts(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        posts = []
        cursor = db.farmgram_posts.find({}).sort("created_at", -1).limit(100)
        async for p in cursor:
            p["_id"] = str(p["_id"])
            posts.append(p)
        return {"posts": posts, "count": len(posts)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/feature-usage")
async def admin_feature_usage(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        pipeline = [{"$group": {"_id": "$feature", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}]
        usage = []
        async for doc in db.feature_logs.aggregate(pipeline):
            usage.append({"feature": doc["_id"], "count": doc["count"]})
        return {"usage": usage}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/growth")
async def admin_growth(authorization: str = Header(None)):
    verify_admin(authorization)
    try:
        pipeline = [
            {"$group": {"_id": {"$substr": ["$created_at", 0, 10]}, "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}}, {"$limit": 30}
        ]
        growth = []
        async for doc in db.users.aggregate(pipeline):
            growth.append({"date": doc["_id"], "new_users": doc["count"]})
        return {"growth": growth}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/errors")
async def admin_errors(authorization: str = Header(None)):
    verify_admin(authorization)
    return {"errors": [], "message": "Error logging via Railway logs. Check Railway console."}
