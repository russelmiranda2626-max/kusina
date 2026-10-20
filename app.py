"""
KusinaAI Digital Cookbook & Calendar: Main Application Entry
"""
import os
import re
import ssl
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

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
except Exception:
    df = pd.DataFrame(columns=COLUMNS)

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
        return '/' + cur.lstrip('/')
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

def _wiki_get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'KusinaAI/1.0 (digital cookbook)'})
    try:
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except (ssl.SSLError, urllib.error.URLError) as e:
        if 'CERTIFICATE_VERIFY_FAILED' not in str(e):
            raise
        # Sira ang certificate store ng makina mo (hal. antivirus o lumang Windows certs).
        # Public na data lang ito mula sa Wikipedia kaya dito lang ito pinapayagan.
        with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as r:
            return json.loads(r.read().decode('utf-8'))

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
    ('lechon kawali', 650), ('sisig', 480), ('bicol express', 520), ('kare', 560),
    ('adobo', 430), ('sinigang', 280), ('tinola', 250), ('nilaga', 350), ('pinakbet', 180),
]

def estimate_kcal(title, ingredients=''):
    t = str(title).lower()
    for key, val in DISH_KCAL:
        if key in t:
            return val
    return 300

df['kcal'] = [estimate_kcal(t, i) for t, i in zip(df['title'], df['ingredients'])]
PUBLIC_COLS = ['id', 'title', 'category', 'ingredients', 'instructions', 'budget_level', 'best_for', 'image_url', 'kcal']
ALL_RECORDS = json.loads(df[PUBLIC_COLS].to_json(orient='records', force_ascii=False))
BY_ID = {r['id']: r for r in ALL_RECORDS}
CATEGORIES = sorted({str(c) for c in df['category'] if str(c)})

def records(sub):
    return [BY_ID[i] for i in sub['id'].tolist() if i in BY_ID]

# ---------------------------------------------------------------------------
# AI HELPERS (chat + weekly meal plan)
# ---------------------------------------------------------------------------
DAYS = ['Lunes', 'Martes', 'Miyerkules', 'Huwebes', 'Biyernes', 'Sabado', 'Linggo']
MEALS = ['Almusal', 'Tanghalian', 'Hapunan']

_WEEK_RE = re.compile(
    r'(is+ang|1|one|\ba|buong|whole)\s*-?\s*(linggo|lingo|week)|weekly|linggu-?linggo|'
    r'7\s*(days|araw)|pitong\s+araw|meal\s*plan', re.I)
_FOOD_RE = re.compile(
    r'ulam|menu|meal|plan|pagkain|kainin|lutuin|ihanda|kalendaryo|calendar|almusal|hapunan|tanghalian', re.I)

def wants_week_plan(q):
    return bool(_WEEK_RE.search(q) and _FOOD_RE.search(q))

def gemini_text(prompt, key):
    """Subukan ang SDK, tapos ang direktang HTTP request. Ibabalik ang text o None."""
    try:
        ai_client = genai.Client(api_key=key)
        for model_name in ['gemini-2.5-flash', 'gemini-3.8-flash', 'gemini-flash-latest']:
            try:
                resp = ai_client.models.generate_content(model=model_name, contents=prompt)
                if resp and resp.text:
                    return resp.text
            except Exception:
                continue
    except Exception as e:
        print("SDK Error:", e)

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'X-Goog-Api-Key': key,
                'Authorization': f'Bearer {key}'
            }
        )
        with urllib.request.urlopen(req, context=_SSL_CTX) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return res_data['candidates'][0]['content']['parts'][0]['text']
    except Exception as err:
        print("Direct REST Call Error:", err)
    return None

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

def build_plan_prompt(q, lang='tl'):
    titles = ', '.join(df['title'].astype(str).head(80).tolist())
    return (
        "Ikaw ang KusinaAI chef sa Pilipinas. Gumawa ng meal plan para sa isang linggo ayon sa hiling na ito: "
        f"{q}\n\n"
        "Gumamit ng mga ulam na pang-Pilipino at iba-iba bawat araw. Hangga't maaari, piliin ang mga ulam "
        f"mula sa listahang ito ng cookbook (puwede ring ibang kilalang ulam kung kulang): {titles}\n\n"
        'Ibalik LAMANG ang isang JSON object, walang ibang teksto at walang code fence, sa ganitong anyo: '
        '{"buod": "isang maikling pangungusap sa Filipino", "plan": {"Lunes": {"Almusal": "...", '
        '"Tanghalian": "...", "Hapunan": "..."}, "Martes": {...}, ... hanggang "Linggo"}}. '
        "Gamitin nang eksakto ang mga susi na Lunes, Martes, Miyerkules, Huwebes, Biyernes, Sabado, Linggo "
        "at Almusal, Tanghalian, Hapunan. Pangalan lang ng ulam ang laman ng bawat slot."
        + (" Isulat sa English ang 'buod' at ang mga pangalan ng ulam." if lang == 'en' else "")
    )

# ---------------------------------------------------------------------------
# MAIN ROUTES, LOGOUT & AI CHAT
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    u = current_user()
    if not u:
        return render_template('login.html')

    boot = {'featured': ALL_RECORDS[:24], 'total': len(ALL_RECORDS), 'categories': CATEGORIES}
    return render_template('index.html', boot=boot, featured_recipes=ALL_RECORDS[:24], total=len(ALL_RECORDS), categories=CATEGORIES)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.get('/api/recipes')
def api_recipes():
    q = request.args.get('q', '').strip().lower()
    cat = request.args.get('category', '').strip()
    sub = df
    if q:
        sub = sub[sub['title'].str.lower().str.contains(q, na=False) | sub['ingredients'].str.lower().str.contains(q, na=False)]
    if cat:
        sub = sub[sub['category'] == cat]
    return jsonify({'recipes': records(sub.head(24)), 'total': int(len(sub))})

@app.get('/api/dish_image')
def api_dish_image():
    t = request.args.get('title', '').strip()[:120]
    return jsonify({'url': wiki_image(t) if t else ''})

@app.post('/ask_ai')
def ask_ai():
    data = request.get_json(silent=True) or {}
    q = str(data.get('query', '')).strip()[:400]
    lang = 'en' if data.get('lang') == 'en' else 'tl'
    if not q:
        return jsonify({'reply': 'Ask me something about cooking!' if lang == 'en'
                        else 'Magtanong ka ng tungkol sa pagluluto!'})

    current_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not current_key:
        return jsonify({'reply': 'Walang nahanap na GEMINI_API_KEY sa .env file.'})

    # Kapag humingi ng pang-isang-linggong ulam: gumawa ng plano at ibalik para
    # awtomatikong mailagay sa kalendaryo.
    if wants_week_plan(q):
        obj = extract_json(gemini_text(build_plan_prompt(q, lang), current_key))
        plan = clean_plan(obj) if obj else {}
        if sum(len(v) for v in plan.values()) >= 7:
            lines = []
            buod = str(obj.get('buod', '')).strip() if isinstance(obj, dict) else ''
            if buod:
                lines.append(buod)
            for d in DAYS:
                if d in plan:
                    parts = ' • '.join(f"{m}: {plan[d][m]}" for m in MEALS if m in plan[d])
                    lines.append(f"**{d}** — {parts}")
            lines.append("I've added this plan to your Calendar." if lang == 'en'
                         else "Nailagay ko na ang planong ito sa Kalendaryo mo.")
            return jsonify({'reply': '\n'.join(lines), 'plan': plan})

    if lang == 'en':
        prompt = f"You are KusinaAI, a Filipino chef. Answer clearly, in detail, and completely in English: {q}"
    else:
        prompt = f"Ikaw ang KusinaAI chef sa Pilipinas. Sagutin sa Filipino nang maayos, detalyado, at kumpleto ang tanong na ito: {q}"
    text = gemini_text(prompt, current_key)
    if text:
        return jsonify({'reply': text})

    return jsonify({'reply': 'Siguraduhing nakalagay ang tamang GEMINI_API_KEY sa .env file.'})

@app.post('/api/translate')
def api_translate():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text', '')).strip()[:1000]
    direction = data.get('direction', 'en-tl')
    if not text:
        return jsonify({'ok': False, 'error': 'Walang tekstong isasalin.'})

    key = os.getenv("GEMINI_API_KEY", "").strip()
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
        return jsonify({'ok': False, 'error': 'Hindi makuha ang salin ngayon. Subukan ulit.'})
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

    key = os.getenv("GEMINI_API_KEY", "").strip()
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
        return jsonify({'ok': False, 'error': 'Hindi makuha ang salin ngayon.'})
    return jsonify({'ok': True, 'translations': [str(x) for x in arr]})

# ---------------------------------------------------------------------------
# MEAL CALENDAR & FAVORITES
# ---------------------------------------------------------------------------
@app.get('/api/favorites')
@login_required
def api_get_favorites(u):
    favs = db.session.execute(db.select(Favorite).filter_by(user_id=u.id)).scalars().all()
    return jsonify({'favorites': [BY_ID[f.recipe_id] for f in favs if f.recipe_id in BY_ID]})

@app.post('/api/plan')
@login_required
def api_save_plan(u):
    body = request.get_json(silent=True) or {}
    u.plan_json = json.dumps(body.get('plan', {}), ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True})

@app.get('/api/plan')
@login_required
def api_get_plan(u):
    try:
        plan = json.loads(u.plan_json) if u.plan_json else {}
    except Exception:
        plan = {}
    return jsonify({'plan': plan})

# ---------------------------------------------------------------------------
# SERVER RUNNER
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)