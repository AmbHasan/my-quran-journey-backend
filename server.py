from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timedelta
import jwt
import bcrypt
import httpx
from enum import Enum
import stripe
import re

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection with connection pooling
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url, maxPoolSize=50, minPoolSize=10, serverSelectionTimeoutMS=5000)
db = client[os.environ['DB_NAME']]

# Create indexes for better performance
async def create_indexes():
    """Create database indexes for better performance"""
    try:
        await db.users.create_index([("email", 1)], unique=True)
        await db.users.create_index([("experience_points", -1)])
        await db.learning_sessions.create_index([("user_id", 1), ("created_at", -1)])
        await db.user_progress.create_index([("user_id", 1), ("surah_number", 1), ("ayah_number", 1)], unique=True)
        logging.info("Database indexes created successfully")
    except Exception as e:
        logging.error(f"Error creating indexes: {e}")

# Create the main app without a prefix
app = FastAPI(
    title="Quran Learning API",
    description="A comprehensive Quran learning platform for English speakers",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Security
security = HTTPBearer()
SECRET_KEY = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Initialize Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

# Rate limiting (basic implementation)
from collections import defaultdict
from time import time
request_counts = defaultdict(list)

def check_rate_limit(client_ip: str, max_requests: int = 100, window_seconds: int = 3600):
    """Basic rate limiting"""
    now = time()
    # Clean old requests
    request_counts[client_ip] = [req_time for req_time in request_counts[client_ip] if now - req_time < window_seconds]
    
    if len(request_counts[client_ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Too many requests")
    
    request_counts[client_ip].append(now)

# Validation functions
def validate_email(email: str) -> bool:
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))

def validate_password(password: str) -> bool:
    """Validate password strength"""
    if len(password) < 8:
        return False
    if not re.search(r'[A-Za-z]', password):
        return False
    if not re.search(r'[0-9]', password):
        return False
    return True

def sanitize_string(input_string: str, max_length: int = 100) -> str:
    """Sanitize input strings"""
    if not input_string:
        return ""
    # Remove potentially harmful characters
    sanitized = re.sub(r'[<>"\';]', '', input_string)
    return sanitized[:max_length].strip()

# Enums
class DifficultyLevel(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"

# Enhanced Models with validation
class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    password_hash: str
    level: int = Field(default=1, ge=1, le=100)
    experience_points: int = Field(default=0, ge=0)
    current_surah: int = Field(default=1, ge=1, le=114)
    current_ayah: int = Field(default=1, ge=1)
    streak_days: int = Field(default=0, ge=0)
    last_activity: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    preferred_reciter: str = Field(default="7")
    is_active: bool = Field(default=True)
    is_premium: bool = Field(default=False)
    plan_type: str = Field(default="free")
    premium_activated_at: Optional[datetime] = None
    stripe_customer_id: Optional[str] = None
    payment_intent_id: Optional[str] = None

class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=8)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserProgress(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    surah_number: int = Field(ge=1, le=114)
    ayah_number: int = Field(ge=1)
    completed: bool = False
    experience_gained: int = Field(default=0, ge=0)
    completed_at: datetime = Field(default_factory=datetime.utcnow)
    difficulty_level: DifficultyLevel

class LearningSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    surah_number: int = Field(ge=1, le=114)
    ayah_number: int = Field(ge=1)
    session_type: str = Field(regex=r'^(reading|memorization|translation|recitation)$')
    duration_minutes: int = Field(ge=1, le=300)
    experience_gained: int = Field(ge=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class LearningSessionCreate(BaseModel):
    surah_number: int = Field(ge=1, le=114)
    ayah_number: int = Field(ge=1)
    session_type: str = Field(regex=r'^(reading|memorization|translation|recitation)$')
    duration_minutes: int = Field(ge=1, le=300)

class UserProgressCreate(BaseModel):
    surah_number: int = Field(ge=1, le=114)
    ayah_number: int = Field(ge=1)
    completed: bool = False
    experience_gained: int = Field(default=0, ge=0)
    difficulty_level: DifficultyLevel

class QuranVerse(BaseModel):
    verse_number: int
    verse_key: str
    text_uthmani: str
    text_simple: str
    translation: str
    transliteration: str
    audio_url: Optional[str] = None

class QuranChapter(BaseModel):
    id: int
    name_simple: str
    name_arabic: str
    verses_count: int
    difficulty_level: DifficultyLevel
    revelation_place: str

# Helper functions
def hash_password(password: str) -> str:
    """Hash password securely"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict):
    """Create JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=30)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm="HS256")
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current authenticated user"""
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=["HS256"])
        user_id: str = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = await db.users.find_one({"id": user_id, "is_active": True})
        if user is None:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        # Remove MongoDB _id field to avoid serialization issues
        user.pop("_id", None)
        return User(**user)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Quran API integration with caching
cached_chapters = None
cache_timestamp = None
CACHE_DURATION = 3600  # 1 hour

async def fetch_quran_chapters():
    """Fetch all Quran chapters from Quran.com API with caching"""
    global cached_chapters, cache_timestamp
    
    # Check cache
    current_time = datetime.utcnow().timestamp()
    if cached_chapters and cache_timestamp and (current_time - cache_timestamp) < CACHE_DURATION:
        return cached_chapters
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://api.quran.com/api/v4/chapters")
            if response.status_code == 200:
                chapters_data = response.json()["chapters"]
                # Add difficulty levels based on chapter length and complexity
                chapters = []
                for chapter in chapters_data:
                    if chapter["verses_count"] <= 10:
                        difficulty = DifficultyLevel.BEGINNER
                    elif chapter["verses_count"] <= 50:
                        difficulty = DifficultyLevel.INTERMEDIATE
                    else:
                        difficulty = DifficultyLevel.ADVANCED
                        
                    chapters.append(QuranChapter(
                        id=chapter["id"],
                        name_simple=chapter["name_simple"],
                        name_arabic=chapter["name_arabic"],
                        verses_count=chapter["verses_count"],
                        difficulty_level=difficulty,
                        revelation_place=chapter["revelation_place"]
                    ))
                
                # Update cache
                cached_chapters = chapters
                cache_timestamp = current_time
                return chapters
    except Exception as e:
        logging.error(f"Error fetching chapters: {e}")
        # Return cached data if available
        if cached_chapters:
            return cached_chapters
    
    return []

async def fetch_quran_verses(chapter_id: int, per_page: int = 50):
    """Fetch verses for a specific chapter with error handling"""
    if not (1 <= chapter_id <= 114):
        raise HTTPException(status_code=400, detail="Invalid chapter ID")
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Fetch verses with translations and transliterations
            verses_response = await client.get(
                f"https://api.quran.com/api/v4/verses/by_chapter/{chapter_id}",
                params={
                    "translations": "131", 
                    "words": "true", 
                    "fields": "text_uthmani,text_simple",
                    "per_page": per_page
                }
            )
            
            if verses_response.status_code == 200:
                verses_data = verses_response.json()["verses"]
                verses = []
                
                for verse in verses_data:
                    # Get transliteration from words
                    transliteration = " ".join([
                        word.get("transliteration", {}).get("text", "") or ""
                        for word in verse.get("words", [])
                        if word.get("transliteration", {}).get("text")
                    ])
                    
                    # Get translation (using first available translation)
                    translation = ""
                    if verse.get("translations"):
                        translation = verse["translations"][0].get("text", "")
                    
                    verses.append(QuranVerse(
                        verse_number=verse["verse_number"],
                        verse_key=verse["verse_key"],
                        text_uthmani=verse.get("text_uthmani", ""),
                        text_simple=verse.get("text_simple", ""),
                        translation=translation,
                        transliteration=transliteration
                    ))
                
                return verses
    except Exception as e:
        logging.error(f"Error fetching verses for chapter {chapter_id}: {e}")
    
    return []

async def fetch_audio_url(chapter_id: int, verse_number: int, reciter: str = "1"):
    """Fetch audio URL for a specific verse with better error handling"""
    if not (1 <= chapter_id <= 114):
        return None
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Convert reciter string to ID if needed
            reciter_id = reciter
            if reciter.startswith("ar."):
                # Map old string IDs to numeric IDs
                reciter_map = {
                    "ar.alafasy": "7",  # Mishary Rashid Alafasy
                    "ar.husary": "1",   # AbdulBaset AbdulSamad (Mujawwad)
                    "ar.minshawi": "2", # AbdulBaset AbdulSamad (Murattal)
                    "ar.muhammed": "3", # Abdur-Rahman as-Sudais
                    "ar.walk": "5"      # Hani ar-Rifai
                }
                reciter_id = reciter_map.get(reciter, "1")
            
            # Try the verse-specific audio endpoint
            response = await client.get(
                f"https://api.quran.com/api/v4/recitations/{reciter_id}/by_chapter/{chapter_id}"
            )
            if response.status_code == 200:
                data = response.json()
                if "audio_files" in data and len(data["audio_files"]) > 0:
                    # Find the specific verse audio
                    for audio_file in data["audio_files"]:
                        if audio_file.get("verse_key") == f"{chapter_id}:{verse_number}":
                            return f"https://verses.quran.com/{audio_file.get('url')}"
                    # If specific verse not found, return the first audio file
                    return f"https://verses.quran.com/{data['audio_files'][0].get('url')}"
    except Exception as e:
        logging.error(f"Error fetching audio for chapter {chapter_id}, verse {verse_number}: {e}")
    
    return None

# Routes with enhanced error handling
@api_router.post("/auth/register")
async def register(user_data: UserCreate):
    """Register a new user with validation"""
    # Validate input
    if not validate_email(user_data.email):
        raise HTTPException(status_code=400, detail="Invalid email format")
    
    if not validate_password(user_data.password):
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long and contain letters and numbers")
    
    # Sanitize username
    sanitized_username = sanitize_string(user_data.username, 30)
    if len(sanitized_username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters long")
    
    try:
        # Check if user already exists
        existing_user = await db.users.find_one({"email": user_data.email})
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Create new user
        user = User(
            email=user_data.email,
            username=sanitized_username,
            password_hash=hash_password(user_data.password)
        )
        
        await db.users.insert_one(user.dict())
        
        # Create access token
        access_token = create_access_token({"user_id": user.id})
        
        # Remove sensitive data from response
        user_dict = user.dict()
        user_dict.pop("password_hash", None)
        
        return {"access_token": access_token, "user": user_dict}
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.post("/auth/login")
async def login(user_data: UserLogin):
    """Login user with validation"""
    try:
        user = await db.users.find_one({"email": user_data.email, "is_active": True})
        if not user or not verify_password(user_data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        access_token = create_access_token({"user_id": user["id"]})
        
        # Remove sensitive data
        user.pop("_id", None)
        user.pop("password_hash", None)
        
        return {"access_token": access_token, "user": user}
    
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.get("/user/profile")
async def get_profile(current_user: User = Depends(get_current_user)):
    """Get user profile"""
    user_dict = current_user.dict()
    user_dict.pop("password_hash", None)
    return user_dict

@api_router.get("/quran/chapters")
async def get_chapters():
    """Get all Quran chapters with difficulty levels"""
    try:
        chapters = await fetch_quran_chapters()
        return chapters
    except Exception as e:
        logging.error(f"Error getting chapters: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch chapters")

@api_router.get("/quran/chapter/{chapter_id}/verses")
async def get_verses(chapter_id: int, per_page: int = 50):
    """Get verses for a specific chapter"""
    if per_page > 100:
        per_page = 100  # Limit to prevent overload
    
    try:
        verses = await fetch_quran_verses(chapter_id, per_page)
        return verses
    except Exception as e:
        logging.error(f"Error getting verses: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch verses")

@api_router.get("/quran/verse/{chapter_id}/{verse_number}/audio")
async def get_verse_audio(chapter_id: int, verse_number: int, reciter: str = "1"):
    """Get audio URL for a specific verse"""
    try:
        audio_url = await fetch_audio_url(chapter_id, verse_number, reciter)
        return {"audio_url": audio_url}
    except Exception as e:
        logging.error(f"Error getting audio: {e}")
        return {"audio_url": None}

@api_router.get("/quran/reciters")
async def get_reciters():
    """Get available reciters"""
    return [
        {"id": "7", "name": "Mishary Rashid Alafasy"},
        {"id": "1", "name": "AbdulBaset AbdulSamad (Mujawwad)"},
        {"id": "2", "name": "AbdulBaset AbdulSamad (Murattal)"},
        {"id": "3", "name": "Abdur-Rahman as-Sudais"},
        {"id": "5", "name": "Hani ar-Rifai"}
    ]

@api_router.post("/learning/session")
async def create_learning_session(
    session_data: LearningSessionCreate,
    current_user: User = Depends(get_current_user)
):
    """Create a new learning session with validation"""
    try:
        # Calculate experience points based on session type and duration
        base_xp = 10
        if session_data.session_type == "memorization":
            base_xp = 20
        elif session_data.session_type == "recitation":
            base_xp = 15
        
        experience_gained = base_xp * session_data.duration_minutes
        
        # Create full session object
        session = LearningSession(
            user_id=current_user.id,
            surah_number=session_data.surah_number,
            ayah_number=session_data.ayah_number,
            session_type=session_data.session_type,
            duration_minutes=session_data.duration_minutes,
            experience_gained=experience_gained
        )
        
        # Save session
        await db.learning_sessions.insert_one(session.dict())
        
        # Update user progress
        new_xp = current_user.experience_points + experience_gained
        new_level = (new_xp // 100) + 1
        
        await db.users.update_one(
            {"id": current_user.id},
            {
                "$set": {
                    "experience_points": new_xp,
                    "level": new_level,
                    "last_activity": datetime.utcnow()
                }
            }
        )
        
        return {"message": "Session created", "experience_gained": experience_gained}
    
    except Exception as e:
        logging.error(f"Error creating session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create session")

@api_router.get("/learning/progress")
async def get_user_progress(current_user: User = Depends(get_current_user)):
    """Get user's learning progress"""
    try:
        progress = await db.user_progress.find({"user_id": current_user.id}, {"_id": 0}).to_list(1000)
        return progress
    except Exception as e:
        logging.error(f"Error getting progress: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch progress")

@api_router.post("/learning/progress")
async def update_progress(
    progress_data: UserProgressCreate,
    current_user: User = Depends(get_current_user)
):
    """Update user's progress on a specific verse"""
    try:
        # Create full progress object
        progress = UserProgress(
            user_id=current_user.id,
            surah_number=progress_data.surah_number,
            ayah_number=progress_data.ayah_number,
            completed=progress_data.completed,
            experience_gained=progress_data.experience_gained,
            difficulty_level=progress_data.difficulty_level
        )
        
        # Check if progress already exists
        existing_progress = await db.user_progress.find_one({
            "user_id": current_user.id,
            "surah_number": progress_data.surah_number,
            "ayah_number": progress_data.ayah_number
        })
        
        if existing_progress:
            await db.user_progress.update_one(
                {"id": existing_progress["id"]},
                {"$set": progress.dict()}
            )
        else:
            await db.user_progress.insert_one(progress.dict())
        
        return {"message": "Progress updated"}
    
    except Exception as e:
        logging.error(f"Error updating progress: {e}")
        raise HTTPException(status_code=500, detail="Failed to update progress")

@api_router.get("/leaderboard")
async def get_leaderboard(limit: int = 10):
    """Get top users by experience points"""
    if limit > 50:
        limit = 50  # Prevent overload
    
    try:
        users = await db.users.find(
            {"is_active": True}, 
            {"_id": 0, "password_hash": 0, "email": 0}
        ).sort("experience_points", -1).limit(limit).to_list(limit)
        
        leaderboard = []
        for i, user in enumerate(users):
            leaderboard.append({
                "rank": i + 1,
                "username": user["username"],
                "level": user["level"],
                "experience_points": user["experience_points"]
            })
        return leaderboard
    
    except Exception as e:
        logging.error(f"Error getting leaderboard: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch leaderboard")

@api_router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Check database connection
        await db.users.find_one()
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "1.0.0"
        }
    except Exception as e:
        logging.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unavailable")

# Payment endpoints
SUBSCRIPTION_PLANS = {
    "premium_monthly": {
        "price_id": "price_1RmOxBDWvZy2eejBM7G4BzGi",
        "amount": 499,  # $4.99 in cents
        "interval": "month",
        "name": "Premium Monthly"
    },
    "premium_yearly": {
        "price_id": "price_1RmOzTDWvZy2eejBPSieKdda",
        "amount": 4999,  # $49.99 in cents
        "interval": "year",
        "name": "Premium Yearly"
    },
    "lifetime": {
        "price_id": "price_1RmP0QDWvZy2eejBTXldmifv",
        "amount": 1999,  # $19.99 in cents
        "interval": "lifetime",
        "name": "Lifetime Access"
    }
}

@api_router.post("/create-payment-intent")
async def create_payment_intent(
    request: dict,
    current_user: User = Depends(get_current_user)
):
    """Create payment intent for purchases"""
    try:
        plan_type = request.get("plan_type")
        if plan_type not in SUBSCRIPTION_PLANS:
            raise HTTPException(status_code=400, detail="Invalid plan type")
        
        plan = SUBSCRIPTION_PLANS[plan_type]
        
        # Check if user already has premium
        if current_user.dict().get("is_premium", False):
            raise HTTPException(status_code=400, detail="User already has premium access")
        
        intent = stripe.PaymentIntent.create(
            amount=plan["amount"],
            currency='usd',
            automatic_payment_methods={'enabled': True},
            metadata={
                'user_id': current_user.id,
                'plan_type': plan_type,
                'user_email': current_user.email
            }
        )
        
        logging.info(f"Payment intent created for user {current_user.id}: {intent.id}")
        
        return {
            "client_secret": intent.client_secret,
            "amount": plan["amount"],
            "currency": "usd"
        }
    
    except stripe.error.StripeError as e:
        logging.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=f"Payment error: {str(e)}")
    except Exception as e:
        logging.error(f"Payment intent creation error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@api_router.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhooks"""
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    if not webhook_secret:
        logging.error("Webhook secret not configured")
        return {"status": "webhook not configured"}
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError as e:
        logging.error(f"Invalid payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        logging.error(f"Invalid signature: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    # Handle payment success
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        user_id = payment_intent['metadata'].get('user_id')
        plan_type = payment_intent['metadata'].get('plan_type')
        
        if user_id:
            # Update user to premium
            await db.users.update_one(
                {"id": user_id},
                {
                    "$set": {
                        "is_premium": True,
                        "plan_type": plan_type,
                        "premium_activated_at": datetime.utcnow(),
                        "payment_intent_id": payment_intent['id']
                    }
                }
            )
            logging.info(f"User {user_id} upgraded to premium via payment intent {payment_intent['id']}")
    
    return {"status": "success"}

@api_router.get("/subscription-status")
async def get_subscription_status(current_user: User = Depends(get_current_user)):
    """Get user's subscription status"""
    user_dict = current_user.dict()
    return {
        "is_premium": user_dict.get("is_premium", False),
        "plan_type": user_dict.get("plan_type", "free"),
        "premium_activated_at": user_dict.get("premium_activated_at"),
    }

# Include the router in the main app
app.include_router(api_router)

# Add security middleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])  # Configure for production

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production with specific domains
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize app on startup"""
    await create_indexes()
    logger.info("Quran Learning API started successfully")

@app.on_event("shutdown")
async def shutdown_db_client():
    """Clean shutdown"""
    client.close()
    logger.info("Database connection closed")
