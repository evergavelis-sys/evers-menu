#!/usr/bin/env python3
"""Compute per-serving macros for recipes using USDA FoodData Central.

Usage:
  FDC_API_KEY=... python3 scripts/fdc_macros.py [--test RECIPE_ID ...]
  FDC_API_KEY=... python3 scripts/fdc_macros.py --all

Output: scripts/out/macros_report.json (old vs new per recipe, with warnings).
When --apply is passed, patches index.html in place.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(__file__).resolve().parent / 'cache'
OUT = Path(__file__).resolve().parent / 'out'
CACHE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

KEY = os.environ.get('FDC_API_KEY')
if not KEY:
    # fall back to .env
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith('FDC_API_KEY='):
                KEY = line.split('=', 1)[1].strip()
                break
if not KEY:
    sys.exit('FDC_API_KEY not set')

BASE = 'https://api.nal.usda.gov/fdc/v1'
# Energy: 1008 (SR Legacy kcal), 2047/2048 (Foundation Atwater computed kcal)
N_ENERGY_IDS = {1008, 2047, 2048}
N_PROTEIN, N_FAT, N_CARB = 1003, 1004, 1005

# ---- HTTP + cache ----------------------------------------------------------

def _fetch(url: str, cache_key: str) -> dict:
    p = CACHE / f'{cache_key}.json'
    if p.exists():
        return json.loads(p.read_text())
    for attempt in range(3):
        try:
            req = Request(url, headers={'Accept': 'application/json'})
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            p.write_text(json.dumps(data))
            time.sleep(0.1)
            return data
        except (HTTPError, URLError) as e:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError('unreachable')


def fdc_search(query: str, page_size: int = 10) -> list[dict]:
    key = 'search_' + re.sub(r'[^a-z0-9]+', '_', query.lower()).strip('_')
    url = f'{BASE}/foods/search?query={quote(query)}&pageSize={page_size}&dataType={quote("SR Legacy,Foundation")}&api_key={KEY}'
    return _fetch(url, key).get('foods', [])


def fdc_food(fdc_id: int) -> dict:
    url = f'{BASE}/food/{fdc_id}?api_key={KEY}'
    return _fetch(url, f'food_{fdc_id}')


# ---- nutrient extraction ---------------------------------------------------

def macros_per_100g(food: dict) -> dict:
    """Return {cal, prot, fat, carb} per 100g. Prefer SR Legacy 1008 > Atwater 2048 > 2047."""
    out = {}
    energy_priority = {1008: 3, 2048: 2, 2047: 1}  # higher = better
    current_priority = 0
    for n in food.get('foodNutrients', []):
        nutrient = n.get('nutrient') or {}
        nid = nutrient.get('id') or n.get('nutrientId')
        amt = n.get('amount')
        if amt is None:
            amt = n.get('value')
        if amt is None:
            continue
        if nid in N_ENERGY_IDS:
            unit = (nutrient.get('unitName') or n.get('unitName') or '').lower()
            if unit == 'kj':
                continue
            p = energy_priority.get(nid, 0)
            if p > current_priority:
                out['cal'] = amt
                current_priority = p
        elif nid == N_PROTEIN:
            out['prot'] = amt
        elif nid == N_FAT:
            out['fat'] = amt
        elif nid == N_CARB:
            out['carb'] = amt
    return out


def pick_best(foods: list[dict], query: str) -> dict | None:
    if not foods:
        return None
    def score(f):
        m = macros_per_100g(f)
        complete = all(k in m for k in ('cal', 'prot', 'fat', 'carb'))
        # SR Legacy tends to have Energy explicit; Foundation sometimes omits
        legacy = f.get('dataType') == 'SR Legacy'
        # Prefer "raw" entries for proteins unless cooking is implied
        desc = (f.get('description') or '').lower()
        raw_pref = 1 if 'raw' in desc else 0
        return (complete, legacy, raw_pref)
    return max(foods, key=score)


# ---- ingredient parsing ----------------------------------------------------

# Generic density defaults (grams per unit) when FDC foodPortions doesn't help
GENERIC_DENSITY = {
    'cup':    240.0,  # water baseline; overridden per food below
    'tbsp':   15.0,
    'tsp':    5.0,
    'oz':     28.35,
    'ounce':  28.35,
    'ounces': 28.35,
    'g':      1.0,
    'gram':   1.0,
    'grams':  1.0,
    'kg':     1000.0,
    'lb':     453.6,
    'ml':     1.0,
    'l':      1000.0,
    'liter':  1000.0,
    'pinch':  0.5,
    'dash':   0.6,
    'clove':  3.0,
    'cloves': 3.0,
    'slice':  28.0,
    'slices': 28.0,
    'can':    400.0,   # overridden if "(14oz)" etc. in ingredient text
    'stalk':  40.0,
    'stalks': 40.0,
    'scoop':  30.0,    # protein powder
    'scoops': 30.0,
    'handful': 30.0,
    'piece':  50.0,
    'pieces': 50.0,
    'block':  396.0,   # tofu block
    'blocks': 396.0,
    'fillet': 140.0,
    'fillets':140.0,
    'serving':100.0,
    'servings':100.0,
    'cube':   5.0,    # dorot garlic/herb cubes
    'cubes':  5.0,
    'wedge':  30.0,
    'wedges': 30.0,
    'sprig':  2.0,
    'sprigs': 2.0,
}

# Per-food density overrides for "cup" etc. (grams/cup)
FOOD_DENSITY = {
    # liquids ~ water
    'water':          240, 'broth': 240, 'vegetable broth': 240, 'chicken broth': 240,
    'milk':           245, 'oat milk': 240, 'almond milk': 240, 'coconut milk': 240,
    'heavy cream':    238, 'cream': 238,
    # oils/fats
    'olive oil':      216, 'vegetable oil': 218, 'sesame oil': 218, 'coconut oil': 218,
    'butter':         227,
    # sauces
    'soy sauce':      255, 'tamari': 255, 'fish sauce': 255, 'oyster sauce': 280,
    'sriracha':       250, 'hot sauce': 250, 'honey': 340, 'maple syrup': 320,
    'mayo':           230, 'mayonnaise': 230,
    'balsamic vinegar': 240, 'rice vinegar': 240, 'vinegar': 240,
    'lemon juice':    244, 'lime juice': 242, 'orange juice': 248,
    'tahini':         240,
    # grains (uncooked)
    'rice':           185, 'white rice': 185, 'brown rice': 190, 'jasmine rice': 185,
    'basmati rice':   185, 'quinoa': 170, 'farro': 185,
    'rolled oats':    80,  'oats': 80, 'steel cut oats': 170,
    'flour':          120, 'all-purpose flour': 120, 'whole wheat flour': 120,
    # pasta dry
    'pasta':          100, 'spaghetti': 100, 'penne': 100, 'fettuccine': 100,
    'protein pasta':  100,
    # dairy solids
    'greek yogurt':   245, 'yogurt': 245, 'cottage cheese': 225,
    'parmesan':       100, 'parmigiano reggiano': 100, 'pecorino romano': 100,
    'feta':           150, 'feta cheese': 150, 'shredded cheese': 113,
    'mexican cheese blend': 113, 'mini chocolate chips': 180,
    # nuts/seeds
    'peanut butter':  258, 'almond butter': 258, 'peanut butter ': 258,
    'ground flaxseed': 120, 'chia seeds': 170, 'sesame seeds': 144,
    # sweeteners
    'sugar':          200, 'brown sugar': 220,
    # produce (loose/chopped cup)
    'spinach':        30,  'baby spinach': 30, 'kale': 67, 'arugula': 20, 'spring mix': 20,
    'lettuce':        50,  'shredded lettuce': 50,
    'broccoli':       91,  'broccoli florets': 91,
    'cherry tomato':  149, 'cherry tomatoes': 149, 'cherry tomato medley': 149,
    'tomato':         180, 'diced tomatoes': 240, 'crushed tomatoes': 240, 'canned tomatoes': 240,
    'cucumber':       119, 'mushroom': 70, 'mushrooms': 70, 'mushrooms sliced': 70,
    'onion':          160, 'red onion': 160, 'corn': 154, 'black beans': 172,
    'chickpeas':      164, 'edamame': 118, 'peas': 145, 'frozen peas': 145,
    'avocado':        150, 'mango': 165, 'frozen mango': 165, 'banana': 150,
    'frozen banana':  150, 'berries': 144, 'granola': 120, 'hummus': 240,
    'fresh parsley':  60,  'parsley': 60, 'fresh basil': 24, 'basil': 24, 'cilantro': 16,
    'fresh dill':     10,  'dill': 10, 'fresh thyme': 28, 'thyme': 28,
    'fresh ginger':   96,
    'kalamata olives':135,
    'pickled ginger': 120,
}

# Generic "per count" weights for bare numbers + food name (grams each)
PER_ITEM = {
    'egg': 50, 'eggs': 50, 'large egg': 50, 'large eggs': 50,
    'banana': 118, 'apple': 182, 'lemon': 58, 'lime': 67, 'orange': 131,
    'beet': 82, 'onion': 110, 'red onion': 110, 'tomato': 123,
    'avocado': 150, 'zucchini': 196, 'cucumber': 301, 'bell pepper': 119,
    'red bell pepper': 119, 'garlic': 3, 'clove': 3, 'cloves': 3,
    'salmon fillet': 170, 'chicken breast': 170, 'chicken thigh': 130,
    'chicken breasts': 170, 'salmon fillets': 170,
    'tortilla': 45, 'flour tortilla': 62, 'rice cake': 9, 'rice cakes': 9,
    'slice': 28, 'slices': 28, 'fillet': 140, 'fillets': 140,
    'fish1': 170, 'mini cucumber': 100,
}

FRACTION_MAP = {
    '½': '1/2', '⅓': '1/3', '⅔': '2/3', '¼': '1/4', '¾': '3/4',
    '⅛': '1/8', '⅜': '3/8', '⅝': '5/8', '⅞': '7/8',
}

UNIT_SYNONYMS = {
    't': 'tsp', 'T': 'tbsp', 'teaspoon': 'tsp', 'teaspoons': 'tsp',
    'tbs': 'tbsp', 'tablespoon': 'tbsp', 'tablespoons': 'tbsp', 'Tbsp': 'tbsp',
    'cups': 'cup', 'c': 'cup', 'C': 'cup',
    'ounce': 'oz', 'ounces': 'oz',
    'pound': 'lb', 'pounds': 'lb', 'lbs': 'lb',
    'grams': 'g', 'gram': 'g',
    'milliliter': 'ml', 'milliliters': 'ml', 'liter': 'l', 'liters': 'l',
    'package': 'can',  # best guess
}

# Ingredients we should effectively skip entirely (trace/unmeasurable)
SKIP_KEYWORDS = {'to taste', 'optional', 'for serving', 'for garnish', 'as needed',
                 'salt and pepper', 'salt & pepper', 'splash', 'drizzle',
                 'for dipping', 'for finishing', 'pasta water', 'hot water',
                 'water', 'flaky salt', 'salt', 'cracked pepper',
                 'salt and cracked pepper', 'kosher salt', 'sea salt',
                 'flaky salt and black pepper', 'salt flakes'}

# Amount-words that indicate a small/unmeasured portion — rough gram estimates
AMOUNT_WORDS = {
    'pinch': 0.5, 'dash': 0.6, 'drizzle': 5.0, 'splash': 10.0,
    'handful': 30.0, 'small handful': 20.0, 'large handful': 45.0,
    'few': 15.0, 'some': 20.0, 'several': 25.0,
    'sprinkle': 2.0, 'squeeze': 10.0, 'knob': 15.0,
    'to taste': 0.0, 'optional': 0.0,
}


def normalize_ingredient_name(raw: str) -> str:
    """Strip parentheticals, commas, qualifiers; return searchable name.

    Handles "X or Y" by taking the first option.
    """
    s = raw.lower().strip()
    s = re.sub(r'\([^)]*\)', '', s)        # drop parentheticals
    s = re.sub(r',.*$', '', s)              # drop everything after first comma
    # "X or Y" → X
    if ' or ' in s:
        s = s.split(' or ', 1)[0].strip()
    # strip trailing "to finish" / "for serving" / "on side" etc.
    s = re.sub(r'\s+(to finish|to serve|for dipping|for serving|for garnish|for the |on top|on side|as needed|optional|to taste)\b.*$', '', s)
    # "X and Y" for aromatics is a generic skip-pattern — take first
    if ' and ' in s:
        s = s.split(' and ', 1)[0].strip()
    s = re.sub(r'\s+', ' ', s).strip()
    # strip leading qualifiers (size + prep)
    for lead in ('fresh ', 'frozen ', 'dried ', 'chopped ', 'sliced ', 'diced ',
                 'minced ', 'grated ', 'shredded ', 'crushed ', 'cooked ',
                 'roasted ', 'toasted ', 'baked ', 'cubed ', 'pressed ',
                 'crumbled ', 'thinly sliced ', 'massaged ', 'ripe ', 'ground ',
                 'medium ', 'large ', 'small ', 'big ', 'mini ', 'mixed ',
                 'regular '):
        if s.startswith(lead):
            s = s[len(lead):]
    # trailing descriptive qualifiers
    s = re.sub(r'\s+(sliced|diced|minced|chopped|grated|crumbled|drained|halved|cubed|rinsed|leaves|wedge|wedges|slices|florets)$', '', s)
    return s.strip()


def parse_amount(amount_str: str) -> float | None:
    """Parse '1', '1/2', '1 1/2', '3-4' → float quantity. None if unparseable."""
    s = amount_str.strip().lower()
    for uni, txt in FRACTION_MAP.items():
        s = s.replace(uni, txt)
    if not s:
        return None
    # amount-words ("handful", "pinch", "drizzle") return 1 so grams_for can look up their weight
    if s in AMOUNT_WORDS:
        return 1.0
    # "3-4" → average
    m = re.match(r'^(\d+(?:/\d+)?)\s*-\s*(\d+(?:/\d+)?)$', s)
    if m:
        a = float(Fraction(m.group(1)))
        b = float(Fraction(m.group(2)))
        return (a + b) / 2
    # "1 1/2"
    m = re.match(r'^(\d+)\s+(\d+/\d+)$', s)
    if m:
        return float(int(m.group(1)) + Fraction(m.group(2)))
    # "1/2", "3", "0.5"
    try:
        return float(Fraction(s))
    except (ValueError, ZeroDivisionError):
        pass
    try:
        return float(s)
    except ValueError:
        return None


def parse_amount_with_unit(amount_str: str):
    """Parse '2 tbsp', '1 cup', '400g', '3-4 cloves', 'handful' → (qty, unit)."""
    s = amount_str.strip().lower()
    for uni, txt in FRACTION_MAP.items():
        s = s.replace(uni, txt)
    # amount-word alone: unit=the-word, qty=1
    if s in AMOUNT_WORDS:
        return 1.0, s
    # "400g" glued
    m = re.match(r'^([\d./\- ]+?)\s*(g|kg|ml|l|oz|lb)\b', s, re.IGNORECASE)
    if m:
        qty = parse_amount(m.group(1))
        unit = m.group(2).lower()
        return qty, unit
    # e.g. "2 tbsp" or "1 cup" or "3 cloves"
    m = re.match(r'^([\d./\- ]+?)\s+([a-zA-Z]+)\b', s)
    if m:
        qty = parse_amount(m.group(1).strip())
        unit = m.group(2).lower()
        unit = UNIT_SYNONYMS.get(unit, unit)
        return qty, unit
    # bare number (count of items)
    qty = parse_amount(s)
    return qty, ''


def grams_for(qty: float, unit: str, name: str, full_text: str) -> tuple[float, str]:
    """Return (grams, note). note carries warnings about assumptions."""
    if qty is None:
        return 0.0, f'unparseable qty'
    unit = unit.lower()

    # Amount-words ("handful", "pinch", "drizzle") → fixed gram estimate
    if unit in AMOUNT_WORDS:
        return qty * AMOUNT_WORDS[unit], f'estimate-{unit}'

    # explicit (14oz) in parenthetical
    m = re.search(r'\((\d+(?:\.\d+)?)\s*(oz|ounce|g|ml|lb)s?\)', full_text, re.IGNORECASE)
    if unit in ('can', '') and m:
        inner_qty = float(m.group(1))
        inner_unit = m.group(2).lower()
        per_unit_g = GENERIC_DENSITY.get(inner_unit, 1.0)
        return qty * inner_qty * per_unit_g, ''

    # "serving(s)" → food-dependent (pasta 55g, protein powder 30g, generic 100g)
    if unit in ('serving', 'servings'):
        if 'pasta' in name:
            return qty * 55.0, 'pasta-serving (55g)'
        if 'protein powder' in name or name == 'protein powder':
            return qty * 30.0, 'scoop (30g)'
        return qty * 100.0, f'generic-serving (100g)'

    # weight/volume (direct grams)
    if unit in ('g', 'gram', 'grams'):
        return qty * 1.0, ''
    if unit in ('kg',):
        return qty * 1000.0, ''
    if unit in ('oz', 'ounce', 'ounces'):
        return qty * 28.35, ''
    if unit in ('lb', 'pound', 'pounds'):
        return qty * 453.6, ''
    if unit == 'ml':
        return qty * 1.0, ''
    if unit == 'l':
        return qty * 1000.0, ''

    # food-specific density for cups (best signal)
    if unit == 'cup':
        d = FOOD_DENSITY.get(name) or FOOD_DENSITY.get(re.sub(r's$', '', name))
        if d:
            return qty * d, ''
        return qty * 240.0, f'default-cup (240g/cup)'
    if unit == 'tbsp':
        # oils ~14, water ~15, solids vary
        d = FOOD_DENSITY.get(name) or FOOD_DENSITY.get(re.sub(r's$', '', name))
        if d:
            # assume 1/16 cup
            return qty * (d / 16), ''
        return qty * 15.0, ''
    if unit == 'tsp':
        d = FOOD_DENSITY.get(name) or FOOD_DENSITY.get(re.sub(r's$', '', name))
        if d:
            return qty * (d / 48), ''
        return qty * 5.0, ''

    # count-based (bare number or "cloves" etc.)
    if unit == '' or unit in GENERIC_DENSITY:
        per = PER_ITEM.get(name) or PER_ITEM.get(re.sub(r's$', '', name))
        if per:
            return qty * per, ''
        if unit in GENERIC_DENSITY:
            return qty * GENERIC_DENSITY[unit], f'generic-{unit}'
        # bare count no match: guess 50g
        return qty * 50.0, 'unknown-count (guess 50g)'

    return qty * 50.0, f'unknown unit {unit!r}'


# ---- recipe extraction from index.html ------------------------------------

HTML_PATH = ROOT / 'index.html'

def load_recipes() -> list[dict]:
    """Parse DEFAULT_RECIPES from index.html. Returns list of dicts."""
    import subprocess
    js = r'''
    const fs = require('fs');
    const html = fs.readFileSync(process.argv[1], 'utf8');
    const start = html.indexOf('const DEFAULT_RECIPES = [') + 'const DEFAULT_RECIPES = '.length;
    const end = html.indexOf('];\n\nconst SHOP_DATA') + 1;
    const recipes = eval(html.slice(start, end));
    // strip photo base64 to keep output small; we don't need it
    recipes.forEach(r => { if (r.photo) r.photo = '[stripped]'; });
    process.stdout.write(JSON.stringify(recipes));
    '''
    r = subprocess.run(['node', '-e', js, str(HTML_PATH)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


# ---- computation -----------------------------------------------------------

def canonical_ingredient(amount: str, name: str) -> tuple[str, str]:
    """Given the (amount, name) tuple, return (canonical_search_name, full_text)."""
    return normalize_ingredient_name(name), f'{amount} {name}'


def compute_recipe_macros(recipe: dict, ing_db: dict) -> tuple[dict, list[str]]:
    """Return (macros_per_serving, warnings)."""
    warnings = []
    totals = {'cal': 0.0, 'prot': 0.0, 'fat': 0.0, 'carb': 0.0}
    servings = recipe.get('servings') or 1

    # Old format: ings: [[amount, name], ...]
    # New format: ingredients: ['2 tbsp olive oil', ...]
    ings = recipe.get('ings')
    if ings:
        pairs = [(a, n) for a, n in ings]
    else:
        pairs = []
        for s in recipe.get('ingredients', []):
            # split into leading amount+unit vs rest (heuristic)
            m = re.match(r'^([\d./\- ]+(?:g|kg|ml|l|oz|lb|cup|cups|tbsp|tsp|tbs|teaspoon|tablespoon|can|cans|clove|cloves|scoop|scoops|slice|slices|stalk|stalks|piece|pieces|block|blocks|fillet|fillets)?(?:\s+\([^)]*\))?)\s+(.+)$',
                         s, re.IGNORECASE)
            if m:
                pairs.append((m.group(1).strip(), m.group(2).strip()))
            else:
                pairs.append(('1', s))  # fallback

    for amount, name in pairs:
        full = f'{amount} {name}'.lower()
        if any(k in full for k in SKIP_KEYWORDS):
            continue
        qty, unit = parse_amount_with_unit(amount)
        canonical = normalize_ingredient_name(name)
        if not canonical:
            continue
        food = ing_db.get(canonical)
        if not food:
            warnings.append(f'no FDC match for "{canonical}"')
            continue
        grams, note = grams_for(qty, unit, canonical, full)
        if note:
            warnings.append(f'{canonical}: {note}')
        m = macros_per_100g(food)
        if not all(k in m for k in totals):
            warnings.append(f'{canonical}: incomplete FDC macros')
            continue
        for k in totals:
            totals[k] += (grams / 100.0) * m[k]

    return {k: round(v / servings) for k, v in totals.items()}, warnings


# ---- ingredient DB build ---------------------------------------------------

# Manual FDC id overrides — only IDs I've verified against the live API.
# For everything else, rely on SEARCH_REMAP queries + search fallback.
FDC_OVERRIDES = {
    'olive oil': 171413,
    'extra virgin olive oil': 171413,
    'truffle olive oil': 171413,
    'black truffle olive oil': 171413,
    'truffle oil': 171413,
    'red pepper flakes': 170932,       # Spices, pepper, red or cayenne
    'chili flakes': 170932,
    'crushed red pepper': 170932,
    'peanut butter': 172470,           # Peanut butter, smooth, no salt
    'lemon juice': 167747,             # Lemon juice, raw
    'parmesan': 171247,                # Cheese, parmesan, grated
    'parmigiano': 171247,
    'parmigiano reggiano': 171247,
    'protein pasta': 168915,           # Pasta, whole grain, dry
    'whole wheat pasta': 168915,
    'rice cake': 170250,               # Snacks, rice cakes, brown rice, plain, unsalted
    'rice cakes': 170250,
    'parsley': 170416,                 # Parsley, fresh
    'fresh parsley': 170416,
    'cinnamon': 171320,                # Spices, cinnamon, ground
    'ground cinnamon': 171320,
    'basil': 172232,                   # (may 404 — fallback will search)
    'fresh basil': 172232,
    'fresh basil leaves': 172232,
    'basil leaves': 172232,
    'balsamic vinegar': 172241,        # Vinegar, balsamic
    'aged balsamic vinegar': 172241,
    'truffle balsamic': 172241,
    'pecorino romano': 171249,         # Cheese, romano
    'thyme': 173470,                   # Thyme, fresh
    'fresh thyme': 173470,
    'dried thyme': 170938,
    'oat milk': 2257046,
    'rolled oats': 2346396,            # Oats, whole grain, rolled, old fashioned
    'oats': 2346396,
    'coconut milk': 170173,            # coconut milk canned
    'protein powder': 173180,          # Beverages, Protein powder whey based
    'vanilla protein powder': 173180,
    'whey protein': 173180,
    'evoo': 171413,                    # olive oil
    'cherry tomato': 170457,
    'mixed cherry tomatoes': 170457,
    'cherry tomatoes': 170457,
    'cherry tomato medley': 170457,
    'farro': 2710828,                  # Farro, pearled, dry, raw
    'cheese': 328637,                  # Cheese, cheddar (generic default)
    'cheddar': 328637,
    'corn': 169214,                    # Corn, sweet, yellow, canned, whole kernel
}

# Manual search query remaps for better FDC hits
SEARCH_REMAP = {
    'eggs': 'egg, whole, raw',
    'large eggs': 'egg, whole, raw',
    'egg': 'egg, whole, raw',
    'chicken': 'chicken, broiler, thigh, meat only, raw',
    'chicken thighs': 'chicken, broiler, thigh, meat only, raw',
    'chicken breast': 'chicken, broiler, breast, meat only, raw',
    'chicken breasts': 'chicken, broiler, breast, meat only, raw',
    'grilled chicken': 'chicken, broiler, breast, meat only, cooked',
    'ground pork': 'pork, ground, raw',
    'ground beef': 'beef, ground, 85% lean, raw',
    'flank steak': 'beef, flank, raw',
    'beef': 'beef, ground, 85% lean, raw',
    'salmon': 'salmon, atlantic, raw',
    'salmon fillets': 'salmon, atlantic, raw',
    'salmon fillet': 'salmon, atlantic, raw',
    'tofu': 'tofu, extra firm, raw',
    'extra firm tofu': 'tofu, extra firm, raw',
    'firm tofu': 'tofu, firm, raw',
    'lentils': 'lentils, dry',
    'chickpeas': 'chickpeas, canned, drained',
    'black beans': 'beans, black, canned',
    'edamame': 'edamame, frozen, prepared',
    'frozen peas': 'peas, green, frozen',
    'peas': 'peas, green, cooked',
    'broccoli': 'broccoli, raw',
    'broccoli florets': 'broccoli, raw',
    'spinach': 'spinach, raw',
    'baby spinach': 'spinach, raw',
    'kale': 'kale, raw',
    'arugula': 'arugula, raw',
    'spring mix': 'lettuce, mixed greens',
    'lettuce': 'lettuce, romaine, raw',
    'shredded lettuce': 'lettuce, iceberg, raw',
    'cherry tomato': 'tomato, red, raw',
    'cherry tomatoes': 'tomato, red, raw',
    'cherry tomato medley': 'tomato, red, raw',
    'tomato': 'tomato, red, raw',
    'tomatoes': 'tomato, red, raw',
    'diced tomatoes': 'tomatoes, canned',
    'crushed tomatoes': 'tomatoes, canned',
    'cucumber': 'cucumber, with peel, raw',
    'cucumbers': 'cucumber, with peel, raw',
    'onion': 'onion, raw',
    'red onion': 'onion, red, raw',
    'bell pepper': 'peppers, sweet, red, raw',
    'red bell pepper': 'peppers, sweet, red, raw',
    'avocado': 'avocado, raw',
    'banana': 'banana, raw',
    'frozen banana': 'banana, raw',
    'frozen mango': 'mango, raw',
    'mango': 'mango, raw',
    'berries': 'blueberries, raw',
    'mushrooms': 'mushrooms, white, raw',
    'mushroom': 'mushrooms, white, raw',
    'zucchini': 'zucchini, raw',
    'beet': 'beets, raw',
    'corn': 'corn, sweet, canned',
    'garlic': 'garlic, raw',
    'ginger': 'ginger root, raw',
    'fresh ginger': 'ginger root, raw',
    'fresh parsley': 'parsley, raw',
    'parsley': 'parsley, raw',
    'fresh basil': 'basil, fresh',
    'basil': 'basil, fresh',
    'cilantro': 'coriander leaves, raw',
    'fresh dill': 'dill weed, fresh',
    'dill': 'dill weed, fresh',
    'fresh thyme': 'thyme, fresh',
    'thyme': 'thyme, fresh',
    'rolled oats': 'oats, rolled, raw',
    'oats': 'oats, rolled, raw',
    'rice': 'rice, white, long-grain, raw',
    'white rice': 'rice, white, long-grain, raw',
    'brown rice': 'rice, brown, long-grain, raw',
    'basmati rice': 'rice, white, long-grain, raw',
    'jasmine rice': 'rice, white, long-grain, raw',
    'quinoa': 'quinoa, uncooked',
    'farro': 'wheat, hard red winter',
    'pasta': 'pasta, dry, unenriched',
    'spaghetti': 'pasta, dry, unenriched',
    'penne': 'pasta, dry, unenriched',
    'fettuccine': 'pasta, dry, unenriched',
    'protein pasta': 'pasta, whole-wheat, dry',
    'sourdough bread': 'bread, white',
    'whole grain bread': 'bread, whole-wheat',
    'flour tortilla': 'tortilla, flour',
    'large flour tortilla': 'tortilla, flour',
    'rice cake': 'rice cake',
    'rice cakes': 'rice cake',
    'greek yogurt': 'yogurt, greek, plain, whole milk',
    'yogurt': 'yogurt, plain, whole milk',
    'cottage cheese': 'cheese, cottage, 2% milkfat',
    'parmesan': 'cheese, parmesan, grated',
    'parmigiano reggiano': 'cheese, parmesan, grated',
    'pecorino romano': 'cheese, romano',
    'feta': 'cheese, feta',
    'feta cheese': 'cheese, feta',
    'shredded cheese': 'cheese, cheddar',
    'mexican cheese blend': 'cheese, cheddar',
    'milk': 'milk, whole, 3.25%',
    'oat milk': 'oat milk, unsweetened',
    'almond milk': 'almond milk, unsweetened',
    'coconut milk': 'coconut milk, canned',
    'heavy cream': 'cream, heavy whipping',
    'olive oil': 'oil, olive',
    'sesame oil': 'oil, sesame',
    'vegetable oil': 'oil, canola',
    'coconut oil': 'oil, coconut',
    'butter': 'butter, salted',
    'soy sauce': 'soy sauce',
    'tamari': 'soy sauce',
    'oyster sauce': 'oyster sauce',
    'sriracha': 'sriracha sauce',
    'hot sauce': 'hot sauce',
    'fish sauce': 'fish sauce',
    'honey': 'honey',
    'maple syrup': 'syrups, maple',
    'mayo': 'mayonnaise',
    'mayonnaise': 'mayonnaise',
    'sriracha mayo': 'mayonnaise',
    'balsamic vinegar': 'vinegar, balsamic',
    'rice vinegar': 'vinegar, rice',
    'aged balsamic vinegar': 'vinegar, balsamic',
    'lemon': 'lemon, raw',
    'lemons': 'lemon, raw',
    'lime': 'lime, raw',
    'lemon juice': 'lemon juice, raw',
    'tahini': 'seeds, sesame butter, tahini',
    'peanut butter': 'peanut butter, smooth',
    'almond butter': 'almond butter',
    'chia seeds': 'seeds, chia, dried',
    'ground flaxseed': 'seeds, flaxseed',
    'sesame seeds': 'seeds, sesame, whole',
    'mini chocolate chips': 'candies, semisweet chocolate',
    'chocolate chips': 'candies, semisweet chocolate',
    'cumin': 'spices, cumin seed',
    'paprika': 'spices, paprika',
    'smoked paprika': 'spices, paprika',
    'red pepper flakes': 'spices, pepper, red',
    'chili flakes': 'spices, pepper, red',
    'chili powder': 'spices, chili powder',
    'curry powder': 'spices, curry powder',
    'turmeric': 'spices, turmeric',
    'garam masala': 'spices, curry powder',
    'salt': 'salt, table',
    'pepper': 'spices, pepper, black',
    'black pepper': 'spices, pepper, black',
    'hummus': 'hummus',
    'granola': 'granola',
    'bread': 'bread, white',
    'crusty bread': 'bread, white',
    'kalamata olives': 'olives, ripe, canned',
    'olives': 'olives, ripe, canned',
    'pickled ginger': 'ginger root, raw',
    'vegetable broth': 'soup, vegetable broth',
    'chicken broth': 'soup, chicken broth',
    'protein powder': 'formulated bar, power bar, high protein',  # approx
    'vanilla protein powder': 'formulated bar, power bar, high protein',
    'vanilla extract': 'vanilla extract',
    'dijon mustard': 'mustard, prepared, yellow',
    'miso paste': 'miso',
    'capers': 'capers, canned',
    'rice cake': 'rice cake',
    'fresh basil leaves': 'basil, fresh',
    'cooked rice': 'rice, white, long-grain, cooked',
    'everything bagel seasoning': 'spices, sesame seeds',  # approximate
    'truffle balsamic': 'vinegar, balsamic',
    'black truffle olive oil': 'oil, olive',
    'truffle oil': 'oil, olive',
    'potatoes': 'potatoes, raw',
    'potato': 'potatoes, raw',
    'regular potatoes': 'potatoes, raw',
    'garlic powder': 'spices, garlic powder',
    'oregano': 'spices, oregano, dried',
    'dried oregano': 'spices, oregano, dried',
    'red wine vinegar': 'vinegar, red wine',
    'crusty': 'bread, white',
}


def resolve_food(name: str) -> dict | None:
    # direct FDC override (ID may be stale — fall back to search on 404)
    if name in FDC_OVERRIDES:
        try:
            food = fdc_food(FDC_OVERRIDES[name])
            if all(k in macros_per_100g(food) for k in ('cal', 'prot', 'fat', 'carb')):
                return food
        except HTTPError as e:
            if e.code != 404:
                raise
            # fall through to search
    query = SEARCH_REMAP.get(name, name)
    foods = fdc_search(query)
    best = pick_best(foods, query)
    if best and 'foodNutrients' in best and all(
        k in macros_per_100g(best) for k in ('cal', 'prot', 'fat', 'carb')
    ):
        return best
    # fallback: fetch full food detail for the top hit (search sometimes omits nutrients)
    if foods:
        return fdc_food(foods[0]['fdcId'])
    return None


# ---- main ------------------------------------------------------------------

# Servings overrides for recipes missing the field (proposed 2026-04-23)
SERVINGS_OVERRIDES = {
    'pork': 4, 'mexican': 4,
    'chicken': 2, 'tofu': 2, 'lentils': 2, 'stirfry': 2, 'shakshuka': 2,
    'guac': 2, 'fries': 2, 'pasta': 2, 'truffle-salad': 2, 'arugula_salad': 2,
    'oats': 1, 'eggs': 1, 'yogurt': 1, 'cacio': 1, 'roasted-veg': 1,
    'miso': 1, 'pb': 1, 'hummus-plate': 1, 'truffle-bread': 1,
}


def apply_patches(patches: dict, path: Path) -> None:
    """Patch index.html: inject servings + update macros for each recipe id."""
    html = path.read_text()
    n_servings = 0
    n_macros = 0
    for rid, patch in patches.items():
        anchor = f"{{id:'{rid}',"
        start = html.find(anchor)
        if start == -1:
            print(f'  SKIP {rid}: anchor not found', file=sys.stderr)
            continue
        # end = next top-level "{id:'" or the array close "];"
        rest = html[start + len(anchor):]
        m = re.search(r"(?:\n\s*|,\s*)\{id:'", rest)
        close = rest.find('\n];')
        if m and (close == -1 or m.start() < close):
            end = start + len(anchor) + m.start()
        elif close != -1:
            end = start + len(anchor) + close
        else:
            end = start + len(anchor) + len(rest)
        block = html[start:end]
        new_block = block
        # 1) update macros
        nm = patch.get('macros')
        if nm:
            updated = re.sub(
                r'macros:\{cal:\d+,\s*prot:\d+,\s*carb:\d+,\s*fat:\d+\}',
                f'macros:{{cal:{nm["cal"]},prot:{nm["prot"]},carb:{nm["carb"]},fat:{nm["fat"]}}}',
                new_block,
                count=1,
            )
            if updated != new_block:
                n_macros += 1
                new_block = updated
        # 2) insert servings if missing
        srv = patch.get('servings')
        if srv and 'servings:' not in new_block:
            updated = re.sub(
                r'(\n?\s*)macros:',
                rf'\1servings:{srv},\1macros:',
                new_block,
                count=1,
            )
            if updated != new_block:
                n_servings += 1
                new_block = updated
        html = html[:start] + new_block + html[end:]
    path.write_text(html)
    print(f'Patched: {n_macros} macros, {n_servings} servings inserted.', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', nargs='+', help='recipe ids to test')
    ap.add_argument('--all', action='store_true', help='process all recipes')
    ap.add_argument('--apply', action='store_true', help='patch index.html with new servings + macros')
    ap.add_argument('--dry-run', action='store_true', help='do not patch index.html')
    args = ap.parse_args()

    recipes = load_recipes()
    print(f'Loaded {len(recipes)} recipes', file=sys.stderr)

    # inject SERVINGS_OVERRIDES in-memory so compute divides correctly
    for r in recipes:
        if r['id'] in SERVINGS_OVERRIDES:
            r['servings'] = SERVINGS_OVERRIDES[r['id']]

    if args.test:
        recipes = [r for r in recipes if r['id'] in args.test]
        if not recipes:
            sys.exit(f'no matching ids: {args.test}')

    # 1) collect unique ingredient names
    unique = {}
    for r in recipes:
        pairs = r.get('ings') or []
        if not pairs:
            for s in r.get('ingredients', []):
                m = re.match(r'^([\d./\- ]+(?:\s*(?:g|kg|ml|l|oz|lb|cup|cups|tbsp|tsp|tbs|can|cans|clove|cloves|scoop|scoops|slice|slices|stalk|stalks|piece|pieces|block|blocks|fillet|fillets))?(?:\s+\([^)]*\))?)\s+(.+)$',
                             s, re.IGNORECASE)
                if m:
                    pairs.append((m.group(1).strip(), m.group(2).strip()))
                else:
                    pairs.append(('1', s))
        for amt, name in pairs:
            canonical = normalize_ingredient_name(name)
            if canonical and canonical not in unique:
                unique[canonical] = None

    print(f'Unique ingredient names: {len(unique)}', file=sys.stderr)

    # 2) resolve each via FDC (cached)
    for i, name in enumerate(list(unique.keys()), 1):
        try:
            unique[name] = resolve_food(name)
        except Exception as e:
            print(f'  [{i}/{len(unique)}] {name}: ERROR {e}', file=sys.stderr)
            continue
        tag = unique[name].get('description', '(none)') if unique[name] else 'NO MATCH'
        print(f'  [{i}/{len(unique)}] {name} → {tag[:70]}', file=sys.stderr)

    # 3) compute per recipe
    report = []
    for r in recipes:
        new_macros, warnings = compute_recipe_macros(r, unique)
        old = r.get('macros', {})
        report.append({
            'id': r['id'],
            'name': r['name'],
            'servings': r.get('servings') or 1,
            'old': old,
            'new': new_macros,
            'delta_pct': {
                k: round(100 * ((new_macros.get(k, 0) - old.get(k, 0)) / max(old.get(k, 0), 1)))
                for k in ('cal', 'prot', 'fat', 'carb')
            },
            'warnings': warnings,
        })

    out_path = OUT / 'macros_report.json'
    out_path.write_text(json.dumps(report, indent=2))
    print(f'\nReport written to {out_path}', file=sys.stderr)

    if args.apply and not args.dry_run:
        patches = {}
        for item in report:
            patches[item['id']] = {'macros': item['new']}
            if item['id'] in SERVINGS_OVERRIDES:
                patches[item['id']]['servings'] = SERVINGS_OVERRIDES[item['id']]
        apply_patches(patches, HTML_PATH)


if __name__ == '__main__':
    main()
