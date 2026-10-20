"""
KusinaAI Digital Cookbook: ISANG FILE LANG (AI + calories/sakit + login/signup + database).

Environment variables (ilagay sa .env locally, o sa Render > Environment):
  GEMINI_API_KEY  - API key mula sa Google AI Studio  (kailangan para gumana ang AI)
  SECRET_KEY      - mahabang random na text para sa login session (kailangan sa production)
  DATABASE_URL    - (optional) Postgres URL para hindi mawala ang accounts. Kung wala, SQLite ang gagamitin.
  GEMINI_MODEL    - (optional) piliin ang model na uunahin
"""
import os
import re
import json
import time
import random
import secrets
import difflib
from functools import wraps
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session
import importlib

try:
    SQLAlchemy = importlib.import_module("flask_sqlalchemy").SQLAlchemy
except ImportError as exc:
    raise ImportError(
        "Flask-SQLAlchemy is required. Install it with: "
        "python -m pip install Flask-SQLAlchemy"
    ) from exc

try:
    IntegrityError = importlib.import_module("sqlalchemy.exc").IntegrityError
except ImportError as exc:
    raise ImportError(
        "SQLAlchemy is required. Install it with: "
        "python -m pip install SQLAlchemy"
    ) from exc
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
_secret = os.getenv("SECRET_KEY")
if not _secret:
    print("[WARNING] Walang SECRET_KEY. Random muna ang gagamitin, kaya mala-logout ang lahat tuwing mag-restart. "
          "Maglagay ng SECRET_KEY sa environment.")
    _secret = secrets.token_hex(32)

app.config.update(
    SECRET_KEY=_secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")) or os.getenv("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=1024 * 1024,
)

_db_url = os.getenv("DATABASE_URL") or "sqlite:///" + os.path.join(BASE_DIR, "kusina.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
db = SQLAlchemy(app)

api_key = os.getenv("GEMINI_API_KEY")
client = (
    genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=45000))
    if api_key else None
)

PH_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# DATABASE MODELS (dito nase-save ang data ng mga nag-sign up)
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    plan_json = db.Column(db.Text, default="")  # naka-save na meal plan


class Favorite(db.Model):
    __tablename__ = "favorites"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipe_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "recipe_id", name="uq_user_recipe"),)


with app.app_context():
    db.create_all()

# ---------------------------------------------------------------------------
# RECIPES DATABASE (recipes.csv)
# ---------------------------------------------------------------------------
COLUMNS = ['id', 'title', 'category', 'ingredients', 'instructions',
           'budget_level', 'best_for', 'image_url']

try:
    _csv = os.path.join(BASE_DIR, 'recipes.csv')
    df = pd.read_csv(_csv) if os.path.exists(_csv) else pd.DataFrame(columns=COLUMNS)
except Exception as _e:
    print(f"[ERROR] Hindi mabasa ang recipes.csv: {_e}")
    df = pd.DataFrame(columns=COLUMNS)

if len(df) == 0:
    print("[WARNING] Walang laman o nawawala ang recipes.csv.")

for _c in COLUMNS:
    if _c not in df.columns:
        df[_c] = ''
df = df.fillna('').reset_index(drop=True)
_ids = pd.to_numeric(df['id'], errors='coerce')
df['id'] = _ids.where(_ids.notna(), pd.Series(range(1, len(df) + 1))).astype(int)
df['instructions'] = df['instructions'].astype(str).str.replace('\\n', '\n', regex=False)

# --- larawan: gamitin lang kung talagang nandoon ang file sa static/images ---
RECIPE_IMAGES = {
    "Adobong Manok": "/static/images/adobo.jpg",
    "Sinigang na Baboy": "/static/images/download.jpg",
    "Tinolang Manok": "/static/images/images (1).jpg",
    "Ginisang Munggo na may Malunggay": "/static/images/images (2).jpg",
    "Tortang Talong": "/static/images/torta.jpg",
    "Chicken Curry": "/static/images/EasyChickenCurry3.jpg",
    "Pork Sisig": "/static/images/images (3).jpg",
    "Champorado": "/static/images/champorado-7-scaled.jpg",
}
_exists_cache = {}


def local_image(title):
    t = str(title).lower()
    for key, url in RECIPE_IMAGES.items():
        if key.lower() in t:
            if url not in _exists_cache:
                _exists_cache[url] = os.path.exists(os.path.join(app.root_path, url.lstrip('/')))
            return url if _exists_cache[url] else ''
    return ''


df['image_url'] = df['title'].map(local_image)

# ---------------------------------------------------------------------------
# CALORIES / PAMPAPAYAT / SAKIT  (dating hiwalay na file, nandito na)
# ---------------------------------------------------------------------------
SLIM_TARGET = 1500  # tantiyang kcal/araw ng nagpapapayat

DISH_KCAL = [
    ('lechon kawali', 650), ('bangus sisig', 380), ('sisig', 480), ('bicol express', 520),
    ('kare', 560), ('dinuguan', 380), ('bulalo', 480), ('caldereta', 520), ('kaldereta', 520),
    ('menudo', 420), ('afritada', 400), ('adobo', 430), ('sinigang', 280), ('tinola', 250),
    ('nilaga', 350), ('paksiw', 280), ('pinakbet', 180), ('chopsuey', 170), ('chop suey', 170),
    ('tortang talong', 220), ('torta', 240), ('munggo', 230), ('kangkong', 90), ('sayote', 120),
    ('ginataang kalabasa', 260), ('kalabasa', 230), ('ginataang', 320), ('pancit', 380),
    ('fried chicken', 480), ('arroz caldo', 300), ('lugaw', 210), ('goto', 280),
    ('champorado', 320), ('tapsilog', 650), ('longsilog', 680), ('tocilog', 650), ('silog', 650),
    ('turon', 250), ('banana cue', 280), ('bibingka', 280), ('kutsinta', 130), ('puto', 120),
    ('lumpia', 220), ('halo-halo', 350), ('giniling', 380), ('curry', 450), ('tuyo', 200),
    ('daing', 300), ('longganisa', 320), ('tocino', 330),
]

HEAVY = re.compile(r'baboy|liempo|baka|gata|chicharon|bacon|hotdog|keso|mantikilya')
VEG = re.compile(r'gulay|talong|okra|sitaw|repolyo|petsay|kangkong|ampalaya|sayote|labanos')
NOT_LEAN = re.compile(r'prito|fried|liempo|gata|chicharon|bacon|asukal|condensada')


def estimate_kcal(title, ingredients=''):
    t, ing = str(title).lower(), str(ingredients).lower()
    for key, val in DISH_KCAL:
        if key in t:
            return val
    k = 300
    if HEAVY.search(ing): k += 120
    if re.search(r'prito|fried|deep', t + ing): k += 100
    if re.search(r'asukal|sugar|condensada|gatas', ing): k += 60
    if VEG.search(ing): k -= 70
    return max(120, min(700, k))


df['kcal'] = [estimate_kcal(t, i) for t, i in zip(df['title'], df['ingredients'])]
_lean_text = (df['title'].astype(str) + ' ' + df['ingredients'].astype(str)).str.lower()
df['lean'] = (df['kcal'] <= 350) & ~_lean_text.str.contains(NOT_LEAN.pattern, regex=True)

CONDITIONS = [
    dict(name='Lagnat / Trangkaso', keys=['lagnat', 'trangkaso', 'flu', 'fever', 'may sakit', 'masama ang pakiramdam'],
         match='lugaw|arroz caldo|goto|tinola|nilaga|sinigang|champorado',
         eat='maiinit na sabaw at malalambot na pagkain (lugaw, arroz caldo, tinola); maraming tubig',
         avoid='mamantika, sobrang maalat, matatamis na inumin',
         warn='magpatingin kung higit 3 araw ang lagnat o sobrang taas'),
    dict(name='Ubo / Sipon / Masakit na Lalamunan', keys=['ubo', 'sipon', 'lalamunan', 'sore throat', 'plema', 'cough'],
         match='tinola|lugaw|arroz caldo|nilaga|sinigang|luya',
         eat='tinolang maraming luya, lugaw, sabaw; maligamgam na tubig na may kalamansi at pulot',
         avoid='malalamig, mamantika, pritong pagkain',
         warn='magpatingin kung higit isang linggo ang ubo o nahihirapang huminga'),
    dict(name='LBM / Pagtatae', keys=['lbm', 'pagtatae', 'diarrhea', 'tumatae', 'masakit ang tiyan'],
         match='lugaw|arroz caldo|champorado|tinola',
         eat='lugaw, plain na kanin, saging, malinaw na sabaw; oresol/tubig para hindi ma-dehydrate',
         avoid='maanghang, mamantika, may gata, hilaw na pagkain',
         warn='magpatingin agad kung may dugo sa dumi, higit 2 araw, o nahihilo at tuyo ang bibig'),
    dict(name='Pagsusuka / Nausea', keys=['nasusuka', 'nausea', 'nagsusuka', 'pagsusuka', 'nahihilo'],
         match='lugaw|arroz caldo|tinola|champorado',
         eat='kaunting kain pero madalas: lugaw, crackers, sabaw; higop-higop ng tubig',
         avoid='malalangis at matapang ang amoy',
         warn='magpatingin kung hindi tumitigil ang pagsusuka'),
    dict(name='Diabetes / Mataas ang Sugar', keys=['diabetes', 'diabetic', 'mataas ang sugar', 'blood sugar'],
         match='pinakbet|chopsuey|chop suey|tinola|sinigang|kangkong|sayote|paksiw|nilaga|munggo',
         eat='maraming gulay, isda, manok na hindi prito; maliit na kanin lang',
         avoid='matatamis, softdrinks, puting tinapay, sobrang kanin',
         warn='sundin ang diet plan ng doktor o nutritionist'),
    dict(name='High Blood / Altapresyon', keys=['high blood', 'altapresyon', 'hypertension', 'mataas ang bp', 'mataas ang blood pressure'],
         match='pinakbet|chopsuey|chop suey|tinola|sinigang|kangkong|sayote|nilaga|munggo',
         eat='gulay, prutas, isda, lutong hindi masyadong maalat; kalamansi at bawang para sa lasa',
         avoid='tuyo, daing, sobrang toyo/patis, instant noodles, processed meat',
         warn='regular na magpa-check ng BP at uminom ng gamot ayon sa doktor'),
    dict(name='Mataas ang Kolesterol', keys=['kolesterol', 'cholesterol'],
         match='pinakbet|chopsuey|chop suey|tinola|sinigang|paksiw|kangkong|sayote|munggo',
         eat='isda, gulay, munggo, manok na walang balat; inihaw, nilaga o sinabawan',
         avoid='taba ng baboy, lechon, chicharon, pritong pagkain',
         warn='magpa-lab test at sundin ang payo ng doktor'),
    dict(name='Ulcer / Hyperacidity', keys=['ulcer', 'hyperacidity', 'acid reflux', 'gerd', 'hapdi ng sikmura'],
         match='lugaw|arroz caldo|tinola|champorado|nilaga',
         eat='lugaw, tinola, nilaga, malalambot na pagkain; kaunti pero madalas',
         avoid='maanghang, maasim, kape, softdrinks',
         warn='magpatingin kung madalas at matindi ang sakit ng sikmura'),
    dict(name='Dengue', keys=['dengue'],
         match='lugaw|arroz caldo|tinola|nilaga',
         eat='maraming likido (tubig, sabaw, oresol) at malalambot na pagkain tulad ng lugaw',
         avoid='huwag mag-self-medicate ng aspirin o ibuprofen',
         warn='emergency ang dengue: pumunta agad sa ospital kung may pagdurugo, matinding sakit ng tiyan, o patuloy na pagsusuka'),
]

SLIM_KEYS = ['pampapayat', 'papayat', 'pumayat', 'magpapayat', 'nagpapapayat', 'diet',
             'low calorie', 'low cal', 'mababang calorie', 'calorie', 'weight loss']


def detect_health(query):
    q = query.lower()
    best, best_len = None, 0
    for c in CONDITIONS:
        for k in c['keys']:
            if re.search(r'\b' + re.escape(k) + r'\b', q) and len(k) > best_len:
                best, best_len = c, len(k)
    return best


def is_slim(query):
    q = query.lower()
    return any(k in q for k in SLIM_KEYS)


def health_prompt(query):
    cond, slim = detect_health(query), is_slim(query)
    if not cond and not slim:
        return ''
    parts = ['\nPANGUNAHING LAYUNIN: ito ay tanong tungkol sa kalusugan at pagkain. Unahin ito kaysa sa ibang patakaran sa itaas.']
    if cond:
        parts.append(
            f"Kondisyon ng user: {cond['name']}.\n"
            f"Gabay: pwedeng kainin: {cond['eat']}. Iwasan: {cond['avoid']}. Paalala: {cond['warn']}.\n"
            "Ibigay ang: ✅ Pwedeng kainin, 🚫 Iwasan, 3 Pilipinong putahe na bagay (may sangkap at maikling paraan, "
            "at tinatayang calories bawat serving), at ⚠️ kailan dapat magpatingin sa doktor.\n"
            "HUWAG mag-diagnose at huwag magreseta ng gamot. Magtapos sa paalalang hindi ito pamalit sa payo ng doktor."
        )
    if slim:
        parts.append(
            f"Para sa nagpapapayat: piliin ang sinabawan, inihaw, at gulay; iwasan ang prito at may gata. "
            f"Ang tantiyang target ay mga {SLIM_TARGET} kcal bawat araw (iba-iba bawat tao). "
            "Ilagay ang tinatayang kcal bawat putahe at kabuuang kcal ng araw kung meal plan. "
            "Magtapos sa paalalang kumonsulta sa nutritionist para sa tamang plano."
        )
    return '\n'.join(parts) + '\n'


def health_text(query):
    """Sagot na hindi na kailangan ng AI (gamit kapag hindi maabot ang Gemini)."""
    cond, slim = detect_health(query), is_slim(query)
    if not cond and not slim:
        return ''
    out = []
    if cond:
        w = cond['warn'][0].upper() + cond['warn'][1:]
        out.append(f"🩺 {cond['name']}\n\n✅ Pwedeng kainin: {cond['eat']}.\n🚫 Iwasan: {cond['avoid']}.\n⚠️ {w}.")
    if slim:
        out.append(f"🥗 Pampapayat: piliin ang sinabawan, inihaw, o gulay at iwasan ang prito at may gata. "
                   f"Tantiyang target: mga {SLIM_TARGET} kcal bawat araw (iba-iba bawat tao).")
    out.append("Gabay lang ito at hindi pamalit sa payo ng doktor o nutritionist.")
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# PINAL NA DATA NA IPAPADALA SA BROWSER
# ---------------------------------------------------------------------------
SEARCH_TEXT = (
    df['title'].astype(str) + ' ' + df['category'].astype(str) + ' ' + df['ingredients'].astype(str)
).str.lower()

PUBLIC_COLS = ['id', 'title', 'category', 'ingredients', 'instructions',
               'budget_level', 'best_for', 'image_url', 'kcal', 'lean']
ALL_RECORDS = json.loads(df[PUBLIC_COLS].to_json(orient='records', force_ascii=False))
BY_ID = {r['id']: r for r in ALL_RECORDS}
CATEGORIES = sorted({str(c) for c in df['category'] if str(c)})


def records(sub):
    return [BY_ID[i] for i in sub['id'].tolist() if i in BY_ID]


def health_recipes(query, limit=20):
    cond, slim = detect_health(query), is_slim(query)
    if not cond and not slim:
        return pd.DataFrame(columns=df.columns)
    if cond:
        pool = df[SEARCH_TEXT.str.contains(cond['match'], regex=True)]
        if slim:
            pool = pool[pool['lean']]
        return pool.sample(n=min(limit, len(pool))) if len(pool) else pool
    return df[df['lean']].sort_values('kcal').head(limit)


# ---------------------------------------------------------------------------
# PAG-INTINDI SA TANONG (anong kainan, ilang putahe, anong araw)
# ---------------------------------------------------------------------------
DAY_NAMES = ['Lunes', 'Martes', 'Miyerkules', 'Huwebes', 'Biyernes', 'Sabado', 'Linggo']
MONTH_NAMES = ['Enero', 'Pebrero', 'Marso', 'Abril', 'Mayo', 'Hunyo', 'Hulyo', 'Agosto',
               'Setyembre', 'Oktubre', 'Nobyembre', 'Disyembre']

MEAL_KEYWORDS = {
    'umagahan':   ['umagahan', 'almusal', 'agahan', 'breakfast', 'umaga'],
    'tanghalian': ['tanghalian', 'tanghali', 'lunch'],
    'meryenda':   ['meryenda', 'merienda', 'miryenda', 'snack'],
    'hapunan':    ['hapunan', 'hapunin', 'dinner', 'gabi'],
}
MEAL_LABEL = {'umagahan': 'Umagahan (Almusal)', 'tanghalian': 'Tanghalian',
              'meryenda': 'Meryenda', 'hapunan': 'Hapunan'}
MEAL_GUIDE = {
    'umagahan': "Pang-umagahan: karaniwang almusal ng Pilipino (sinangag na may itlog at ulam, silog meals, lugaw/arroz caldo, "
                "champorado at tuyo, pandesal na may palaman). Mabilis lutuin at magaan hanggang katamtaman ang bigat.",
    'tanghalian': "Pang-tanghalian: mabigat at kumpletong pagkain na swak sa kanin (ulam na may sabaw o gulay). Busog at masustansya.",
    'meryenda': "Pang-meryenda: maliit na serving na pang-hapon (turon, banana cue, pancit, puto, bibingka, lugaw, kakanin, sandwich). "
                "HUWAG ulam na pang-kanin.",
    'hapunan': "Pang-hapunan: ulam na pampamilya na masarap ihain sa gabi, hindi masyadong mabigat para sa tulog, may sabaw o gulay kung maaari.",
}
MEAL_FILTERS = {
    'umagahan': {'keywords': ['champorado', 'arroz caldo', 'lugaw', 'goto', 'tapsilog', 'longsilog', 'tocilog',
                              'sinangag', 'tortang', 'pandesal', 'daing', 'tuyo'], 'categories': ['Almusal']},
    'tanghalian': {'keywords': [], 'categories': ['Ulam', 'Pritong Ulam', 'Gulay', 'Sabaw']},
    'meryenda': {'keywords': ['champorado', 'turon', 'banana', 'bibingka', 'puto', 'kutsinta', 'lugaw', 'pancit'],
                 'categories': ['Meryenda', 'Pansit']},
    'hapunan': {'keywords': [], 'categories': ['Ulam', 'Sabaw', 'Gulay']},
}
SLOT_TO_MEAL = {'umaga': 'umagahan', 'tanghali': 'tanghalian', 'meryenda': 'meryenda', 'hapunan': 'hapunan'}
SLOTS = list(SLOT_TO_MEAL)

NUM_WORDS = {'isa': 1, 'dalawa': 2, 'tatlo': 3, 'apat': 4, 'lima': 5, 'anim': 6, 'pito': 7}
COUNT_RE = re.compile(
    r'(\d+|isa|dalawa|tatlo|apat|lima|anim|pito)(?:ng)?\s*(?:na\s*)?'
    r'(?:putahe|ulam|resipi|recipe|ideya|idea|mungkahi|suggestion|dish)')
DAY_ALIASES = ['lunes', 'martes', 'miyerkules', 'huwebes', 'biyernes', 'sabado', 'linggo']
KW_TO_MEAL = {kw: meal for meal, kws in MEAL_KEYWORDS.items() for kw in kws}


def fmt_date(d):
    return f"{DAY_NAMES[d.weekday()]}, {MONTH_NAMES[d.month - 1]} {d.day}"


def _words(q):
    return re.findall(r'\w+', q.lower())


def _fuzzy(word, targets, cutoff=0.75):
    if len(word) < 5:
        return None
    m = difflib.get_close_matches(word, targets, n=1, cutoff=cutoff)
    return m[0] if m else None


def plan_days(intent):
    now = datetime.now(PH_TZ).date()
    rng = intent.get('day_range')
    if rng:
        start = now + timedelta(days=(rng[0] - now.weekday()) % 7)
        n = rng[1]
    else:
        n = 7
        start = now + timedelta(days=7 - now.weekday()) if intent['next_week'] else now + timedelta(days=1)
    return [fmt_date(start + timedelta(days=i)) for i in range(n)]


def meal_mask(meals):
    cats, kws = set(), set()
    for m in meals:
        cats.update(MEAL_FILTERS[m]['categories'])
        kws.update(MEAL_FILTERS[m]['keywords'])
    mask = df['category'].isin(cats)
    if kws:
        mask = mask | SEARCH_TEXT.str.contains('|'.join(re.escape(k) for k in kws), regex=True)
    return mask


def detect_intent(query):
    q = query.lower()
    words = _words(q)

    found = set()
    for name, kws in MEAL_KEYWORDS.items():
        if any(k in q for k in kws):
            found.add(name)
    for w in words:
        hit = _fuzzy(w, list(KW_TO_MEAL), 0.8)
        if hit:
            found.add(KW_TO_MEAL[hit])
    meals = [m for m in MEAL_LABEL if m in found]
    if not meals and any(w in q for w in ['lahat', 'buong araw', 'bawat kainan', 'meal plan', 'mealplan']):
        meals = list(MEAL_LABEL)

    count = 1
    m = COUNT_RE.search(q)
    if m:
        tok = m.group(1)
        count = max(1, min(NUM_WORDS.get(tok) or int(tok), 10))

    day_hits = []
    for w in words:
        d = _fuzzy(w, DAY_ALIASES)
        if d:
            day_hits.append(DAY_ALIASES.index(d))
    day_range = None
    if len(day_hits) >= 2 and day_hits[0] != day_hits[-1]:
        day_range = (day_hits[0], (day_hits[-1] - day_hits[0]) % 7 + 1)

    if any(w in q for w in ['buwan', 'month', '30 araw', '30 days']):
        span = 'buwan'
    elif day_range:
        span = 'linggo'
    elif 'week' in words or any(w in q for w in ['next week', 'susunod na linggo', 'isang linggo',
                                                  'lingguhan', 'weekly', '7 araw', '7 days']):
        span = 'linggo'
    elif any(w in q for w in ['bukas', 'kinabukasan', 'tomorrow']):
        span = 'bukas'
    elif any(w in q for w in ['meal plan', 'mealplan']):
        span = 'linggo'
    else:
        span = 'ngayon'

    return {
        'meals': meals,
        'meal': meals[0] if len(meals) == 1 else None,
        'count': count,
        'span': span,
        'day_range': day_range if span == 'linggo' else None,
        'next_week': any(w in q for w in ['next week', 'susunod na linggo']),
    }


def build_prompt(user_query, intent):
    now = datetime.now(PH_TZ).date()
    tomorrow = now + timedelta(days=1)
    meal, span = intent['meal'], intent['span']
    days = plan_days(intent) if span == 'linggo' else []

    rules = f"""Ikaw ang KusinaAI, isang malikhaing Pilipinong chef at meal planner.
Ang hiling ng user: "{user_query}"
Ngayong araw ay {fmt_date(now)}, {now.year}.

MAHIGPIT NA PATAKARAN:
- Sagutin LAMANG ang eksaktong hiniling. Huwag magdagdag ng ibang oras ng kain, ibang araw, o alternatibong hindi hiniling.
- Ang mga putahe ay dapat bagay sa oras ng kain at sa panahong hiniling.
- Pilipinong putahe at sangkap na madaling mabili sa palengke. Sundin din ang anumang dagdag na hiling (budget, bilang ng kakain, sangkap na meron sila).
- Walang paulit-ulit na putahe sa iisang plano. Maging malikhain pero praktikal.
- Sa Filipino/Tagalog ang sagot. Simpleng teksto na may emoji sa mga pamagat; huwag gumamit ng table.
"""
    guide = f"\nGabay sa oras ng kain: {MEAL_GUIDE[meal]}\n" if meal else ""

    meals = intent['meals'] or list(MEAL_LABEL)
    meal_text = ", ".join(MEAL_LABEL[m] for m in meals)
    n = intent['count']
    when = "ngayon" if span == 'ngayon' else f"BUKAS ({fmt_date(tomorrow)})"
    no_extra = "HUWAG magdagdag ng alternatibo, ibang putahe, o ibang oras ng kain."

    if span in ('ngayon', 'bukas'):
        if len(intent['meals']) == 1:
            task = f"""
GAWIN: Magbigay ng EKSAKTONG {n} putahe para sa {MEAL_LABEL[meal].upper()} {when}.
Bawat putahe: Pangalan, Sangkap (may sukat), Hakbang-hakbang na pagluluto, tinatayang oras at budget.
{no_extra}"""
        elif len(intent['meals']) > 1:
            task = f"""
GAWIN: Para sa {when}, magbigay ng EKSAKTONG {n} putahe bawat isa sa mga kainang ito LAMANG: {meal_text}.
Bawat putahe: Pangalan, Sangkap (may sukat), Hakbang-hakbang."""
        else:
            task = f"""
GAWIN: Sagutin LAMANG ang tanong. Kung humihingi ng resipi o ideya, magbigay ng EKSAKTONG {n} putahe. {no_extra}"""
    elif span == 'linggo':
        day_list = "\n".join(f"- {d}" for d in days)
        task = f"""
GAWIN: Magbigay ng plano para sa {len(days)} ARAW, ayon sa petsa:
{day_list}
Kainan sa bawat araw (ito LAMANG): {meal_text}.
Bawat kainan: pangalan ng putahe + 1 maikling linya ng pangunahing sangkap.
Sa dulo: listahan ng ipamamalengke para sa linggo."""
    else:
        task = f"""
GAWIN: Gumawa ng 1-buwang plano, hatiin sa Linggo 1 hanggang Linggo 4.
Kainan sa bawat araw (ito LAMANG): {meal_text}. Pangalan ng putahe lang, walang paulit-ulit."""

    return rules + guide + task + health_prompt(user_query)


STOPWORDS = {'anong', 'ano', 'masarap', 'na', 'pang', 'para', 'sa', 'ng', 'ang', 'mga', 'ba', 'nga',
             'bukas', 'next', 'week', 'ngayon', 'naman', 'po', 'paki', 'pakibigyan', 'bigyan', 'ako',
             'umagahan', 'tanghalian', 'meryenda', 'hapunan', 'plan', 'meal', 'lutuin', 'ulam'}


def search_recipes(query, intent):
    hr = health_recipes(query)
    if len(hr):
        return records(hr)

    if intent['meals']:
        matches = df[meal_mask(intent['meals'])]
        if len(matches) > 0:
            return records(matches.sample(n=min(20, len(matches))))

    q = query.lower()
    matches = df[SEARCH_TEXT.str.contains(q, regex=False)]
    if len(matches) == 0:
        words = [w for w in re.findall(r'\w+', q) if len(w) >= 4 and w not in STOPWORDS]
        if words:
            matches = df[SEARCH_TEXT.str.contains('|'.join(re.escape(w) for w in words), regex=True)]
    if len(matches) == 0 and len(df):
        matches = df.sample(n=min(16, len(df)))
    return records(matches.head(20))


# ---------------------------------------------------------------------------
# AI (GEMINI)
# ---------------------------------------------------------------------------
MODELS_TO_TRY = [m for m in [os.getenv("GEMINI_MODEL"), 'gemini-2.5-flash',
                             'gemini-2.5-flash-lite', 'gemini-flash-latest'] if m]
working_model = None


def classify_error(e):
    s = str(e).lower()
    if any(k in s for k in ('api key', 'api_key', 'permission_denied', 'unauthenticated', '401', '403')):
        return 'bad_key'
    if any(k in s for k in ('429', 'resource_exhausted', 'quota', 'rate limit')):
        return 'quota'
    if any(k in s for k in ('404', 'not_found', 'not found', 'is not supported')):
        return 'model'
    if any(k in s for k in ('timeout', 'timed out', 'deadline')):
        return 'timeout'
    if any(k in s for k in ('connect', 'network', 'resolve', 'unreachable')):
        return 'network'
    return 'other'


def gemini_call(prompt, max_tokens):
    """Ibabalik ang (text, error_code). Sumusubok ng ilang model hanggang may sumagot."""
    global working_model
    if not client:
        return None, 'no_key'

    order = MODELS_TO_TRY[:]
    if working_model in order:
        order.remove(working_model)
        order.insert(0, working_model)

    started = time.time()
    last_code = 'other'
    for m in order:
        if time.time() - started > 40:
            break
        for use_thinking in ((True, False) if '2.5' in m else (False,)):
            cfg = {'temperature': 0.8, 'max_output_tokens': max_tokens}
            if use_thinking:
                cfg['thinking_config'] = types.ThinkingConfig(thinking_budget=0)
            try:
                resp = client.models.generate_content(model=m, contents=prompt, config=cfg)
                if resp and resp.text:
                    working_model = m
                    return resp.text, None
                last_code = 'other'
                break
            except Exception as e:
                print(f"[Gemini:{m}] {type(e).__name__}: {e}")
                if use_thinking and 'thinking' in str(e).lower():
                    continue  # subukan ulit ang parehong model nang walang thinking config
                last_code = classify_error(e)
                break
        if last_code in ('bad_key', 'quota'):
            break
    return None, last_code


_ai_status_cache = {'t': 0, 'data': None}


def local_fallback(user_query, intent, recipes):
    """Kapag hindi maabot ang AI: sagot mula sa sariling cookbook, angkop pa rin sa hiniling."""
    htxt = health_text(user_query)
    if htxt:
        extra = f"\n\nNarito ang {len(recipes)} ulam sa cookbook na bagay." if recipes else ""
        return htxt + extra

    span, n = intent['span'], intent['count']
    meals = intent['meals'] or (list(MEAL_LABEL) if span in ('linggo', 'buwan') else [])
    lines = ["🍳 Hindi ko maabot ang AI ngayon, pero narito ang mungkahi mula sa cookbook:", ""]

    def pool_for(meal, k):
        sub = df[meal_mask([meal])]
        sub = sub if len(sub) else df
        return records(sub.sample(n=min(k, len(sub))))

    if span in ('linggo', 'buwan') and meals:
        days = plan_days(intent) if span == 'linggo' else [f"Araw {i + 1}" for i in range(28)]
        pools = {m: pool_for(m, len(days)) for m in meals}
        for i, d in enumerate(days):
            lines.append(f"📅 {d}")
            for m in meals:
                p = pools[m]
                if p:
                    lines.append(f"   {MEAL_LABEL[m]}: {p[i % len(p)]['title']}")
            lines.append("")
    elif meals:
        for m in meals:
            lines.append(f"🍽️ {MEAL_LABEL[m]}")
            for r in pool_for(m, n):
                lines.append(f"• {r['title']}\nSangkap: {r['ingredients']}\nParaan:\n{r['instructions']}\n")
    else:
        for r in recipes[:max(1, n)]:
            lines.append(f"🍽️ {r['title']}\nSangkap: {r['ingredients']}\nParaan:\n{r['instructions']}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AUTH HELPERS
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_attempts = {}


def client_ip():
    return (request.headers.get('X-Forwarded-For', request.remote_addr or '') or '').split(',')[0].strip()


def throttled(key, limit, window):
    now = time.time()
    lst = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = lst
    return len(lst) >= limit


def note_attempt(key):
    _attempts.setdefault(key, []).append(time.time())


def current_user():
    uid = session.get('uid')
    return db.session.get(User, uid) if uid else None


def user_json(u):
    return {'id': u.id, 'name': u.name, 'email': u.email}


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({'error': 'Mag-login muna.'}), 401
        return fn(u, *a, **kw)
    return wrapper


def err(msg, code=400):
    return jsonify({'error': msg}), code


def json_body():
    if not request.is_json:
        return None
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# ROUTES: PAGE
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    boot = {'featured': ALL_RECORDS[:24], 'total': len(ALL_RECORDS), 'categories': CATEGORIES}
    return render_template('index.html', boot=boot)


@app.get('/healthz')
def healthz():
    return jsonify({'ok': True, 'recipes': len(ALL_RECORDS)})


# ---------------------------------------------------------------------------
# ROUTES: RECIPES
# ---------------------------------------------------------------------------
@app.get('/api/recipes')
def api_recipes():
    q = request.args.get('q', '').strip().lower()
    cat = request.args.get('category', '').strip()
    slot = request.args.get('slot', '').strip()

    def num(name, default, lo, hi):
        try:
            return min(max(int(request.args.get(name, default)), lo), hi)
        except ValueError:
            return default

    offset, limit = num('offset', 0, 0, 100000), num('limit', 24, 1, 60)

    mask = pd.Series(True, index=df.index)
    if q:
        mask &= SEARCH_TEXT.str.contains(q, regex=False)
    if cat:
        mask &= (df['category'] == cat)
    if slot in SLOT_TO_MEAL:
        mask &= meal_mask([SLOT_TO_MEAL[slot]])
    if request.args.get('lean') == '1':
        mask &= df['lean']

    sub = df[mask]
    return jsonify({'recipes': records(sub.iloc[offset:offset + limit]), 'total': int(len(sub))})


# ---------------------------------------------------------------------------
# ROUTES: AI
# ---------------------------------------------------------------------------
@app.post('/ask_ai')
def ask_ai():
    data = request.get_json(silent=True) or {}
    user_query = str(data.get('query', '')).strip()[:400]

    if not user_query:
        return jsonify({'reply': 'Magtanong ka ng tungkol sa pagluluto o humingi ng meal plan!',
                        'recipes': [], 'ai_ok': True, 'ai_error': None})

    key = 'ai:' + client_ip()
    if throttled(key, 25, 600):
        return jsonify({'reply': 'Sandali lang po, masyadong madalas ang tanong. Subukan ulit pagkalipas ng ilang minuto.',
                        'recipes': [], 'ai_ok': True, 'ai_error': None}), 429
    note_attempt(key)

    intent = detect_intent(user_query)
    recipes = search_recipes(user_query, intent)

    big = intent['span'] in ('linggo', 'buwan')
    max_tokens = 8192 if intent['span'] == 'buwan' else (4096 if big else 2048)
    text, code = gemini_call(build_prompt(user_query, intent), max_tokens)

    if text is None:
        text = local_fallback(user_query, intent, recipes)

    return jsonify({'reply': text, 'recipes': recipes, 'ai_ok': code is None, 'ai_error': code})


@app.get('/api/ai_status')
def ai_status():
    """Para malaman kung bakit hindi gumagana ang AI. Walang sikreto na ibinabalik."""
    now = time.time()
    if _ai_status_cache['data'] and now - _ai_status_cache['t'] < 60:
        return jsonify(_ai_status_cache['data'])
    text, code = gemini_call("Sumagot ng isang salita lang: OK", 20)
    data = {'key_configured': bool(api_key), 'ok': code is None, 'error': code,
            'model': working_model, 'tried_models': MODELS_TO_TRY}
    _ai_status_cache.update(t=now, data=data)
    return jsonify(data)


# ---------------------------------------------------------------------------
# ROUTES: LOGIN / SIGN UP
# ---------------------------------------------------------------------------
@app.get('/api/me')
def api_me():
    u = current_user()
    if not u:
        return jsonify({'user': None, 'favorites': []})
    favs = [f.recipe_id for f in Favorite.query.filter_by(user_id=u.id).all()]
    return jsonify({'user': user_json(u), 'favorites': favs})


@app.post('/api/signup')
def api_signup():
    d = json_body()
    if d is None:
        return err('Maling request.')
    name = str(d.get('name', '')).strip()
    email = str(d.get('email', '')).strip().lower()
    pw = str(d.get('password', ''))

    if not (1 <= len(name) <= 60):
        return err('Ilagay ang iyong pangalan (hanggang 60 letra).')
    if not EMAIL_RE.match(email) or len(email) > 255:
        return err('Hindi tama ang email.')
    if len(pw) < 8:
        return err('Dapat hindi bababa sa 8 letra ang password.')
    if len(pw) > 128:
        return err('Masyadong mahaba ang password.')

    key = 'signup:' + client_ip()
    if throttled(key, 10, 3600):
        return err('Masyadong maraming pagsubok. Subukan ulit mamaya.', 429)
    note_attempt(key)

    if User.query.filter_by(email=email).first():
        return err('May account na gamit ang email na iyan. Mag-login na lang.', 409)

    u = User(name=name, email=email, password_hash=generate_password_hash(pw))
    db.session.add(u)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return err('May account na gamit ang email na iyan. Mag-login na lang.', 409)

    session.clear()
    session['uid'] = u.id
    session.permanent = True
    return jsonify({'user': user_json(u), 'favorites': []}), 201


@app.post('/api/login')
def api_login():
    d = json_body()
    if d is None:
        return err('Maling request.')
    email = str(d.get('email', '')).strip().lower()
    pw = str(d.get('password', ''))

    key = f'login:{client_ip()}:{email}'
    if throttled(key, 8, 600):
        return err('Masyadong maraming maling pagsubok. Maghintay ng 10 minuto.', 429)

    u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.password_hash, pw):
        note_attempt(key)
        return err('Mali ang email o password.', 401)

    session.clear()
    session['uid'] = u.id
    session.permanent = True
    favs = [f.recipe_id for f in Favorite.query.filter_by(user_id=u.id).all()]
    return jsonify({'user': user_json(u), 'favorites': favs})


@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# ROUTES: PABORITO AT MEAL PLAN (naka-save sa account)
# ---------------------------------------------------------------------------
@app.get('/api/favorites')
@login_required
def api_favorites(u):
    ids = [f.recipe_id for f in Favorite.query.filter_by(user_id=u.id).order_by(Favorite.id.desc()).all()]
    return jsonify({'recipes': [BY_ID[i] for i in ids if i in BY_ID]})


@app.post('/api/favorites/<int:rid>')
@login_required
def api_toggle_favorite(u, rid):
    if rid not in BY_ID:
        return err('Walang ganyang resipi.', 404)
    fav = Favorite.query.filter_by(user_id=u.id, recipe_id=rid).first()
    if fav:
        db.session.delete(fav)
        state = False
    else:
        db.session.add(Favorite(user_id=u.id, recipe_id=rid))
        state = True
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return jsonify({'favorited': state})


def plan_with_recipes(days):
    return {str(d): {s: BY_ID.get(slots.get(s)) for s in SLOTS} for d, slots in days.items()}


@app.get('/api/plan')
@login_required
def api_get_plan(u):
    if not u.plan_json:
        return jsonify({'plan': None, 'slim': False})
    try:
        saved = json.loads(u.plan_json)
        return jsonify({'plan': plan_with_recipes(saved['days']), 'slim': bool(saved.get('slim'))})
    except Exception:
        return jsonify({'plan': None, 'slim': False})


@app.post('/api/plan')
@login_required
def api_save_plan(u):
    d = json_body()
    if d is None or not isinstance(d.get('days'), dict):
        return err('Maling meal plan.')
    clean = {}
    for day in map(str, range(1, 8)):
        slots = d['days'].get(day) or {}
        clean[day] = {}
        for s in SLOTS:
            rid = slots.get(s)
            clean[day][s] = rid if isinstance(rid, int) and rid in BY_ID else None
    u.plan_json = json.dumps({'days': clean, 'slim': bool(d.get('slim'))})
    db.session.commit()
    return jsonify({'ok': True})


@app.get('/api/plan/suggest')
def api_plan_suggest():
    """Gumagawa ng 7-araw na plano. Bawat kainan ay kukuha sa tamang uri ng pagkain."""
    slim = request.args.get('mode') == 'slim'
    picks = {}
    for slot in SLOTS:
        pool = df[meal_mask([SLOT_TO_MEAL[slot]])]
        if len(pool) == 0:
            pool = df
        if slim:
            lean = pool[pool['lean']]
            pool = lean if len(lean) >= 7 else pool.nsmallest(min(len(pool), 12), 'kcal')
        picks[slot] = pool.sample(frac=1)['id'].tolist()

    plan = {}
    for day in range(1, 8):
        row, used = {}, set()
        for slot in SLOTS:
            ids = picks[slot]
            if not ids:
                row[slot] = None
                continue
            choice = ids[(day - 1) % len(ids)]
            for step in range(len(ids)):
                cand = ids[((day - 1) + step) % len(ids)]
                if cand not in used:
                    choice = cand
                    break
            used.add(choice)
            row[slot] = BY_ID[choice]
        plan[str(day)] = row
    return jsonify({'plan': plan, 'slim': slim})


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG') == '1', port=int(os.getenv('PORT', 5000)), threaded=True)