from flask import Flask, render_template, request, jsonify, Response
import pandas as pd
import os
import re
import time
import difflib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

app = Flask(__name__)

api_key = os.getenv("GEMINI_API_KEY")
# May timeout para hindi mag-hang kapag mabagal o walang response ang Gemini
client = (
    genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=45000))
    if api_key else None
)

COLUMNS = ['id', 'title', 'category', 'ingredients', 'instructions',
           'budget_level', 'best_for', 'image_url']

# Ilang resipi lang ang ilo-load sa home page (para hindi bumigat ang browser)
HOME_LIMIT = 24

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
try:
    if os.path.exists('recipes.csv'):
        df = pd.read_csv('recipes.csv')
    else:
        df = pd.DataFrame(columns=COLUMNS)
except Exception:
    df = pd.DataFrame(columns=COLUMNS)

# Tiyaking aabot ng 200 ang recipes sa database
if len(df) == 0:  # fallback lang ito kung walang laman o nawawala ang recipes.csv
    extra_dishes = []
    categories = ['Ulam', 'Sabaw', 'Gulay', 'Pritong Ulam', 'Meryenda', 'Pansit']
    budgets = ['Mura / Budget-Friendly', 'Katamtaman', 'Espesyal']
    sikat_ulam_list = [
        "Adobo", "Sinigang", "Tinola", "Pinakbet", "Menudo", "Afritada", "Caldereta",
        "Paksiw", "Lechon Kawali", "Sisig", "Chopsuey", "Fried Chicken", "Arroz Caldo",
        "Champorado", "Pancit Canton", "Bicol Express", "Bulalo", "Kare-Kare", "Dinuguan", "Bangus Sisig"
    ]

    for i in range(len(df) + 1, 201):
        base_name = sikat_ulam_list[(i - 1) % len(sikat_ulam_list)]
        dish_title = f"{base_name} Bersyon {i}"
        cat = categories[(i - 1) % len(categories)]
        bud = budgets[(i - 1) % len(budgets)]

        extra_dishes.append({
            'id': i,
            'title': dish_title,
            'category': cat,
            'ingredients': 'bawang sibuyas kamatis patis toyo mantika karne gulay',
            'instructions': f'1. Ihanda ang mga sangkap para sa {dish_title}.\n2. Igisa ang bawang at sibuyas.\n3. Ilagay ang pangunahing sahog at pakuluan o prituhin hanggang maluto.\n4. Timplahan ayon sa panlasa at ihain.',
            'budget_level': bud,
            'best_for': 'Pang-araw-araw na Hapag-kainan',
            'image_url': '/static/images/adobo.jpg'
        })
    df = pd.concat([df, pd.DataFrame(extra_dishes)], ignore_index=True)

# ---------------------------------------------------------------------------
# LARAWAN
# ---------------------------------------------------------------------------
DEFAULT_IMAGE = "/static/images/adobo.jpg"
PLACEHOLDER_URL = "/placeholder.svg"
RECIPE_IMAGES = {
    "Adobong Manok": "/static/images/adobo.jpg",
    "Sinigang na Baboy": "/static/images/download.jpg",
    "Tinolang Manok": "/static/images/images (1).jpg",
    "Ginisang Munggo na may Malunggay": "/static/images/images (2).jpg",
    "Tortang Talong": "/static/images/torta.jpg",
    "Chicken Curry": "/static/images/EasyChickenCurry3.jpg",
    "Pork Sisig": "/static/images/images (3).jpg",
    "Champorado": "/static/images/champorado-7-scaled.jpg"
}
_exists_cache = {}


def pick_image(title):
    t = str(title).lower()
    for key, img_path in RECIPE_IMAGES.items():
        if key.lower() in t:
            return img_path
    return PLACEHOLDER_URL  # walang tugmang larawan, kaya placeholder


def image_or_placeholder(url):
    if url not in _exists_cache:
        local = os.path.join(app.root_path, url.lstrip('/'))
        _exists_cache[url] = os.path.exists(local)
    return url if _exists_cache[url] else PLACEHOLDER_URL


df['image_url'] = df['title'].map(pick_image).map(image_or_placeholder)
df = df.fillna('')
df['instructions'] = df['instructions'].astype(str).str.replace('\\n', '\n', regex=False)

SEARCH_TEXT = (
    df['title'].astype(str) + ' ' +
    df['category'].astype(str) + ' ' +
    df['ingredients'].astype(str)
).str.lower()

ALL_RECORDS = df.to_dict(orient='records')
TOTAL_COUNT = len(df)

# ---------------------------------------------------------------------------
# PAG-INTINDI SA TANONG
# ---------------------------------------------------------------------------
PH_TZ = timezone(timedelta(hours=8))
DAY_NAMES = ['Lunes', 'Martes', 'Miyerkules', 'Huwebes', 'Biyernes', 'Sabado', 'Linggo']
MONTH_NAMES = ['Enero', 'Pebrero', 'Marso', 'Abril', 'Mayo', 'Hunyo', 'Hulyo', 'Agosto',
               'Setyembre', 'Oktubre', 'Nobyembre', 'Disyembre']

MEAL_KEYWORDS = {
    'umagahan':   ['umagahan', 'almusal', 'agahan', 'breakfast', 'umaga'],
    'tanghalian': ['tanghalian', 'tanghali', 'lunch'],
    'meryenda':   ['meryenda', 'merienda', 'miryenda', 'snack'],
    'hapunan':    ['hapunan', 'hapunin', 'dinner', 'gabi'],
}

MEAL_LABEL = {
    'umagahan': 'Umagahan (Almusal)',
    'tanghalian': 'Tanghalian',
    'meryenda': 'Meryenda',
    'hapunan': 'Hapunan',
}

MEAL_GUIDE = {
    'umagahan': "Pang-umagahan: mga pagkaing karaniwang almusal ng Pilipino...",
    'tanghalian': "Pang-tanghalian: mabigat at kumpletong pagkain na swak sa kanin...",
    'meryenda': "Pang-meryenda: maliit na serving na pang-hapon o pang-pagitan...",
    'hapunan': "Pang-hapunan: ulam na pampamilya na masarap ihain sa gabi...",
}

MEAL_FILTERS = {
    'umagahan': {'keywords': ['champorado', 'arroz caldo', 'lugaw', 'goto', 'tapsilog', 'longsilog', 'tocilog',
                              'sinangag', 'tortang', 'pandesal', 'daing', 'tuyo'], 'categories': ['Almusal']},
    'tanghalian': {'keywords': [], 'categories': ['Ulam', 'Pritong Ulam', 'Gulay', 'Sabaw']},
    'meryenda': {'keywords': ['champorado', 'turon', 'banana', 'bibingka', 'puto', 'kutsinta', 'lugaw', 'pancit'],
                 'categories': ['Meryenda', 'Pansit']},
    'hapunan': {'keywords': [], 'categories': ['Ulam', 'Sabaw', 'Gulay']},
}

NUM_WORDS = {'isa': 1, 'dalawa': 2, 'tatlo': 3, 'apat': 4, 'lima': 5, 'anim': 6, 'pito': 7}
COUNT_RE = re.compile(
    r'(\d+|isa|dalawa|tatlo|apat|lima|anim|pito)(?:ng)?\s*(?:na\s*)?'
    r'(?:putahe|ulam|resipi|recipe|ideya|idea|mungkahi|suggestion|dish)')


def fmt_date(d):
    return f"{DAY_NAMES[d.weekday()]}, {MONTH_NAMES[d.month - 1]} {d.day}"


DAY_ALIASES = ['lunes', 'martes', 'miyerkules', 'huwebes', 'biyernes', 'sabado', 'linggo']
KW_TO_MEAL = {kw: meal for meal, kws in MEAL_KEYWORDS.items() for kw in kws}


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
        count = NUM_WORDS.get(tok) or int(tok)
        count = max(1, min(count, 10))

    day_hits = []
    for w in words:
        d = _fuzzy(w, DAY_ALIASES)
        if d:
            day_hits.append(DAY_ALIASES.index(d))
    day_range = None
    if len(day_hits) >= 2 and day_hits[0] != day_hits[-1]:
        day_range = (day_hits[0], (day_hits[-1] - day_hits[0]) % 7 + 1)

    has_plan = any(w in q for w in ['meal plan', 'mealplan'])

    if any(w in q for w in ['buwan', 'month', '30 araw', '30 days']):
        span = 'buwan'
    elif day_range:
        span = 'linggo'
    elif any(w in q for w in ['next week', 'susunod na linggo', 'isang linggo', 'lingguhan', 'weekly',
                                'week', '7 araw', '7 days']):
        span = 'linggo'
    elif any(w in q for w in ['bukas', 'kinabukasan', 'tomorrow']):
        span = 'bukas'
    elif has_plan:
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
- Pilipinong putahe at sangkap na madaling mabili sa palengke. Sundin din ang anumang dagdag na hiling.
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

    return rules + guide + task


# ---------------------------------------------------------------------------
# PAGHAHANAP NG RESIPI PARA SA GRID
# ---------------------------------------------------------------------------
STOPWORDS = {'anong', 'ano', 'masarap', 'na', 'pang', 'para', 'sa', 'ng', 'ang', 'mga', 'ba', 'nga',
             'bukas', 'next', 'week', 'ngayon', 'naman', 'po', 'paki', 'pakibigyan', 'bigyan', 'ako',
             'umagahan', 'tanghalian', 'meryenda', 'hapunan', 'plan', 'meal', 'lutuin', 'ulam'}


def search_recipes(query, intent):
    meals = intent['meals']

    if meals:
        cats, kws = set(), set()
        for m in meals:
            cats.update(MEAL_FILTERS[m]['categories'])
            kws.update(MEAL_FILTERS[m]['keywords'])
        mask = df['category'].isin(cats)
        if kws:
            pattern = '|'.join(re.escape(k) for k in kws)
            mask = mask | SEARCH_TEXT.str.contains(pattern, regex=True)
        matches = df[mask]
        if len(matches) > 0:
            return matches.sample(n=min(20, len(matches))).to_dict(orient='records')

    q = query.lower()
    matches = df[SEARCH_TEXT.str.contains(q, regex=False)]
    if len(matches) == 0:
        words = [w for w in re.findall(r'\w+', q) if len(w) >= 4 and w not in STOPWORDS]
        if words:
            pattern = '|'.join(re.escape(w) for w in words)
            matches = df[SEARCH_TEXT.str.contains(pattern, regex=True)]
    if len(matches) == 0:
        matches = df.sample(n=min(16, len(df)))
    return matches.head(20).to_dict(orient='records')


# ---------------------------------------------------------------------------
# AI GENERATION
# ---------------------------------------------------------------------------
MODELS_TO_TRY = ['gemini-2.5-flash', 'gemini-2.0-flash']
working_model = None


def recipes_for_meal(meal, n):
    f = MEAL_FILTERS[meal]
    mask = df['category'].isin(f['categories'])
    if f['keywords']:
        mask = mask | SEARCH_TEXT.str.contains('|'.join(re.escape(k) for k in f['keywords']), regex=True)
    pool = df[mask]
    if len(pool) == 0:
        pool = df
    return pool.sample(n=min(n, len(pool))).to_dict(orient='records')


def local_fallback(user_query, intent, recipes, reason=""):
    span, n = intent['span'], intent['count']
    if reason:
        print(f"[fallback] {reason}")
    if not recipes:
        return f"Chef AI: Hindi ko maabot ang AI ngayon. Subukan ulit mamaya para sa '{user_query}'."

    meals = intent['meals'] or (list(MEAL_LABEL) if span in ('linggo', 'buwan') else [])
    lines = ["🍳 Hindi ko maabot ang AI ngayon, pero narito ang mungkahi mula sa cookbook:", ""]

    if span in ('linggo', 'buwan'):
        days = plan_days(intent) if span == 'linggo' else [f"Araw {i + 1}" for i in range(28)]
        pools = {m: recipes_for_meal(m, len(days)) for m in meals}
        for i, d in enumerate(days):
            lines.append(f"📅 {d}")
            for m in meals:
                p = pools[m]
                lines.append(f"   {MEAL_LABEL[m]}: {p[i % len(p)]['title']}")
            lines.append("")
    elif meals:
        for m in meals:
            lines.append(f"🍽️ {MEAL_LABEL[m]}")
            for r in recipes_for_meal(m, n):
                lines.append(f"• {r['title']}")
                lines.append(f"Sangkap: {r['ingredients']}")
                lines.append(f"Paraan:\n{r['instructions']}\n")
            lines.append("")
    else:
        for r in recipes[:n]:
            lines.append(f"🍽️ {r['title']}")
            lines.append(f"Sangkap: {r['ingredients']}")
            lines.append(f"Paraan:\n{r['instructions']}\n")
    return "\n".join(lines)


def generate_ai_reply(user_query, intent, recipes):
    global working_model

    prompt = build_prompt(user_query, intent)
    big = intent['span'] in ('linggo', 'buwan')
    max_tokens = 8192 if intent['span'] == 'buwan' else (4096 if big else 2048)

    order = MODELS_TO_TRY[:]
    if working_model in order:
        order.remove(working_model)
        order.insert(0, working_model)

    started = time.time()
    last_error = ""
    for m in order:
        if time.time() - started > 30:
            break
        try:
            cfg = {'temperature': 0.8, 'max_output_tokens': max_tokens}
            if '2.5' in m:
                cfg['thinking_config'] = types.ThinkingConfig(thinking_budget=0)
            response = client.models.generate_content(model=m, contents=prompt, config=cfg)
            if response and response.text:
                working_model = m
                return response.text
        except Exception as e:
            print(f"[Gemini:{m}] {type(e).__name__}: {e}")
            msg = str(e)
            last_error = f"[{m}] {type(e).__name__}: {msg}"
            if any(k in msg for k in ("API_KEY", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "429", "401", "403")):
                break
            continue

    return local_fallback(user_query, intent, recipes, last_error or "Walang sagot mula sa AI.")


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.route('/placeholder.svg')
def placeholder():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300">'
           '<rect width="400" height="300" fill="#f3e9dc"/>'
           '<text x="200" y="160" font-size="60" text-anchor="middle">🍲</text></svg>')
    resp = Response(svg, mimetype='image/svg+xml')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/')
def home():
    return render_template('index.html',
                           featured_recipes=ALL_RECORDS[:HOME_LIMIT],
                           total_count=TOTAL_COUNT)


@app.route('/ask_ai', methods=['POST'])
def ask_ai():
    data = request.get_json(silent=True) or {}
    user_query = str(data.get('query', '')).strip()

    if not user_query:
        return jsonify({'reply': 'Magtanong ka ng tungkol sa pagluluto o humingi ng meal plan!', 'recipes': []})

    intent = detect_intent(user_query)
    recipes_list = search_recipes(user_query, intent)

    if client:
        ai_reply = generate_ai_reply(user_query, intent, recipes_list)
    else:
        ai_reply = "Walang nahanap na GEMINI_API_KEY sa iyong .env file."

    return jsonify({'reply': ai_reply, 'recipes': recipes_list})


if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)