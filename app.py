"""
KusinaAI Digital Cookbook & Calendar: Main Application Entry
"""
import os
import re
import ssl
import time
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.parse
import urllib.error

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from google import genai

from auth import auth_bp, init_auth, login_required, current_user

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# override=True: ang .env ang masusunod, kahit may lumang GEMINI_API_KEY sa system environment
load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
_secret = os.getenv("SECRET_KEY") or secrets.token_hex(32)

app.config.update(
    SECRET_KEY=_secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

_db_url = os.getenv("DATABASE_URL") or "sqlite:///" + os.path.join(BASE_DIR, "kusina.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# SSL: gamitin ang certifi (updated na certificates) para hindi bumagsak sa
# "CERTIFICATE_VERIFY_FAILED" ang mga online request.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# ---------------------------------------------------------------------------
# DATABASE MODELS
# ---------------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    plan_json = db.Column(db.Text, default="")


class Favorite(db.Model):
    __tablename__ = "favorites"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipe_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "recipe_id", name="uq_user_recipe"),)


init_auth(db, User)
app.register_blueprint(auth_bp)

with app.app_context():
    db.create_all()

# ---------------------------------------------------------------------------
# DATASET LOADING & IMAGE MAPPING
# ---------------------------------------------------------------------------
COLUMNS = ['id', 'title', 'category', 'ingredients', 'instructions', 'budget_level', 'best_for', 'image_url']

try:
    _csv = os.path.join(BASE_DIR, 'recipes.csv')
    df = pd.read_csv(_csv) if os.path.exists(_csv) else pd.DataFrame(columns=COLUMNS)
    # kung walang header ang CSV, gamitin ang tamang pangalan ng column
    if len(df) and 'title' not in df.columns:
        df = pd.read_csv(_csv, header=None, names=COLUMNS)
except Exception:
    df = pd.DataFrame(columns=COLUMNS)
print("Recipes loaded:", len(df))

for _c in COLUMNS:
    if _c not in df.columns:
        df[_c] = ''
df = df.fillna('').reset_index(drop=True)
_ids = pd.to_numeric(df['id'], errors='coerce')
df['id'] = _ids.where(_ids.notna(), pd.Series(range(1, len(df) + 1))).astype(int)

# Manual na larawan (opsyonal). Ilagay ang sarili mong litrato sa static/images/
# tapos i-map dito, halimbawa:
#   "Adobong Manok": "/static/images/adobo.jpg",
MANUAL_IMAGES = {
}

def local_image(title, current=''):
    """Priority: manual map > lokal na larawan sa CSV (/static/...). Kung wala, blangko
    (kukunin ng Wikipedia lookup, o placeholder kung wala talaga)."""
    t = str(title).lower()
    for key, url in MANUAL_IMAGES.items():
        if key.lower() in t:
            return url
    cur = str(current).strip()
    if cur.startswith('/static/') or cur.startswith('static/'):
        rel = '/' + cur.lstrip('/')
        # gamitin lang kung talagang nandoon ang file; kung wala, Wikipedia lookup ang gagana
        if os.path.isfile(os.path.join(BASE_DIR, urllib.parse.unquote(rel).lstrip('/'))):
            return rel
    return ''

df['image_url'] = [local_image(t, c) for t, c in zip(df['title'], df['image_url'])]

# ---------------------------------------------------------------------------
# DISH IMAGE LOOKUP (Wikipedia, naka-cache)
# ---------------------------------------------------------------------------
IMG_CACHE_FILE = os.path.join(BASE_DIR, 'image_cache.json')
_img_lock = threading.Lock()
try:
    with open(IMG_CACHE_FILE, encoding='utf-8') as f:
        IMG_CACHE = json.load(f)
except Exception:
    IMG_CACHE = {}

_STOP = {'na', 'ng', 'may', 'at', 'sa', 'with', 'and', 'the', 'ang', 'mga', 'ni'}

def _tokens(s):
    return [w for w in re.findall(r'[a-zñ]+', str(s).lower()) if len(w) >= 4 and w not in _STOP]

def _relevant(dish, page_title):
    """Tama lang ang larawan kung may pagkakahawig ang pangalan ng ulam at ng Wikipedia page
    (hal. 'Adobong Manok' ~ 'Adobo', 'Tinolang Manok' ~ 'Tinola')."""
    for a in _tokens(dish):
        for b in _tokens(page_title):
            if a.startswith(b) or b.startswith(a):
                return True
    return False

WIKI_UA = 'KusinaAI/1.0 (https://kusina-2.onrender.com; digital cookbook project) python-urllib'
_wiki_lock = threading.Lock()
_wiki_last = [0.0]

def _wiki_fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': WIKI_UA, 'Accept': 'application/json'})
    for attempt in range(2):
        try:
            try:
                with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as r:
                    return json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError:
                raise
            except (ssl.SSLError, urllib.error.URLError) as e:
                if 'CERTIFICATE_VERIFY_FAILED' not in str(e):
                    raise
                # Sira ang certificate store ng makina mo (hal. antivirus o lumang Windows certs).
                # Public na data lang ito mula sa Wikipedia kaya dito lang ito pinapayagan.
                with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as r:
                    return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:  # sandali lang, tapos ulit
                try:
                    wait = min(float(e.headers.get('Retry-After', 2)), 4)
                except Exception:
                    wait = 2
                time.sleep(wait)
                continue
            raise

def _wiki_get(url):
    # Isa-isa lang ang request at may pagitan, para hindi ma-"429 Too Many Requests"
    with _wiki_lock:
        wait = 0.35 - (time.time() - _wiki_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return _wiki_fetch(url)
        finally:
            _wiki_last[0] = time.time()

def wiki_image(title):
    key = title.strip().lower()
    if key in IMG_CACHE:
        return IMG_CACHE[key]

    toks = _tokens(title)
    queries = [f"{title} Filipino dish", title]
    if toks:
        queries.append(f"{toks[0]} Philippines food")

    url, had_error = '', False
    for q in queries:
        try:
            params = urllib.parse.urlencode({
                'action': 'query', 'format': 'json', 'generator': 'search',
                'gsrsearch': q, 'gsrlimit': 5, 'prop': 'pageimages',
                'piprop': 'thumbnail', 'pithumbsize': 500,
            })
            data = _wiki_get("https://en.wikipedia.org/w/api.php?" + params)
            pages = sorted(data.get('query', {}).get('pages', {}).values(),
                           key=lambda p: p.get('index', 99))
            for p in pages:
                if p.get('thumbnail') and _relevant(title, p.get('title', '')):
                    url = p['thumbnail']['source']
                    break
        except Exception as e:
            had_error = True
            print("Image lookup error:", e)
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                break  # huwag nang ituloy ang ibang query kapag pinipigilan tayo
        if url:
            break

    if url or not had_error:  # huwag i-cache ang pagkabigo dahil sa network
        with _img_lock:
            IMG_CACHE[key] = url
            try:
                with open(IMG_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(IMG_CACHE, f, ensure_ascii=False)
            except Exception:
                pass
    return url

DISH_KCAL = [
    ('adobong kangkong', 150), ('adobong sitaw', 210), ('adobong pusit', 230),
    ('ensaladang', 90), ('tortang talong', 250), ('laing', 300), ('ginataang langka', 280),
    ('sinigang na hipon', 230), ('sinigang na isda', 240), ('sinigang sa miso', 240),
    ('paksiw na isda', 220), ('inihaw na bangus', 240), ('inihaw na pusit', 180), ('kilawin', 150),
    ('taho', 150), ('mais con yelo', 200), ('buko pandan', 230),
    ('lechon kawali', 650), ('crispy pata', 720), ('bagnet', 680), ('sisig', 480),
    ('bicol express', 520), ('kare', 560),
    ('adobo', 430), ('sinigang', 280), ('tinola', 250), ('nilaga', 350), ('pinakbet', 180),
]
# Pang-tantiya kung walang tiyak na ulam sa itaas (batay sa kategorya)
CATEGORY_KCAL = {
    'Gulay': 160, 'Sabaw': 260, 'Pansit': 380, 'Meryenda': 300, 'Almusal': 400,
    'Pritong Ulam': 450, 'Ulam': 400, 'Panghimagas': 320,
}

def estimate_kcal(title, ingredients='', category=''):
    t = str(title).lower()
    for key, val in DISH_KCAL:
        if key in t:
            return val
    return CATEGORY_KCAL.get(str(category).strip(), 300)

df['kcal'] = [estimate_kcal(t, i, c) for t, i, c in zip(df['title'], df['ingredients'], df['category'])]
PUBLIC_COLS = ['id', 'title', 'category', 'ingredients', 'instructions', 'budget_level', 'best_for', 'image_url', 'kcal']
ALL_RECORDS = json.loads(df[PUBLIC_COLS].to_json(orient='records', force_ascii=False))
BY_ID = {r['id']: r for r in ALL_RECORDS}
CATEGORIES = sorted({str(c) for c in df['category'] if str(c)})

def records(sub):
    return [BY_ID[i] for i in sub['id'].tolist() if i in BY_ID]

# ---------------------------------------------------------------------------
# GEMINI API (REST, x-goog-api-key lang; sumusuporta sa bagong "AQ." na key)
# ---------------------------------------------------------------------------
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Gagamitin lang kung hindi makuha ang listahan ng model mula sa Google
FALLBACK_MODELS = ['gemini-3.5-flash', 'gemini-3-flash-preview', 'gemini-3.1-flash-lite', 'gemini-flash-latest']
_model_cache = {'models': None, 'at': 0.0}
LAST_AI_ERROR = {'status': None, 'message': ''}

def get_api_key():
    k = os.getenv("GEMINI_API_KEY", "")
    return k.strip().strip('"').strip("'").strip()

def _gemini_request(path, key, payload=None, timeout=40):
    headers = {'x-goog-api-key': key}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(GEMINI_BASE + path, data=data, headers=headers,
                                 method='POST' if payload is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode('utf-8'))

def _set_ai_error(e):
    status, msg = None, str(e)
    if isinstance(e, urllib.error.HTTPError):
        status = e.code
        try:
            body = json.loads(e.read().decode('utf-8'))
            msg = body.get('error', {}).get('message', msg)
        except Exception:
            pass
    LAST_AI_ERROR.update(status=status, message=str(msg)[:300])
    print("Gemini error:", status, msg)

_MODEL_RE = re.compile(r'^gemini-(\d+(?:\.\d+)?)-flash(-lite)?(-preview[\w-]*)?$')

def list_gen_models(key):
    """Kunin mula sa Google ang mga text model na gumagana sa key mo (pinakabago muna)."""
    now = time.time()
    if _model_cache['models'] and now - _model_cache['at'] < 3600:
        return _model_cache['models']
    found = []
    try:
        data = _gemini_request('/models?pageSize=200', key, timeout=15)
        for m in data.get('models', []):
            name = str(m.get('name', '')).replace('models/', '')
            if 'generateContent' not in m.get('supportedGenerationMethods', []):
                continue
            mt = _MODEL_RE.match(name)
            if mt:
                found.append((-float(mt.group(1)), bool(mt.group(2)), bool(mt.group(3)), len(name), name))
    except Exception as e:
        _set_ai_error(e)
        return []
    found.sort()
    names = [f[-1] for f in found][:4]
    if names:
        _model_cache.update(models=names, at=now)
    return names

def gemini_text(prompt, key):
    """Ibabalik ang sagot ng Gemini, o None kung bumagsak (nasa LAST_AI_ERROR ang dahilan)."""
    LAST_AI_ERROR.update(status=None, message='')
    deadline = time.time() + 55  # huwag lumampas sa ~1 minuto kahit maraming model
    candidates = list(list_gen_models(key))[:3]
    for fm in FALLBACK_MODELS[:2]:
        if fm not in candidates:
            candidates.append(fm)

    if LAST_AI_ERROR['status'] in (401, 403):  # mali ang key, walang saysay ang ibang model
        return None

    for model in candidates:
        remaining = deadline - time.time()
        if remaining < 5:
            break
        try:
            data = _gemini_request(f"/models/{model}:generateContent", key,
                                   {"contents": [{"parts": [{"text": prompt}]}]},
                                   timeout=min(40, remaining))
            parts = data['candidates'][0]['content']['parts']
            text = ''.join(p.get('text', '') for p in parts).strip()
            if text:
                LAST_AI_ERROR.update(status=None, message='')
                return text
        except urllib.error.HTTPError as e:
            _set_ai_error(e)
            if e.code in (401, 403):
                return None
        except Exception as e:
            _set_ai_error(e)

    # Huling subok: opisyal na SDK (huwag na kung naubos na ang oras o timeout ang problema)
    if time.time() >= deadline or 'timed out' in LAST_AI_ERROR['message']:
        return None
    try:
        ai_client = genai.Client(api_key=key)
        for model in candidates[:3]:
            try:
                resp = ai_client.models.generate_content(model=model, contents=prompt)
                if resp and resp.text:
                    LAST_AI_ERROR.update(status=None, message='')
                    return resp.text
            except Exception as e:
                print("SDK error:", e)
    except Exception as e:
        print("SDK Error:", e)
    return None

def ai_error_message(lang='tl'):
    s, m = LAST_AI_ERROR['status'], LAST_AI_ERROR['message']
    if s is None and 'timed out' in m:
        return ("Gemini took too long to respond. Please try again." if lang == 'en'
                else "Natagalan ang sagot ng Gemini. Subukan ulit.")
    if lang == 'en':
        if s in (401, 403):
            return "Gemini rejected the API key (error %s). Create a new key in Google AI Studio, put it in .env as GEMINI_API_KEY, then restart the server." % s
        if s == 429:
            return "The Gemini quota is used up for now (error 429). Try again later or check the quota/billing in AI Studio."
        if s == 404:
            return "No working Gemini model was found (error 404). Please check /api/ai_status."
        return "Could not reach Gemini right now. %s" % m
    if s in (401, 403):
        return "Hindi tinanggap ng Gemini ang API key (error %s). Gumawa ng bagong key sa Google AI Studio, ilagay sa .env bilang GEMINI_API_KEY, tapos i-restart ang server." % s
    if s == 429:
        return "Naubos na ang quota ng Gemini sa ngayon (error 429). Subukan ulit mamaya o i-check ang quota/billing sa AI Studio."
    if s == 404:
        return "Walang nahanap na gumaganang Gemini model (error 404). Buksan ang /api/ai_status para makita ang detalye."
    return "Hindi maabot ang Gemini ngayon. %s" % m

# ---------------------------------------------------------------------------
# AI HELPERS (weekly meal plan)
# ---------------------------------------------------------------------------
DAYS = ['Lunes', 'Martes', 'Miyerkules', 'Huwebes', 'Biyernes', 'Sabado', 'Linggo']
MEALS = ['Almusal', 'Tanghalian', 'Hapunan']

_WEEK_RE = re.compile(
    r'(is+ang|1|one|\ba|buong|whole)\s*-?\s*(linggo|lingo|week)|weekly|linggu-?linggo|'
    r'7\s*(days|araw)|pitong\s+araw|meal\s*plan', re.I)
_FOOD_RE = re.compile(
    r'ulam|menu|meal|plan|pagkain|kainin|lutuin|ihanda|kalendaryo|calendar|almusal|hapunan|tanghalian', re.I)
# Kapag binanggit ang kalendaryo (kasama ang maling spelling), ilalagay ng AI sa kalendaryo
_CAL_RE = re.compile(r'kalend|calend|kalind|calind', re.I)

def wants_week_plan(q):
    return bool(_WEEK_RE.search(q) and _FOOD_RE.search(q))

def wants_calendar(q):
    return bool(_CAL_RE.search(q))

def extract_json(text):
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def clean_plan(obj):
    raw = obj.get('plan', obj) if isinstance(obj, dict) else {}
    plan = {}
    if not isinstance(raw, dict):
        return plan
    for d in DAYS:
        day = raw.get(d)
        if not isinstance(day, dict):
            continue
        for m in MEALS:
            v = day.get(m)
            if isinstance(v, str) and v.strip():
                plan.setdefault(d, {})[m] = v.strip()[:80]
    return plan

def build_plan_prompt(q, lang='tl', history=''):
    titles = ', '.join(df['title'].astype(str).tolist())
    today = DAYS[datetime.now().weekday()]
    return (
        "Ikaw ang KusinaAI chef sa Pilipinas. Ilalagay sa kalendaryo ng user ang ulam ayon sa hiling niya.\n"
        + (f"Naunang usapan:\n{history}\n\n" if history else "")
        + f"Hiling ngayon: {q}\n\n"
        f"Ngayon ay {today}. Kung isang ulam o ilang slot lang ang hiniling, ang mga slot na iyon LANG ang isama. "
        "Kung walang binanggit na araw, ilagay sa araw ngayon; kung walang binanggit na oras, piliin ang babagay na oras. "
        "Kung buong linggo ang hiniling, punuin ang lahat ng araw at oras gamit ang iba-ibang ulam. "
        "Kung may ulam sa naunang usapan na tinutukoy ng user (hal. 'ilagay mo yan'), iyon ang ilagay. "
        "Gumamit ng mga ulam na pang-Pilipino. Hangga't maaari, gamitin nang eksakto ang pangalan mula sa "
        f"listahang ito ng cookbook (puwede ring ibang kilalang ulam kung kulang): {titles}\n\n"
        'Ibalik LAMANG ang isang JSON object, walang ibang teksto at walang code fence, sa ganitong anyo: '
        '{"buod": "isang maikling pangungusap sa Filipino", "plan": {"Lunes": {"Almusal": "...", '
        '"Tanghalian": "...", "Hapunan": "..."}, "Martes": {...}}}. '
        "Gamitin nang eksakto ang mga susi na Lunes, Martes, Miyerkules, Huwebes, Biyernes, Sabado, Linggo "
        "at Almusal, Tanghalian, Hapunan. Pangalan lang ng ulam ang laman ng bawat slot."
        + (" Isulat sa English ang 'buod' lang; panatilihin ang pangalan ng ulam." if lang == 'en' else "")
    )

# ---------------------------------------------------------------------------
# MAIN ROUTES, LOGOUT & AI CHAT
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    u = current_user()
    if not u:
        return render_template('login.html')

    boot = {'featured': ALL_RECORDS, 'total': len(ALL_RECORDS), 'categories': CATEGORIES}
    return render_template('index.html', boot=boot, featured_recipes=ALL_RECORDS, total=len(ALL_RECORDS), categories=CATEGORIES,
                           user={'name': u.name, 'email': u.email})

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/account')
def account():
    """Account page: profile, about, user manual, at logout button."""
    u = current_user()
    if not u:
        return render_template('login.html')
    return render_template('account.html', user={
        'name': u.name,
        'email': u.email,
        'joined': u.created_at.isoformat() if u.created_at else '',
    })

@app.route('/talaan')
def talaan():
    """Bagong page: Talaan ng lahat ng ulam bawat linggo at buwan."""
    if not current_user():
        return render_template('login.html')
    return render_template('talaan.html')

@app.get('/api/recipes')
def api_recipes():
    q = request.args.get('q', '').strip().lower()
    cat = request.args.get('category', '').strip()
    sub = df
    if q:
        sub = sub[sub['title'].str.lower().str.contains(q, na=False) | sub['ingredients'].str.lower().str.contains(q, na=False)]
    if cat:
        sub = sub[sub['category'] == cat]
    flt = request.args.get('filter', '').strip()
    if flt == 'healthy':  # pang-health: gulay o may markang malusog / masustansya
        sub = sub[(sub['category'] == 'Gulay') |
                  sub['best_for'].str.lower().str.contains('malusog|pampalusog|masustansya', na=False)]
    elif flt == 'lowcal':  # low calories: 250 kcal pababa
        sub = sub[sub['kcal'] <= 250]
    return jsonify({'recipes': records(sub), 'total': int(len(sub))})

@app.get('/api/titles')
def api_titles():
    """Lahat ng pangalan ng ulam sa cookbook (para sa dropdown ng kalendaryo)."""
    return jsonify({'titles': [r['title'] for r in ALL_RECORDS]})

@app.get('/api/recipe_lookup')
def api_recipe_lookup():
    """Hanapin ang resipe mula sa pangalan ng ulam na nasa kalendaryo."""
    q = request.args.get('title', '').strip().lower()[:120]
    if not q:
        return jsonify({'recipe': None})
    best, score = None, 0
    qt = set(_tokens(q))
    for r in ALL_RECORDS:
        tl = r['title'].lower()
        if tl == q or tl in q or q in tl:
            return jsonify({'recipe': r})
        s = len(qt & set(_tokens(tl)))
        if s > score:
            best, score = r, s
    if best and score >= min(2, len(qt)):
        return jsonify({'recipe': best})
    return jsonify({'recipe': None})

@app.get('/api/dish_image')
def api_dish_image():
    t = request.args.get('title', '').strip()[:120]
    return jsonify({'url': wiki_image(t) if t else ''})

@app.get('/api/ai_status')
def api_ai_status():
    """Diagnostic: buksan sa browser (kailangang naka-login) para makita kung gumagana ang Gemini key."""
    if not current_user():
        return jsonify({'ok': False, 'error': 'Mag-login muna.'}), 401
    key = get_api_key()
    info = {'key_loaded': bool(key), 'key_starts_with': key[:3], 'key_length': len(key)}
    if key:
        _model_cache.update(models=None, at=0.0)
        info['models_found'] = list_gen_models(key)
        info['test_ok'] = bool(gemini_text("Sabihin mo lang: OK", key))
        info['last_error'] = dict(LAST_AI_ERROR)
    return jsonify(info)

@app.post('/ask_ai')
def ask_ai():
    data = request.get_json(silent=True) or {}
    q = str(data.get('query', '')).strip()[:400]
    lang = 'en' if data.get('lang') == 'en' else 'tl'
    if not q:
        return jsonify({'reply': 'Ask me something about cooking!' if lang == 'en'
                        else 'Magtanong ka ng tungkol sa pagluluto!'})

    current_key = get_api_key()
    if not current_key:
        return jsonify({'reply': 'No GEMINI_API_KEY found in the .env file.' if lang == 'en'
                        else 'Walang nahanap na GEMINI_API_KEY sa .env file.'})

    # Naunang usapan, para maintindihan ng AI ang "ilagay mo yan sa kalendaryo"
    history = ''
    raw_hist = data.get('history')
    if isinstance(raw_hist, list):
        for h in raw_hist[-6:]:
            if isinstance(h, dict):
                who = 'User' if h.get('role') == 'user' else 'Chef'
                history += f"{who}: {str(h.get('text', ''))[:600]}\n"

    # Kapag humingi ng ulam para sa kalendaryo (isa, ilan, o buong linggo): gumawa ng plano at
    # ibalik para awtomatikong mailagay sa kalendaryo.
    is_week = wants_week_plan(q)
    if is_week or wants_calendar(q):
        obj = extract_json(gemini_text(build_plan_prompt(q, lang, history), current_key))
        plan = clean_plan(obj) if obj else {}
        if sum(len(v) for v in plan.values()) >= (7 if is_week else 1):
            lines = []
            buod = str(obj.get('buod', '')).strip() if isinstance(obj, dict) else ''
            if buod:
                lines.append(buod)
            for d in DAYS:
                if d in plan:
                    parts = ' • '.join(f"{m}: {plan[d][m]}" for m in MEALS if m in plan[d])
                    lines.append(f"**{d}** — {parts}")
            lines.append("I've added this to your Calendar." if lang == 'en'
                         else "Nailagay ko na ito sa Kalendaryo mo.")
            return jsonify({'reply': '\n'.join(lines), 'plan': plan})
        if LAST_AI_ERROR['status'] in (401, 403):
            return jsonify({'reply': ai_error_message(lang)})

    if lang == 'en':
        prompt = f"You are KusinaAI, a Filipino chef. Answer clearly, in detail, and completely in English: {q}"
    else:
        prompt = f"Ikaw ang KusinaAI chef sa Pilipinas. Sagutin sa Filipino nang maayos, detalyado, at kumpleto ang tanong na ito: {q}"
    if history:
        prompt = f"Naunang usapan:\n{history}\n{prompt}"
    text = gemini_text(prompt, current_key)
    if text:
        return jsonify({'reply': text})

    return jsonify({'reply': ai_error_message(lang)})

@app.post('/api/translate')
def api_translate():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text', '')).strip()[:1000]
    direction = data.get('direction', 'en-tl')
    if not text:
        return jsonify({'ok': False, 'error': 'Walang tekstong isasalin.'})

    key = get_api_key()
    if not key:
        return jsonify({'ok': False, 'error': 'Walang nahanap na GEMINI_API_KEY sa .env file.'})

    if direction == 'tl-en':
        src, dst = 'Tagalog (Filipino)', 'English'
    else:
        src, dst = 'English', 'Tagalog (Filipino)'

    prompt = (
        f"Isalin ang sumusunod na teksto mula {src} patungong {dst}. "
        "Gawing natural at tama ang salin. "
        "Ibalik LAMANG ang salin, walang paliwanag, panipi, o dagdag na komento.\n\n"
        f"Teksto:\n{text}"
    )
    out = gemini_text(prompt, key)
    if not out:
        return jsonify({'ok': False, 'error': ai_error_message('tl')})
    return jsonify({'ok': True, 'translation': out.strip()})

@app.post('/api/translate_batch')
def api_translate_batch():
    """Isalin ang listahan ng teksto (pamagat ng resipe, sangkap, atbp.) sa 'en' o 'tl'."""
    data = request.get_json(silent=True) or {}
    texts = data.get('texts', [])
    target = 'en' if data.get('target') == 'en' else 'tl'
    if not isinstance(texts, list) or not texts:
        return jsonify({'ok': False, 'error': 'Walang tekstong isasalin.'})
    texts = [str(x)[:3000] for x in texts[:50]]

    key = get_api_key()
    if not key:
        return jsonify({'ok': False, 'error': 'Walang nahanap na GEMINI_API_KEY sa .env file.'})

    dst = 'English' if target == 'en' else 'Tagalog (Filipino)'
    prompt = (
        f"Isalin sa {dst} ang bawat teksto sa JSON array na ito. Panatilihin ang parehong bilang at pagkakasunod-sunod. "
        f"Kung nasa {dst} na ang teksto, ibalik ito nang hindi binabago. Panatilihin ang mga bagong linya. "
        "Ibalik LAMANG ang isang JSON array ng mga string, walang ibang teksto o code fence.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    out = gemini_text(prompt, key)
    m = re.search(r'\[.*\]', out or '', re.S)
    try:
        arr = json.loads(m.group(0)) if m else None
    except Exception:
        arr = None
    if not isinstance(arr, list) or len(arr) != len(texts):
        return jsonify({'ok': False, 'error': ai_error_message('tl')})
    return jsonify({'ok': True, 'translations': [str(x) for x in arr]})

# ---------------------------------------------------------------------------
# MEAL CALENDAR & FAVORITES
# ---------------------------------------------------------------------------
@app.get('/api/favorites')
@login_required
def api_get_favorites(u):
    favs = db.session.execute(db.select(Favorite).filter_by(user_id=u.id)).scalars().all()
    return jsonify({'favorites': [BY_ID[f.recipe_id] for f in favs if f.recipe_id in BY_ID]})

# --- Plano ng linggo + talaan (index) ng mga nakaraang linggo ---------------
# Naka-save sa plan_json ang: {"v": 2, "week": "YYYY-MM-DD" (Lunes), "plan": {...}, "history": {linggo: plano}}
# Kapag nagbago ang linggo, awtomatikong napupunta sa history ang lumang plano at blangko ang bagong linggo.
PH_TZ = timezone(timedelta(hours=8))

def week_start_iso():
    d = datetime.now(PH_TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()

def has_dishes(plan):
    return isinstance(plan, dict) and any(
        isinstance(day, dict) and any(v and v != '-' for v in day.values()) for day in plan.values())

def load_store(u):
    ws = week_start_iso()
    try:
        raw = json.loads(u.plan_json) if u.plan_json else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get('v') == 2:
        store = {'week': raw.get('week') or ws, 'plan': raw.get('plan') or {}, 'history': raw.get('history') or {}}
        changed = False
    else:  # lumang format: ang buong laman ay ang plano ng kasalukuyang linggo
        store = {'week': ws, 'plan': raw, 'history': {}}
        changed = bool(raw)
    if store['week'] != ws:
        if has_dishes(store['plan']):
            store['history'][store['week']] = store['plan']
        store['plan'], store['week'], changed = {}, ws, True
    return store, changed

def save_store(u, store):
    u.plan_json = json.dumps({'v': 2, **store}, ensure_ascii=False)
    db.session.commit()

@app.post('/api/plan')
@login_required
def api_save_plan(u):
    body = request.get_json(silent=True) or {}
    store, _ = load_store(u)
    plan = body.get('plan', {})
    store['plan'] = plan if isinstance(plan, dict) else {}
    save_store(u, store)
    return jsonify({'ok': True})

@app.get('/api/plan')
@login_required
def api_get_plan(u):
    store, changed = load_store(u)
    if changed:
        save_store(u, store)
    return jsonify({'plan': store['plan']})

@app.get('/api/history')
@login_required
def api_history(u):
    """Lahat ng ulam na nailagay sa kalendaryo, bawat linggo (para sa Talaan ng Ulam)."""
    store, changed = load_store(u)
    if changed:
        save_store(u, store)
    weeks = [{'start': k, 'plan': v} for k, v in store['history'].items() if has_dishes(v)]
    if has_dishes(store['plan']):
        weeks.append({'start': store['week'], 'plan': store['plan']})
    weeks.sort(key=lambda w: w['start'], reverse=True)
    return jsonify({'weeks': weeks})

# ---------------------------------------------------------------------------
# SERVER RUNNER
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)