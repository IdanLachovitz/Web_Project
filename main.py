import requests
import os
import re
import functools
import sqlite3
import hashlib
import time
import threading
from fastapi import FastAPI, HTTPException, Depends, status, Header, BackgroundTasks
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
import json
import math

# Load environment variables from the .env file.
load_dotenv()

# Global Priority for choosing the "Main" genre across the site
GENRE_PRIORITY = ["Shooter", "Racing", "Strategy", "Role-playing (RPG)", "Fighting", "Sport", "Arcade", "Simulator", "Adventure"]

# --- Database Setup (SQLite) ---
DATABASE_FILE = "gamesense.db"

# --- Server-Side Caching ---
GLOBAL_DATA_CACHE = {} # Stores (data, timestamp)

def get_cached_data(key, ttl=1800):
    """Retrieve data from cache if it hasn't expired (default 30 mins)."""
    if key in GLOBAL_DATA_CACHE:
        data, ts = GLOBAL_DATA_CACHE[key]
        if time.time() - ts < ttl:
            return data
    return None

def set_cached_data(key, data):
    GLOBAL_DATA_CACHE[key] = (data, time.time())

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row # This makes rows behave like dicts
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS library_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            UNIQUE(user_id, game_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cached_games (
            game_id INTEGER,
            category TEXT,
            game_json TEXT,
            PRIMARY KEY (game_id, category)
        );
    """)
    conn.commit()
    conn.close()

# Run database initialization on startup
init_db()

# --- Authentication Utilities ---
class AuthRequest(BaseModel):
    username: str
    password: str

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def get_current_user_id(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        token = authorization.split(" ")[1]
        return int(token)
    except (IndexError, ValueError):
        return None

# Twitch/IGDB Credentials
CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

app = FastAPI()

# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global cache for the Twitch token
_token_cache = {"token": None, "expires_at": 0}

def get_access_token():
    global _token_cache
    current_time = time.time()
    
    if _token_cache["token"] and current_time < _token_cache["expires_at"]:
        return _token_cache["token"]

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        response = requests.post(url, params=params)
        response.raise_for_status()
        data = response.json()
        _token_cache["token"] = data.get("access_token")
        # Buffer of 60 seconds before actual expiry
        _token_cache["expires_at"] = current_time + data.get("expires_in", 3600) - 60
        return _token_cache["token"]
    except Exception as e:
        return None

def store_games_in_db(games, category):
    """Saves processed game objects into the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # Clear existing cache for this category to keep it fresh
    cursor.execute("DELETE FROM cached_games WHERE category = ?", (category,))
    
    for game in games:
        cursor.execute(
            "INSERT OR REPLACE INTO cached_games (game_id, category, game_json) VALUES (?, ?, ?)",
            (game["id"], category, json.dumps(game))
        )
    conn.commit()
    conn.close()

def fetch_and_store_category(cat_id: str):
    """Internal helper to fetch data from IGDB and update local DB."""
    token = get_access_token()
    if not token:
        return

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    current_time = int(time.time())

    base_fields = (
        "fields name, summary, total_rating, total_rating_count, first_release_date, "
        "cover.url, platforms.name, platforms.abbreviation, genres.name, "
        "screenshots.url, videos.video_id, involved_companies.developer, involved_companies.company.name;"
    )

    if cat_id == "new-releases":
        filters = f"where first_release_date <= {current_time} & first_release_date > 0 & cover != null;"
        sorting = "sort first_release_date desc;"
    elif cat_id == "top":
        filters = f"where version_parent = null & total_rating_count > 200 & total_rating > 80 & cover != null;"
        sorting = "sort total_rating desc;"
    elif cat_id == "trends":
        filters = f"where version_parent = null & hypes > 10 & first_release_date > {int(time.time()) - (180 * 24 * 60 * 60)} & cover != null;"
        sorting = "sort hypes desc;"
    elif cat_id == "upcoming":
        filters = f"where first_release_date > {current_time} & version_parent = null & cover != null;"
        sorting = "sort first_release_date asc;"
    else:
        return

    query = f"{base_fields} {filters} {sorting} limit 250;" # Increased limit for local storage

    try:
        response = requests.post(url, headers=headers, data=query)
        response.raise_for_status()
        raw_data = response.json()
        processed_data = process_game_data(raw_data)
        
        if cat_id == "top":
            processed_data.sort(key=calculate_top_100, reverse=True)

        store_games_in_db(processed_data, cat_id)
        print(f"Successfully refreshed {cat_id} in database.")
    except Exception as e:
        print(f"Error refreshing {cat_id}: {e}")

def refresh_all_games():
    """Runs the refresh for all categories."""
    print("Starting daily database refresh...")
    categories = ["top", "trends", "upcoming", "new-releases"]
    for cat in categories:
        fetch_and_store_category(cat)
    print("Daily refresh complete.")

def scheduler_loop():
    """Background loop that waits until 8:00 AM every day."""
    while True:
        now = datetime.now()
        # Target is 8:00 AM today
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        
        # If 8 AM has already passed today, target 8 AM tomorrow
        if now >= target:
            target += timedelta(days=1)
        
        sleep_seconds = (target - now).total_seconds()
        print(f"Next database refresh scheduled for {target} (in {round(sleep_seconds/3600, 2)} hours)")
        
        # Sleep until 8 AM (check every hour to be safe, or just sleep the whole duration)
        time.sleep(sleep_seconds)
        refresh_all_games()
        # Sleep for a minute to ensure we don't trigger twice in the same minute
        time.sleep(61)

# Start the scheduler in a separate daemon thread so it doesn't block the web server
@app.on_event("startup")
def start_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    # Also trigger an initial refresh if DB is empty
    threading.Thread(target=refresh_all_games, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Error: index.html not found in project directory.</h1>"

def is_password_strong(password: str) -> bool:
    if len(password) < 8: return False
    if not any(c.islower() for c in password): return False
    if not any(c.isupper() for c in password): return False
    # Matches frontend: Special character or digit
    if not any(c.isdigit() or not c.isalnum() for c in password): return False
    return True

@app.post("/register")
def register(request: AuthRequest):
    if not is_password_strong(request.password):
        raise HTTPException(status_code=400, detail="Password does not meet complexity requirements")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (request.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username already registered")
    
    hashed_password = hash_password(request.password)
    cursor.execute("INSERT INTO users (username, hashed_password) VALUES (?, ?)", (request.username, hashed_password))
    conn.commit()
    conn.close()
    return {"message": "User created successfully"}

@app.post("/login")
def login(request: AuthRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, hashed_password FROM users WHERE username = ?", (request.username,))
    user = cursor.fetchone()
    conn.close()
    
    if not user or hash_password(request.password) != user["hashed_password"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    return {"access_token": user["id"], "token_type": "bearer", "username": user["username"]}

@app.post("/library/add/{game_id}")
def add_to_library(game_id: int, current_user_id: Optional[int] = Depends(get_current_user_id)):
    if current_user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM library_items WHERE user_id = ? AND game_id = ?", (current_user_id, game_id))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Game already in library")
    
    cursor.execute("INSERT INTO library_items (user_id, game_id) VALUES (?, ?)", (current_user_id, game_id))
    conn.commit()
    conn.close()

    # Invalidate server-side caches for this user
    GLOBAL_DATA_CACHE.pop(f"lib_full_{current_user_id}", None)
    GLOBAL_DATA_CACHE.pop(f"recs_{current_user_id}", None)

    return {"message": "Added to library"}

@app.delete("/library/remove/{game_id}")
def remove_from_library(game_id: int, current_user_id: Optional[int] = Depends(get_current_user_id)):
    if current_user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM library_items WHERE user_id = ? AND game_id = ?", (current_user_id, game_id))
    conn.commit()
    conn.close()

    # Invalidate server-side caches for this user
    GLOBAL_DATA_CACHE.pop(f"lib_full_{current_user_id}", None)
    GLOBAL_DATA_CACHE.pop(f"recs_{current_user_id}", None)

    return {"message": "Removed from library"}

@app.get("/library/ids")
def get_library_ids(current_user_id: Optional[int] = Depends(get_current_user_id)):
    if current_user_id is None:
        return []
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT game_id FROM library_items WHERE user_id = ?", (current_user_id,))
    ids = [row["game_id"] for row in cursor.fetchall()]
    conn.close()
    return ids

@app.get("/library")
def get_user_library(current_user_id: Optional[int] = Depends(get_current_user_id)):
    if current_user_id is None:
        return []
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT game_id FROM library_items WHERE user_id = ?", (current_user_id,))
    items = cursor.fetchall()
    conn.close()
    
    game_ids = [item["game_id"] for item in items]
    
    if not game_ids:
        return []

    token = get_access_token()
    if not token:
        return []

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    # Added limit 100 to ensure all library items are returned (IGDB defaults to 10)
    query = f'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id, involved_companies.developer, involved_companies.company.name; where id = ({",".join(map(str, game_ids))}); limit 100;'
    
    try:
        response = requests.post(url, headers=headers, data=query)
        response.raise_for_status()
        data = response.json()
        res = process_game_data(data) if isinstance(data, list) else []
        return res
    except Exception as e:
        print(f"Library Fetch Error: {e}")
        return []

@app.get("/recommendations")
def get_recommendations(current_user_id: Optional[int] = Depends(get_current_user_id)):
    token = get_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Authentication failed.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT game_id FROM library_items WHERE user_id = ?", (current_user_id,))
    user_library_ids = [item["game_id"] for item in cursor.fetchall()]
    conn.close()

    if not user_library_ids:
        return []

    try:
        url = "https://api.igdb.com/v4/games"
        headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
        ids_str = ",".join(map(str, user_library_ids))
        
        lib_res = requests.post(url, headers=headers, data=f'fields genres.name; where id = ({ids_str});')
        lib_data = lib_res.json()
        
        unique_main_genres = set()
        for game in lib_data:
            genres = game.get('genres', [])
            if not genres: continue
            
            best_genre = None
            min_prio = len(GENRE_PRIORITY)
            for g in genres:
                name = g.get('name', "")
                prio = GENRE_PRIORITY.index(name) if name in GENRE_PRIORITY else len(GENRE_PRIORITY)
                if prio < min_prio:
                    min_prio = prio
                    best_genre = g
            
            target = best_genre if best_genre else genres[0]
            gid = target.get('id') if isinstance(target, dict) else target
            if gid:
                unique_main_genres.add(gid)

        if not unique_main_genres:
            return []

        genre_filter = ",".join(map(str, unique_main_genres))
        rec_query = (
            f"fields name, summary, total_rating, total_rating_count, first_release_date, "
            f"cover.url, platforms.name, platforms.abbreviation, genres.name, "
            f"screenshots.url, videos.video_id, involved_companies.developer, involved_companies.company.name; "
            f"where genres = ({genre_filter}) & id != ({ids_str}) & "
            f"total_rating != 70 & total_rating_count > 200 & cover != null; "
            f"limit 500;"
        )
        
        response = requests.post(url, headers=headers, data=rec_query)
        raw_games = response.json()
        if not raw_games:
            return []

        filtered_games = []
        for game in raw_games:
            game_genres = game.get('genres', [])
            if not game_genres: continue
            
            current_best_genre = None
            current_min_prio = len(GENRE_PRIORITY)
            for g in game_genres:
                name = g.get('name', "")
                prio = GENRE_PRIORITY.index(name) if name in GENRE_PRIORITY else len(GENRE_PRIORITY)
                if prio < current_min_prio:
                    current_min_prio = prio
                    current_best_genre = g
            
            final_main_genre = current_best_genre if current_best_genre else game_genres[0]
            main_gid = final_main_genre.get('id') if isinstance(final_main_genre, dict) else final_main_genre
            
            if main_gid in unique_main_genres:
                filtered_games.append(game)

        filtered_games.sort(key=calculate_top_100, reverse=True)
        res = process_game_data(filtered_games[:100])
        return res

    except Exception as e:
        print(f"Error: {e}")
        return []

@app.get("/search/{game_name}")
def search_game(game_name: str):
    token = get_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Authentication failed.")

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    # Increased limit to 100 to support frontend pagination (16 per page)
    query = f'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id, involved_companies.developer, involved_companies.company.name; search "{game_name}"; limit 100;'

    try:
        response = requests.post(url, headers=headers, data=query)
        data = response.json()
        return process_game_data(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/category/{cat_id}")
def get_games_by_category(cat_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT game_json FROM cached_games WHERE category = ?", (cat_id,))
        rows = cursor.fetchall()
        
        if not rows:
            # If DB is empty for this category, try to fetch it once immediately
            conn.close()
            fetch_and_store_category(cat_id)
            # Re-open connection to get newly fetched data
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT game_json FROM cached_games WHERE category = ?", (cat_id,))
            rows = cursor.fetchall()

        games = [json.loads(row["game_json"]) for row in rows]
        
        # Sort logic for specific categories
        if cat_id == "top":
            games.sort(key=calculate_top_100, reverse=True)
        elif cat_id == "new-releases":
            games.sort(key=lambda x: x.get('first_release_date', 0), reverse=True)
            
        return games[:100] # Return the requested amount to the frontend
    finally:
        conn.close()

def calculate_top_100(game):
    rating = game.get('total_rating', 0)
    count = game.get('total_rating_count', 0)
    
    if count == 0:
        return 0

    return rating * (math.log(count) ** 0.4)

def calculate_trending_games(game):
    rating = game.get('total_rating')
    if rating is None:
        rating = 75
        
    popularity = game.get('popularity', 0)
    final_score = rating * math.log10(popularity + 1)
    return final_score

def get_genre_priority(g):
    name = g.get('name') if isinstance(g, dict) else ""
    try:
        return GENRE_PRIORITY.index(name)
    except ValueError:
        return len(GENRE_PRIORITY) # Non-priority genres go to the end

def process_game_data(data):
    if not isinstance(data, list):
        print(f"IGDB Data Error (Expected list, got): {data}")
        return []
        
    # Process Metadata (Visuals, Ratings, Dates) only
    for game in data:
        # Reorder genres based on priority list. Higher priority genres move to index 0.
        if "genres" in game and isinstance(game["genres"], list) and len(game["genres"]) > 0:
            game["genres"].sort(key=get_genre_priority)

        if "cover" in game:
            game["cover_url"] = "https:" + game["cover"]["url"].replace("t_thumb", "t_cover_big")
        else:
            game["cover_url"] = "https://via.placeholder.com/300x400?text=No+Cover"

        if "screenshots" in game:
            game["screenshot_urls"] = ["https:" + s["url"].replace("t_thumb", "t_720p") for s in game["screenshots"]]
        else:
            game["screenshot_urls"] = []

        if "videos" in game and len(game["videos"]) > 0:
            game["trailer_url"] = f"https://www.youtube.com/embed/{game['videos'][0]['video_id']}"
        else:
            game["trailer_url"] = None

        if game.get("total_rating") is not None:
            game["total_rating"] = round(game["total_rating"])

        if "first_release_date" in game:
            dt_object = datetime.fromtimestamp(game["first_release_date"])
            game["release_date_formatted"] = dt_object.strftime("%B %d, %Y")
        else:
            game["release_date_formatted"] = "TBA"

    return data
