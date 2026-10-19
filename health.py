"""Calories, pampapayat, at gabay sa pagkain kapag may sakit para sa KusinaAI."""
import re

SLIM_TARGET = 1500  # tantiyang kcal/araw ng nagpapapayat

# (susi sa pangalan ng putahe, tantiyang kcal bawat serving). Mas tiyak ang nauuna.
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


def lean_mask(df):
    """True para sa ulam na mababa sa calories at hindi prito/may gata."""
    text = (df['title'].astype(str) + ' ' + df['ingredients'].astype(str)).str.lower()
    return (df['kcal'] <= 350) & ~text.str.contains(NOT_LEAN.pattern, regex=True)


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
    """Ibalik ang kondisyong tinukoy (pinakamahabang susi ang nananalo), o None."""
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
    """Dagdag na utos para sa Gemini kapag tungkol sa sakit o pagpapapayat ang tanong. Walang laman kung hindi."""
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


def health_recipes(df, search_text, query, limit=20):
    """Resipi mula sa BUONG catalog na bagay sa sakit/pampapayat. Walang laman na list kung hindi tungkol dito."""
    cond, slim = detect_health(query), is_slim(query)
    if not cond and not slim:
        return []
    if cond:
        pool = df[search_text.str.contains(cond['match'], regex=True)]
        if slim:
            pool = pool[lean_mask(pool)]
        pool = pool.sample(n=min(limit, len(pool))) if len(pool) else pool
    else:
        pool = df[lean_mask(df)].sort_values('kcal').head(limit)
    return pool.to_dict(orient='records')