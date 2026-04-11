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

JWT_SECRET = os.environ.get("JWT_SECRET_KEY")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET_KEY environment variable is not set.")

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

_cache = {}
_gemini_model_cache = None  # Cache the working Gemini model name

def cache_get(key: str):
    if key in _cache:
        item = _cache[key]
        if datetime.utcnow() < item["expires"]:
            return item["value"]
        del _cache[key]
    return None

def cache_set(key: str, value, hours: int = 6):
    _cache[key] = {"value": value, "expires": datetime.utcnow() + timedelta(hours=hours)}


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

class FarmGramPost(BaseModel):
    content: str
    crop_type: Optional[str] = None
    location: Optional[str] = None
    image_base64: Optional[str] = None


def create_token(user_id: str, email: str) -> str:
    payload = {"user_id": user_id, "email": email, "exp": datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token. Please login again.")


async def call_ai(prompt: str, system: str = "") -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "system", "content": system or "You are AnnadataHub AI for Indian farmers."},
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


async def get_available_gemini_models() -> list:
    """Get list of actually available Gemini models for this API key"""
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
                    # Filter models that support generateContent and have vision
                    vision_models = []
                    for m in models:
                        name = m.get("name", "")
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in methods:
                            model_id = name.replace("models/", "")
                            # Prefer flash models for speed
                            if "flash" in model_id or "pro" in model_id or "vision" in model_id:
                                vision_models.append((api_version, model_id))
                    logger.info("Available Gemini models: %s", [m[1] for m in vision_models])
                    return vision_models
    except Exception as e:
        logger.error("Could not list Gemini models: %s", e)
    return []


async def call_gemini_vision(image_base64: str, prompt: str) -> Optional[str]:
    """Call Gemini with dynamic model discovery"""
    global _gemini_model_cache

    if not GEMINI_API_KEY:
        return None

    request_body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}},
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}
    }

    # Use cached working model if available
    models_to_try = []
    if _gemini_model_cache:
        models_to_try = [_gemini_model_cache]
    else:
        # First try known good combinations
        models_to_try = [
            ("v1beta", "gemini-2.0-flash"),
            ("v1", "gemini-2.0-flash"),
            ("v1beta", "gemini-2.0-flash-exp"),
            ("v1beta", "gemini-2.0-flash-001"),
            ("v1beta", "gemini-1.5-flash"),
            ("v1", "gemini-1.5-flash"),
            ("v1beta", "gemini-1.5-flash-001"),
            ("v1beta", "gemini-1.5-flash-002"),
            ("v1beta", "gemini-1.5-pro"),
            ("v1", "gemini-1.5-pro"),
            ("v1beta", "gemini-pro-vision"),
        ]

        # Also try dynamically discovered models
        try:
            dynamic_models = await get_available_gemini_models()
            for m in dynamic_models:
                if m not in models_to_try:
                    models_to_try.insert(0, m)  # Try dynamic models first
        except Exception as e:
            logger.warning("Could not get dynamic models: %s", e)

    for api_version, model_id in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_id}:generateContent?key={GEMINI_API_KEY}"
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(url, headers={"Content-Type": "application/json"}, json=request_body)
                logger.info("Gemini %s/%s status: %s", api_version, model_id, r.status_code)

                if r.status_code == 200:
                    data = r.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and parts[0].get("text"):
                            logger.info("✅ Gemini SUCCESS with %s/%s", api_version, model_id)
                            _gemini_model_cache = (api_version, model_id)
                            return parts[0]["text"]

                elif r.status_code == 404:
                    logger.warning("Model %s not found in %s", model_id, api_version)
                    continue

                elif r.status_code == 403:
                    resp_text = r.text
                    logger.error("Permission denied for %s/%s: %s", api_version, model_id, resp_text[:200])
                    # If permission denied for all, no point trying more
                    if "API_KEY_INVALID" in resp_text:
                        logger.error("GEMINI API KEY IS INVALID - check Railway env var")
                        return None
                    continue

                elif r.status_code == 400:
                    logger.warning("Bad request for %s/%s", api_version, model_id)
                    continue

        except Exception as e:
            logger.warning("Exception for %s/%s: %s", api_version, model_id, e)
            continue

    logger.error("❌ All Gemini models failed")
    return None


async def call_groq_vision(image_base64: str, prompt: str) -> Optional[str]:
    """Groq vision fallback"""
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
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                                {"type": "text", "text": prompt}
                            ]
                        }
                    ]
                }
            )
            logger.info("Groq vision status: %s", r.status_code)
            if r.status_code == 200:
                data = r.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"]
            return None
    except Exception as e:
        logger.error("Groq vision error: %s", e)
        return None


def get_mandi_fallback(crop: str, state: str) -> str:
    prices = {"wheat": 2275, "rice": 2183, "maize": 1870, "cotton": 6620, "sugarcane": 315, "soybean": 4600, "mustard": 5650, "groundnut": 6377, "onion": 1500, "potato": 1200, "tomato": 2000}
    msp = prices.get(crop.lower(), 2000)
    return json.dumps({
        "markets": [
            {"market": f"{state} Main Mandi", "price": msp - 50, "unit": "per quintal"},
            {"market": f"{state} Secondary Mandi", "price": msp - 80, "unit": "per quintal"},
            {"market": f"{state} Local Market", "price": msp - 120, "unit": "per quintal"}
        ],
        "msp": msp,
        "best_selling_tip": f"MSP for {crop} is Rs.{msp}/quintal. Compare prices before selling.",
        "date": datetime.utcnow().strftime("%d %b %Y")
    })

def get_weather_fallback(location: str) -> str:
    return json.dumps({
        "temperature": 28, "humidity": 65, "rainfall_chance": 20,
        "spray_suitable": True,
        "farming_advice": "Good conditions for farming. Avoid spraying during afternoon 12-3pm.",
        "best_time_to_work": "Early morning 6-10am",
        "alert": None
    })

FALLBACK_NEWS = [
    {"category": "price", "title": "Wheat MSP ₹2,275/quintal for 2024-25", "summary": "Government has set MSP for wheat at ₹2,275/quintal.", "detail": "The MSP for wheat is ₹2,275 per quintal for the 2024-25 Rabi season.", "impact": "Sell wheat at minimum ₹2,275/quintal.", "action": "Register on your state mandi portal before selling.", "time_ago": "Today"},
    {"category": "scheme", "title": "PM-KISAN — ₹2,000 installment coming soon", "summary": "Check your PM-KISAN status.", "detail": "Over 9 crore farmers receive ₹6,000 per year in 3 installments.", "impact": "₹2,000 will be credited to your bank account.", "action": "Check at pmkisan.gov.in or call 155261.", "time_ago": "Recently"},
    {"category": "scheme", "title": "Kisan Credit Card — crop loan at 4% interest", "summary": "KCC provides easy credit for farming needs.", "detail": "Kisan Credit Card provides credit up to ₹3 lakh at 4% interest.", "impact": "Save money on farming loans.", "action": "Apply at nearest SBI, PNB or cooperative bank.", "time_ago": "This week"},
    {"category": "alert", "title": "Use certified seeds for better yield", "summary": "KVK recommends certified seeds for 20-30% higher yield.", "detail": "Certified seeds ensure better germination and disease resistance.", "impact": "20-30% higher yield with certified seeds.", "action": "Buy seeds only from registered dealers.", "time_ago": "This week"},
    {"category": "scheme", "title": "PM-KUSUM solar pump — 90% subsidy", "summary": "Solar pumps at 10% cost under PM-KUSUM scheme.", "detail": "90% government subsidy on solar pumps. Save ₹20,000-50,000/year on electricity.", "impact": "Free irrigation electricity forever.", "action": "Apply at pmkusum.mnre.gov.in.", "time_ago": "This month"},
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


@app.get("/")
async def root():
    return {"message": "AnnadataHub API is running!", "status": "ok"}

@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "message": "AnnadataHub backend is live!",
        "ai_enabled": bool(GROQ_API_KEY),
        "ai_provider": "Groq (llama-3.3-70b-versatile)",
        "vision_provider": "Google Gemini (auto-discover) + Groq fallback",
        "gemini_enabled": bool(GEMINI_API_KEY)
    }

@app.get("/api/gemini/models")
async def list_gemini_models():
    """Debug endpoint to see available Gemini models"""
    models = await get_available_gemini_models()
    return {"models": models, "count": len(models)}

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


@app.post("/api/crop/scan")
async def scan_crop(request: CropScanRequest, authorization: str = Header(None)):
    lang_map = {
        "hi": "Respond in Hindi.", "pa": "Respond in Punjabi.",
        "mr": "Respond in Marathi.", "te": "Respond in Telugu.",
        "ta": "Respond in Tamil.", "en": "Respond in English."
    }
    lang_instruction = lang_map.get(request.language, "Respond in English.")

    vision_prompt = (
        f"You are an expert agricultural scientist. {lang_instruction} "
        f"Analyze this crop/plant image. Identify disease, pest damage, nutrient deficiency, or if healthy. "
        f"Return ONLY valid JSON: "
        f"{{\"disease\": \"disease name or Healthy\", \"severity\": \"Low/Medium/High/None\", "
        f"\"crop\": \"crop type\", \"confidence\": 85, "
        f"\"treatment\": \"specific treatment steps\", "
        f"\"medicine\": \"medicine name available in India\", "
        f"\"dosage\": \"dosage per litre\", "
        f"\"prevention\": \"prevention tips\", "
        f"\"urgency\": \"Immediate/Within 7 days/No action needed\"}}"
    )

    result = None

    # 1. Try Gemini (with auto-discovery of working model)
    result = await call_gemini_vision(request.image_base64, vision_prompt)

    # 2. Try Groq vision
    if not result:
        logger.info("Gemini failed, trying Groq vision")
        result = await call_groq_vision(request.image_base64, vision_prompt)

    # 3. Text-based diagnosis using crop type (always works)
    if not result:
        logger.info("Vision failed, using text-based diagnosis")
        crop = request.crop_type or "unknown crop"
        text_prompt = (
            f"An Indian farmer's {crop} crop shows disease/pest symptoms. {lang_instruction} "
            f"Give the most common diagnosis for {crop} in India. "
            f"Return ONLY valid JSON: "
            f"{{\"disease\": \"most likely disease\", \"severity\": \"Medium\", "
            f"\"crop\": \"{crop}\", \"confidence\": 65, "
            f"\"treatment\": \"treatment steps\", "
            f"\"medicine\": \"medicine available in India\", "
            f"\"dosage\": \"standard dosage\", "
            f"\"prevention\": \"prevention tips\", "
            f"\"urgency\": \"Within 7 days\"}}"
        )
        result = await call_ai(text_prompt)

    # 4. Final fallback
    if not result:
        result = json.dumps({
            "disease": "Please select crop type and try again",
            "severity": "Unknown",
            "crop": request.crop_type or "Unknown",
            "confidence": 0,
            "treatment": "Select your crop type from the dropdown, then scan again for better diagnosis.",
            "medicine": "N/A",
            "dosage": "N/A",
            "prevention": "For accurate diagnosis visit your nearest KVK — free service.",
            "urgency": "Within 7 days"
        })

    # Clean JSON
    try:
        clean = result.replace("```json", "").replace("```", "").strip()
        idx = clean.find("{")
        if idx > 0:
            clean = clean[idx:]
        json.loads(clean)
        result = clean
    except Exception:
        pass

    if authorization:
        try:
            payload = verify_token(authorization.replace("Bearer ", ""))
            await db.scans.insert_one({
                "_id": str(uuid.uuid4()),
                "user_id": payload["user_id"],
                "result": result,
                "crop_type": request.crop_type,
                "created_at": datetime.utcnow().isoformat()
            })
            await db.users.update_one({"_id": payload["user_id"]}, {"$inc": {"scan_count": 1}})
        except Exception as e:
            logger.warning("Could not save scan: %s", e)

    return {"success": True, "result": result}


@app.post("/api/ai/ask")
async def ask_ai(query: AIQuery):
    cache_key = hashlib.md5(f"{query.question}{query.language}".encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached:
        return {"success": True, "answer": cached, "powered_by": "AnnadataHub AI", "cached": True}

    lang_map = {
        "hi": "हिंदी में जवाब दें।", "pa": "ਪੰਜਾਬੀ ਵਿੱਚ ਜਵਾਬ ਦਿਓ।",
        "mr": "मराठीत उत्तर द्या.", "te": "తెలుగులో సమాధానం.",
        "ta": "தமிழில் பதில்.", "gu": "ગુજરાતીમાં જવાબ.",
        "bn": "বাংলায় উত্তর দিন।", "kn": "ಕನ್ನಡದಲ್ಲಿ ಉತ್ತರ.",
        "ml": "മലയാളത്തിൽ.", "ur": "اردو میں جواب۔", "en": "Reply in English."
    }
    lang = lang_map.get(query.language, "Reply in English.")
    system = query.system_prompt if query.system_prompt else f"You are AnnadataHub AI, a farming assistant for Indian farmers. Give practical, actionable advice specific to India. {lang}"
    result = await call_ai(query.question, system)

    if not result:
        fallback_map = {
            "hi": "AI सेवा अभी व्यस्त है। कृपया 1 मिनट बाद कोशिश करें। किसान हेल्पलाइन: 1800-180-1551",
            "pa": "AI ਸੇਵਾ ਹੁਣੇ ਵਿਅਸਤ ਹੈ। 1 ਮਿੰਟ ਬਾਅਦ ਕੋਸ਼ਿਸ਼ ਕਰੋ। ਕਿਸਾਨ ਹੈਲਪਲਾਈਨ: 1800-180-1551",
        }
        result = fallback_map.get(query.language, "AI service is busy. Please try again in 1 minute. Kisan Helpline: 1800-180-1551 (Free)")
    else:
        cache_set(cache_key, result, hours=6)

    return {"success": True, "answer": result, "powered_by": "AnnadataHub AI"}


@app.get("/api/news")
async def get_news(state: str = "All India", topic: str = "all"):
    cache_key = f"news_{state}_{topic}_{datetime.utcnow().strftime('%Y-%m-%d')}"
    cached = cache_get(cache_key)
    if cached:
        return {"success": True, "news": cached, "source": "cache"}

    rss_news = await fetch_rss_news(state)
    news = rss_news[:2] if rss_news else []

    if len(news) < 5:
        try:
            ai_result = await call_ai(
                f'Generate {5 - len(news)} important agricultural news for {state}, India. Topic: {topic}. Month: {datetime.utcnow().strftime("%B %Y")}. Return ONLY JSON: {{"news":[{{"category":"price/scheme/weather/alert/general","title":"headline","summary":"2 sentences","detail":"3-4 sentences","impact":"farmer impact","action":"what to do","time_ago":"X hours ago"}}]}}',
                "Agricultural news editor for India. Return ONLY valid JSON, no markdown."
            )
            if ai_result:
                clean = ai_result.replace("```json", "").replace("```", "").strip()
                idx = clean.find("{")
                if idx >= 0:
                    ai_data = json.loads(clean[idx:])
                    news.extend(ai_data.get("news", []))
        except Exception as e:
            logger.warning("AI news failed: %s", e)

    if not news:
        news = FALLBACK_NEWS

    cache_set(cache_key, news, hours=6)
    return {"success": True, "news": news, "source": "live"}


@app.get("/api/mandi/prices")
async def mandi_prices(crop: str = "wheat", state: str = "Punjab"):
    cache_key = f"mandi_{crop}_{state}_{datetime.utcnow().strftime('%Y-%m-%d')}"
    cached = cache_get(cache_key)
    if cached:
        return {"success": True, "data": cached, "crop": crop, "state": state}

    result = await call_ai(
        f'Give current mandi prices for {crop} in {state} India. Return ONLY JSON: {{"markets":[{{"market":"name","price":2150,"unit":"per quintal"}}],"msp":2275,"best_selling_tip":"tip","date":"{datetime.utcnow().strftime("%d %b %Y")}"}}'
    )
    if not result:
        result = get_mandi_fallback(crop, state)

    cache_set(cache_key, result, hours=4)
    return {"success": True, "data": result, "crop": crop, "state": state}


@app.get("/api/weather")
async def weather(location: str = "Punjab"):
    WEATHER_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": f"{location},IN", "appid": WEATHER_KEY, "units": "metric"}
            )
            w = r.json()
            if w.get("cod") != 200:
                raise Exception(f"OpenWeather error: {w.get('message')}")
            temp = w["main"]["temp"]
            humidity = w["main"]["humidity"]
            rain = w.get("rain", {}).get("1h", 0)
            desc = w["weather"][0]["description"]
            spray = humidity < 80 and rain == 0
            result = json.dumps({
                "temperature": round(temp), "humidity": humidity,
                "rainfall_chance": min(int(rain * 100), 100),
                "description": desc, "spray_suitable": spray,
                "farming_advice": "Good conditions for farming." if spray else "High humidity or rain. Avoid spraying.",
                "best_time_to_work": "Early morning 6-10am",
                "alert": "Rain detected - protect harvested crops!" if rain > 0 else None
            })
    except Exception as e:
        logger.warning("OpenWeather failed: %s", e)
        result = get_weather_fallback(location)
    return {"success": True, "data": result, "location": location}


@app.get("/api/schemes")
async def govt_schemes(state: str = "Punjab"):
    schemes = [
        {"name": "PM-KISAN", "emoji": "💰", "color": "#2e7d32", "tagline": "₹6,000/year direct to bank", "amount": "₹6,000", "amount_label": "per year", "description": "Direct income support of Rs.6000 per year to all farmer families.", "benefits": ["₹2,000 every 4 months", "Direct bank transfer", "No middlemen"], "documents": ["Aadhar card", "Land records", "Bank passbook"], "how_to_apply": "Visit pmkisan.gov.in or nearest CSC center", "apply_url": "https://pmkisan.gov.in"},
        {"name": "PM Fasal Bima Yojana", "emoji": "🌾", "color": "#f57c00", "tagline": "Crop insurance at lowest premium", "amount": "1.5-2%", "amount_label": "premium only", "description": "Comprehensive crop insurance against flood, drought, pest and disease.", "benefits": ["Full compensation for crop loss", "Kharif premium only 2%", "Rabi premium only 1.5%"], "documents": ["Aadhar card", "Land records", "Bank passbook", "Sowing certificate"], "how_to_apply": "Contact nearest bank before sowing season", "apply_url": "https://pmfby.gov.in"},
        {"name": "Kisan Credit Card", "emoji": "💳", "color": "#1565c0", "tagline": "Crop loan at 4% interest", "amount": "₹3 lakh", "amount_label": "at 4% interest", "description": "Easy credit for crop production at very low interest rate.", "benefits": ["Loan up to Rs.3 lakh", "Interest rate only 4%", "Flexible repayment"], "documents": ["Aadhar card", "Land records", "Bank passbook", "Passport photo"], "how_to_apply": "Apply at nearest SBI, PNB or cooperative bank", "apply_url": "https://www.sbi.co.in/web/agri-rural/agriculture-banking/crop-loan/kisan-credit-card"},
        {"name": "PM Kisan Maan Dhan Yojana", "emoji": "👴", "color": "#6a1b9a", "tagline": "₹3,000/month pension after 60", "amount": "₹3,000", "amount_label": "per month after 60", "description": "Pension scheme for small and marginal farmers.", "benefits": ["₹3,000 monthly pension", "Contribute only ₹55-200/month", "Government matches contribution"], "documents": ["Aadhar card", "Land records", "Bank passbook", "Age proof"], "how_to_apply": "Visit nearest CSC center with documents", "apply_url": "https://maandhan.in"},
        {"name": "Soil Health Card", "emoji": "🌱", "color": "#558b2f", "tagline": "Free soil testing + advice", "amount": "Free", "amount_label": "no cost", "description": "Free soil testing to get crop-wise recommendations.", "benefits": ["Free soil testing", "Fertilizer recommendations", "Reduce input costs by 20%"], "documents": ["Aadhar card", "Land records"], "how_to_apply": "Contact nearest Krishi Vigyan Kendra", "apply_url": "https://soilhealth.dac.gov.in"},
        {"name": "PM-KUSUM Solar Pump", "emoji": "☀️", "color": "#f57f17", "tagline": "90% subsidy on solar pump", "amount": "90%", "amount_label": "subsidy", "description": "Solar water pumps with 90% government subsidy.", "benefits": ["90% subsidy", "Save ₹20,000-50,000/year", "Free irrigation electricity"], "documents": ["Aadhar card", "Land records", "Bank passbook", "Electricity bill"], "how_to_apply": "Apply at pmkusum.mnre.gov.in or district agriculture office", "apply_url": "https://pmkusum.mnre.gov.in"},
    ]
    data = {"summary": f"You are eligible for {len(schemes)} central government schemes.", "schemes": schemes}
    return {"success": True, "data": json.dumps(data)}


@app.get("/api/msp")
async def get_msp(crop: str = "Wheat", state: str = "Punjab"):
    # Real MSP 2024-25 prices from CACP — Cabinet Committee on Economic Affairs
    MSP_PRICES = {
        "Wheat": {"msp": 2275, "season": "Rabi", "procurement": "FCI / State Procurement Agency"},
        "Rice": {"msp": 2183, "season": "Kharif", "procurement": "FCI / State Procurement Agency"},
        "Paddy": {"msp": 2183, "season": "Kharif", "procurement": "FCI / State Procurement Agency"},
        "Maize": {"msp": 1870, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Cotton": {"msp": 6620, "season": "Kharif", "procurement": "CCI (Cotton Corporation of India)"},
        "Soybean": {"msp": 4600, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Mustard": {"msp": 5650, "season": "Rabi", "procurement": "NAFED / State Agency"},
        "Groundnut": {"msp": 6377, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Sugarcane": {"msp": 340, "season": "Annual", "procurement": "Sugar Mills (FRP)"},
        "Moong": {"msp": 8682, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Urad": {"msp": 7400, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Chana": {"msp": 5440, "season": "Rabi", "procurement": "NAFED / State Agency"},
        "Sunflower": {"msp": 6760, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Jowar": {"msp": 3371, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Bajra": {"msp": 2500, "season": "Kharif", "procurement": "NAFED / State Agency"},
        "Ragi": {"msp": 3846, "season": "Kharif", "procurement": "NAFED / State Agency"},
    }

    crop_data = MSP_PRICES.get(crop, MSP_PRICES.get("Wheat"))
    msp_price = crop_data["msp"]

    # State-specific procurement info
    state_portals = {
        "Punjab": "anaajkharid.in", "Haryana": "hsamb.gov.in",
        "Uttar Pradesh": "fcs.up.gov.in", "Madhya Pradesh": "mpeuparjan.nic.in",
        "Rajasthan": "food.raj.nic.in", "Maharashtra": "mahafood.gov.in",
        "Andhra Pradesh": "apagros.com", "Telangana": "pricingtelangana.cgg.gov.in",
    }
    portal = state_portals.get(state, "your state agriculture portal")

    data = {
        "crop": crop,
        "msp_price": msp_price,
        "season": crop_data["season"],
        "procurement_agency": crop_data["procurement"],
        "how_to_sell": f"Register on {portal} before selling. Bring your Aadhaar card, land records (Khasra/Khatauni) and bank passbook to your nearest government procurement centre.",
        "payment_timeline": "3-5 working days — directly to your bank account",
        "documents_needed": ["Aadhaar card", "Land records (Khasra/Khatauni)", "Bank passbook", "Mobile number linked to Aadhaar"],
        "helpline": "1800-180-1551",
        "source": "CACP — Government of India 2024-25",
        "state_portal": portal,
        "important": f"If any trader buys {crop} below ₹{msp_price}/quintal it is illegal. File complaint at your district agriculture office or call 1800-180-1551."
    }
    return {"success": True, "data": json.dumps(data), "crop": crop, "state": state}


@app.get("/api/farmgram/posts")
async def get_posts():
    try:
        posts = await db.farmgram.find().sort("created_at", -1).limit(20).to_list(20)
        for p in posts:
            p["id"] = p.pop("_id")
        return {"success": True, "posts": posts}
    except Exception:
        return {"success": True, "posts": []}


@app.post("/api/farmgram/post")
async def create_post(post: FarmGramPost, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Login required to post")
    payload = verify_token(authorization.replace("Bearer ", ""))
    try:
        user = await db.users.find_one({"_id": payload["user_id"]})
        post_id = str(uuid.uuid4())
        await db.farmgram.insert_one({
            "_id": post_id, "user_id": payload["user_id"],
            "user_name": user["full_name"] if user else "Farmer",
            "user_state": user.get("state", "") if user else "",
            "content": post.content, "crop_type": post.crop_type,
            "location": post.location, "image_base64": post.image_base64,
            "likes": 0, "liked_by": [],
            "created_at": datetime.utcnow().isoformat()
        })
        return {"success": True, "post_id": post_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not create post.")


@app.post("/api/farmgram/like/{post_id}")
async def like_post(post_id: str, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Login required")
    payload = verify_token(authorization.replace("Bearer ", ""))
    try:
        post = await db.farmgram.find_one({"_id": post_id})
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")
        liked_by = post.get("liked_by", [])
        user_id = payload["user_id"]
        if user_id in liked_by:
            await db.farmgram.update_one({"_id": post_id}, {"$inc": {"likes": -1}, "$pull": {"liked_by": user_id}})
            return {"success": True, "action": "unliked"}
        else:
            await db.farmgram.update_one({"_id": post_id}, {"$inc": {"likes": 1}, "$push": {"liked_by": user_id}})
            return {"success": True, "action": "liked"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not like post")


@app.get("/api/user/profile")
async def get_profile(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No token provided")
    payload = verify_token(authorization.replace("Bearer ", ""))
    try:
        user = await db.users.find_one({"_id": payload["user_id"]})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "success": True,
            "user": {
                "id": user["_id"], "email": user["email"],
                "full_name": user["full_name"], "plan": user.get("plan", "free"),
                "scan_count": user.get("scan_count", 0),
                "state": user.get("state", ""), "language": user.get("language", "en")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not fetch profile")
