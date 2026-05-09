import requests
import os
import re
import functools
import sqlite3
import hashlib
import time
from fastapi import FastAPI, HTTPException, Depends, status, Header
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse
from datetime import datetime
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
import json
import math

# Load environment variables from the .env file
load_dotenv()

# --- Database Setup (SQLite) ---
DATABASE_FILE = "gamesense.db"

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
ITAD_API_KEY = os.getenv("ITAD_API_KEY")

app = FastAPI()

# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GamePriceService:
    def __init__(self, client_id, client_secret, itad_api_key=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.itad_api_key = itad_api_key
        
        # CheapShark API Endpoints
        self.igdb_url = "https://api.igdb.com/v4/games"
        self.cheapshark_games_url = "https://www.cheapshark.com/api/1.0/games"
        self.cheapshark_deals_url = "https://www.cheapshark.com/api/1.0/deals"
        self.headers = {"User-Agent": "GameSense/1.0"}


    def _get_token(self):
        auth_url = f"https://id.twitch.tv/oauth2/token?client_id={self.client_id}&client_secret={self.client_secret}&grant_type=client_credentials"
        try:
            r = requests.post(auth_url, headers=self.headers, timeout=5)
            return r.json().get("access_token")
        except: return None

    def _fetch_itad_price(self, game_name):
        """Fetches prices from IsThereAnyDeal for multiple platforms."""
        platform_prices = {"PC": "N/A", "PlayStation": "N/A", "Xbox": "N/A"}
        if not self.itad_api_key:
            return platform_prices

        try:
            # 1. Search for Game UUID
            search_url = f"https://api.isthereanydeal.com/games/search/v1?key={self.itad_api_key}&title={game_name}"
            search_res = requests.get(search_url, timeout=5)
            search_data = search_res.json()
            
            results = search_data.get("results", []) if isinstance(search_data, dict) else search_data
            if not results:
                return platform_prices
            
            game_id = results[0].get('uuid') or results[0].get('id')
            
            # 2. Get Prices for that UUID
            price_url = f"https://api.isthereanydeal.com/games/prices/v2?key={self.itad_api_key}&uuids={game_id}&nondeals=1"
            price_res = requests.get(price_url, timeout=5)
            price_data = price_res.json()
            
            # Extract deals list
            deals = price_data.get("data", {}).get(game_id, {}).get("list", [])
            
            for deal in deals:
                shop_name = deal.get("shop", {}).get("name", "").lower()
                price_val = deal.get("price_new") or deal.get("price_old")
                
                if price_val is not None:
                    formatted_price = f"${float(price_val):.2f}"
                    
                    # Simple shop-to-platform mapping
                    if any(x in shop_name for x in ["steam", "gog", "epic", "humble"]):
                        if platform_prices["PC"] == "N/A": platform_prices["PC"] = formatted_price
                    elif "playstation" in shop_name:
                        if platform_prices["PlayStation"] == "N/A": platform_prices["PlayStation"] = formatted_price
                    elif "xbox" in shop_name or "microsoft" in shop_name:
                        if platform_prices["Xbox"] == "N/A": platform_prices["Xbox"] = formatted_price

        except Exception as e:
            print(f"ITAD Error for {game_name}: {e}")
        
        return platform_prices

    @functools.lru_cache(maxsize=128)
    def fetchPrices(self, game_name):
        # Start with ITAD for multi-platform coverage
        prices = self._fetch_itad_price(game_name)
        
        # Fallback to CheapShark for PC if ITAD failed to find a PC price
        if prices["PC"] == "N/A":
            pc_price = self._fetch_cheapshark_pc_price(game_name)
            if pc_price != "N/A":
                prices["PC"] = pc_price
                
        return {"game": game_name, "live_prices": prices}

    def _fetch_cheapshark_pc_price(self, game_name):
        """Fallback helper for PC prices using CheapShark."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        query_name = re.sub(r'\s*[\(\[].*?[\)\]]', '', game_name).strip()
        try:
            search_res = requests.get(
                self.cheapshark_games_url, 
                params={"title": query_name}, 
                headers=headers, 
                timeout=5
            )
            
            if search_res.status_code == 200:
                search_data = search_res.json()
                if not search_data:
                    return "N/A" # Return N/A if no game found on CheapShark
                
                # Get the internal CheapShark ID for the best match
                cheapshark_game_id = search_data[0].get('gameID')

                lookup_res = requests.get(
                    self.cheapshark_games_url, 
                    params={"id": cheapshark_game_id}, 
                    headers=headers, 
                    timeout=5
                )
                lookup_res.raise_for_status() # Raise an exception for HTTP errors
                
                if lookup_res.status_code == 200:
                    game_info = lookup_res.json()
                    
                    # Initialize pc_price_str for this scope
                    pc_price_str = "N/A"

                    # The price for the overall cheapest deal is usually here:
                    cheapest_price = game_info.get('cheapestPriceEver', {}).get('price')
                    
                    # Alternatively, check current active deals list
                    deals = game_info.get('deals', [])
                    if deals:
                        # Find the lowest price among current active deals
                        current_min = min(float(d['price']) for d in deals if 'price' in d)
                        pc_price_str = f"${current_min:.2f}"
                    elif cheapest_price:
                        pc_price_str = f"${float(cheapest_price):.2f}"
                    return pc_price_str

        except requests.exceptions.RequestException as e:
            print(f"DEBUG CheapShark Request Error for {game_name}: {e}")
        except Exception as e:
            print(f"DEBUG CheapShark Processing Error for {game_name}: {e}")
        return "N/A" # Default return if anything fails

@app.get("/api/v1/game-price-check")
def api_price_check(q: str):
    """
    Exposes the GamePriceService via a clean API endpoint.
    """
    return global_price_service.fetchPrices(q)

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


@app.get("/", response_class=HTMLResponse)
def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Error: index.html not found in project directory.</h1>"

@app.post("/register")
def register(request: AuthRequest):
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
    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    query = f'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id; where id = ({",".join(map(str, game_ids))});'
    
    try:
        response = requests.post(url, headers=headers, data=query)
        return process_game_data(response.json())
    except Exception:
        return []

@app.get("/recommendations")
def get_recommendations(current_user_id: Optional[int] = Depends(get_current_user_id)):
    token = get_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Authentication failed.")

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}

    user_library_ids = []
    if current_user_id:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT game_id FROM library_items WHERE user_id = ?", (current_user_id,))
        items = cursor.fetchall()
        conn.close()
        user_library_ids = [item["game_id"] for item in items]

    if user_library_ids:
        try:
            library_query = f'fields genres; where id = ({",".join(map(str, user_library_ids))});'
            lib_res = requests.post(url, headers=headers, data=library_query)
            lib_data = lib_res.json()
            
            genres = list(set([g for game in lib_data for g in game.get('genres', [])]))
            if genres:
                rec_query = f'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id; where genres = ({",".join(map(str, genres[:3]))}) & total_rating > 80; sort total_rating desc; limit 16;'
                response = requests.post(url, headers=headers, data=rec_query)
                data = response.json()
                if data: return process_game_data(data)
        except Exception:
            pass

    # Fallback for guest or empty library
    fallback_query = 'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id; where total_rating > 85; sort total_rating desc; limit 16;'
    response = requests.post(url, headers=headers, data=fallback_query)
    return process_game_data(response.json())

@app.get("/search/{game_name}")
def search_game(game_name: str):
    token = get_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Authentication failed.")

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    # Increased limit to 100 to support frontend pagination (16 per page)
    query = f'fields name, summary, total_rating, total_rating_count, first_release_date, cover.url, platforms.name, platforms.abbreviation, genres.name, screenshots.url, videos.video_id; search "{game_name}"; limit 100;'

    try:
        response = requests.post(url, headers=headers, data=query)
        data = response.json()
        return process_game_data(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/category/{cat_id}")
def get_games_by_category(cat_id: str):
    token = get_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Authentication failed.")

    url = "https://api.igdb.com/v4/games"
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    current_time = int(time.time())

    base_fields = (
        "fields name, summary, total_rating, total_rating_count, first_release_date, "
        "cover.url, platforms.name, platforms.abbreviation, genres.name, "
        "screenshots.url, videos.video_id;"
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
        raise HTTPException(status_code=404, detail="Category not found")


    query = f"{base_fields} {filters} {sorting} limit 100;"

    try:
        response = requests.post(url, headers=headers, data=query)
        response.raise_for_status()
        data = response.json()
        with open("debug_data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        
        if cat_id == "top" and data:
            print(f"Top Game #1: {data[0].get('name')}")
            data.sort(key=calculate_top_100, reverse=True)
            return process_game_data(data[:100])
        

        return process_game_data(data)
    except Exception as e:
        print(f"Error fetching category {cat_id}: {e}")
        return []

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

# Global instance to ensure lru_cache persists across requests
global_price_service = GamePriceService(CLIENT_ID, CLIENT_SECRET, ITAD_API_KEY)

def process_game_data(data):
    if not isinstance(data, list):
        print(f"IGDB Data Error (Expected list, got): {data}")
        return []
        
    # Process Metadata (Visuals, Ratings, Dates) only
    for game in data:
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

        # Set default price fields (to be filled by frontend)
        game["price"] = "N/A"
        game["all_prices"] = {"PC": "N/A", "PlayStation": "N/A", "Xbox": "N/A"}

    return data
