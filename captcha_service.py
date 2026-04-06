import json
import random
import uuid
import base64
import threading
import math
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

# ═══════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════

captcha_store: dict[str, dict] = {}
CAPTCHA_TTL_SECONDS = 120

# Pool odd-one-out
_ODD_POOL: list[dict] = []
_ODD_POOL_LOCK = threading.Lock()
CACHE_FILE        = Path("odd_one_out_cache.json")
EMERGENCY_FILE    = Path("odd_one_out_emergency.json")  # fallbacks dynamiques
MIN_POOL_SIZE     = 50
EMERGENCY_SIZE    = 10   # questions générées une fois pour les urgences
BATCH_SIZE        = 20
REFRESH_INTERVAL_SECONDS = 3600
LOW_WATER_MARK    = 20


# ═══════════════════════════════════════════════════════════
#  Helpers généraux
# ═══════════════════════════════════════════════════════════

def _cleanup_expired():
    now = datetime.utcnow()
    expired = [t for t, v in captcha_store.items() if v["expires_at"] < now]
    for t in expired:
        del captcha_store[t]


def _store(token: str, answer: str, captcha_type: str = ""):
    captcha_store[token] = {
        "answer": answer,
        "type": captcha_type,
        "expires_at": datetime.utcnow() + timedelta(seconds=CAPTCHA_TTL_SECONDS),
    }


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "C:/Windows/Fonts/verdana.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/consola.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════
#  Pool odd-one-out  —  génération IA + cache JSON
# ═══════════════════════════════════════════════════════════

def _load_cache() -> list[dict]:
    """Charge le cache JSON local. Retourne [] si absent ou corrompu."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_cache(pool: list[dict]):
    """Persiste le pool sur disque."""
    try:
        CACHE_FILE.write_text(
            json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[CAPTCHA] Impossible de sauvegarder le cache : {e}")


def _load_emergency() -> list[dict]:
    """Charge les fallbacks d'urgence générés dynamiquement."""
    if EMERGENCY_FILE.exists():
        try:
            data = json.loads(EMERGENCY_FILE.read_text(encoding="utf-8"))
            if data:
                return data
        except Exception:
            pass
    return []


def _save_emergency(pool: list[dict]):
    try:
        EMERGENCY_FILE.write_text(
            json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[CAPTCHA] Impossible de sauvegarder emergency : {e}")


def _ensure_emergency_pool() -> list[dict]:
    """
    Garantit l'existence d'un fichier de fallbacks d'urgence entièrement
    générés par Claude. Appelé une seule fois au démarrage.
    Si Claude échoue, le fichier reste vide → generate_odd_one_out_captcha
    lèvera une HTTPException plutôt que de servir du contenu hardcodé.
    """
    pool = _load_emergency()
    if len(pool) >= EMERGENCY_SIZE:
        print(f"[CAPTCHA] Emergency pool OK ({len(pool)} items)")
        return pool

    print(f"[CAPTCHA] Génération du pool d'urgence ({EMERGENCY_SIZE} questions)…")
    try:
        pool = _generate_batch_via_claude(EMERGENCY_SIZE)
        _save_emergency(pool)
        print(f"[CAPTCHA] Emergency pool créé : {len(pool)} questions")
    except Exception as e:
        print(f"[CAPTCHA] Impossible de créer l'emergency pool : {e}")
        pool = []
    return pool



def _generate_batch_via_claude(n: int = BATCH_SIZE) -> list[dict]:
    """
    Demande à Claude de générer n questions d'un coup.
    Lève une exception si la réponse est invalide.
    """
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        f"Génère exactement {n} questions CAPTCHA 'quel mot est l'intrus ?' en français.\n"
        "Réponds UNIQUEMENT avec un tableau JSON valide, sans Markdown ni backtick.\n"
        "Format : "
        '[{"words":["mot1","mot2","mot3","mot4","mot5"],"odd":"mot_intrus","category":"catégorie"}, ...]\n'
        "Règles STRICTES :\n"
        "- 5 mots par question, 4 de la même catégorie, 1 intrus clairement différent\n"
        "- Catégories variées : fruits, légumes, couleurs, animaux, métiers, pays, sports, "
        "instruments, planètes, transports, fleurs, matières scolaires, meubles, boissons, vêtements\n"
        "- Aucune répétition entre les questions\n"
        "- L'intrus doit être évident (non ambigu)\n"
        "- Mélange l'ordre des mots (l'intrus ne doit pas toujours être au même index)"
    )
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    items = json.loads(raw)
    # Validation minimale
    for item in items:
        assert "words" in item and "odd" in item and "category" in item
        assert isinstance(item["words"], list) and len(item["words"]) == 5
        assert item["odd"] in item["words"]
    return items


def _fill_pool(pool: list[dict], target: int = MIN_POOL_SIZE) -> list[dict]:
    """
    Complète `pool` jusqu'à `target` questions en appelant Claude
    par batchs. Déduplique par valeur de `odd` (insensible à la casse).
    """
    existing_odds = {item["odd"].lower() for item in pool}
    needed = target - len(pool)

    while needed > 0:
        batch_n = min(needed, BATCH_SIZE)
        try:
            new_items = _generate_batch_via_claude(batch_n)
            added = 0
            for item in new_items:
                key = item["odd"].lower()
                if key not in existing_odds:
                    pool.append(item)
                    existing_odds.add(key)
                    added += 1
            print(f"[CAPTCHA] +{added} questions ajoutées au pool (total={len(pool)})")
            needed -= batch_n
        except Exception as e:
            print(f"[CAPTCHA] Génération IA échouée : {e}")
            break

    return pool


def init_odd_pool():
    """
    À appeler UNE FOIS au démarrage de l'application.
    1. Génère/charge le pool d'urgence (odd_one_out_emergency.json)
    2. Génère/charge le pool principal (odd_one_out_cache.json)
    Les deux fichiers sont 100 % dynamiques — aucune donnée hardcodée.
    """
    global _ODD_POOL

    # ── Pool d'urgence (généré une seule fois, persiste sur disque) ──
    _ensure_emergency_pool()

    # ── Pool principal ───────────────────────────────────────────────
    pool = _load_cache()
    if len(pool) < MIN_POOL_SIZE:
        print(f"[CAPTCHA] Pool insuffisant ({len(pool)} items), génération en cours…")
        pool = _fill_pool(pool, target=MIN_POOL_SIZE)
        _save_cache(pool)
    else:
        print(f"[CAPTCHA] Pool chargé depuis cache : {len(pool)} questions")
    with _ODD_POOL_LOCK:
        _ODD_POOL = pool


def _refresh_pool_background():
    """
    Thread daemon : toutes les REFRESH_INTERVAL_SECONDS,
    si le pool descend sous LOW_WATER_MARK, régénère.
    """
    import time

    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        with _ODD_POOL_LOCK:
            current_size = len(_ODD_POOL)

        if current_size < LOW_WATER_MARK:
            print(f"[CAPTCHA] Pool bas ({current_size}), rechargement…")
            with _ODD_POOL_LOCK:
                pool = list(_ODD_POOL)
            pool = _fill_pool(pool, target=MIN_POOL_SIZE)
            _save_cache(pool)
            with _ODD_POOL_LOCK:
                _ODD_POOL[:] = pool


def start_background_refresh():
    """Lance le thread de surveillance du pool en arrière-plan."""
    t = threading.Thread(target=_refresh_pool_background, daemon=True)
    t.start()
    print("[CAPTCHA] Thread de refresh démarré")


# ═══════════════════════════════════════════════════════════
#  1. CAPTCHA Mathématique
# ═══════════════════════════════════════════════════════════

def generate_math_captcha() -> dict:
    _cleanup_expired()
    a = random.randint(1, 12)
    b = random.randint(1, 12)
    op = random.choice(["+", "-", "×"])
    if op == "-" and b > a:
        a, b = b, a
    real_op = "*" if op == "×" else op
    answer = str(eval(f"{a}{real_op}{b}"))
    question = f"Combien fait {a} {op} {b} ?"
    token = str(uuid.uuid4())
    _store(token, answer, "math")
    return {"token": token, "type": "math", "question": question}


# ═══════════════════════════════════════════════════════════
#  2. CAPTCHA Image (cercles)
# ═══════════════════════════════════════════════════════════

def generate_image_captcha() -> dict:
    _cleanup_expired()
    count = random.randint(2, 7)
    img = Image.new("RGB", (320, 130), color=(245, 247, 250))
    draw = ImageDraw.Draw(img)

    for _ in range(300):
        x, y = random.randint(0, 319), random.randint(0, 129)
        draw.point((x, y), fill=(
            random.randint(200, 230),
            random.randint(200, 230),
            random.randint(200, 230),
        ))

    placed, drawn = [], 0
    attempts = 0
    while drawn < count and attempts < 100:
        attempts += 1
        x, y = random.randint(25, 270), random.randint(20, 100)
        if any(abs(x - px) < 35 and abs(y - py) < 35 for px, py in placed):
            continue
        placed.append((x, y))
        r, g, b_ch = random.randint(60, 200), random.randint(60, 180), random.randint(100, 220)
        draw.ellipse([x - 15, y - 15, x + 15, y + 15],
                     fill=(r, g, b_ch), outline=(50, 50, 80), width=2)
        drawn += 1

    for _ in range(4):
        draw.line(
            [random.randint(0, 320), random.randint(0, 130),
             random.randint(0, 320), random.randint(0, 130)],
            fill=(180, 190, 200), width=1,
        )

    buf = BytesIO()
    img.save(buf, format="PNG")
    token = str(uuid.uuid4())
    _store(token, str(drawn), "image")
    return {
        "token": token,
        "type": "image",
        "question": "Combien de cercles voyez-vous dans l'image ?",
        "image": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}",
    }


# ═══════════════════════════════════════════════════════════
#  3. CAPTCHA Ordre croissant
# ═══════════════════════════════════════════════════════════

def generate_order_captcha() -> dict:
    _cleanup_expired()
    nums = random.sample(range(1, 30), 5)
    sorted_nums = sorted(nums)
    answer = ",".join(str(n) for n in sorted_nums)
    token = str(uuid.uuid4())
    _store(token, answer, "order")
    return {
        "token": token,
        "type": "order",
        "question": "Cliquez les chiffres du plus petit au plus grand",
        "data": nums,
    }


# ═══════════════════════════════════════════════════════════
#  4. CAPTCHA Texte déformé
# ═══════════════════════════════════════════════════════════

def _wave_distort(img: Image.Image, amplitude: int = 4, frequency: float = 0.06) -> Image.Image:
    width, height = img.size
    src = img.load()
    out = Image.new("RGB", (width, height), (248, 249, 252))
    dst = out.load()
    for x in range(width):
        for y in range(height):
            dy = int(amplitude * math.sin(2 * math.pi * frequency * x))
            dx = int((amplitude // 2) * math.sin(2 * math.pi * frequency * y + 1))
            sx = min(max(x + dx, 0), width - 1)
            sy = min(max(y + dy, 0), height - 1)
            dst[x, y] = src[sx, sy]
    return out


def generate_text_captcha() -> dict:
    _cleanup_expired()
    CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    length = random.randint(5, 6)
    code = "".join(random.choices(CHARS, k=length))

    W, H = 360, 100
    img = Image.new("RGB", (W, H), (248, 249, 252))
    draw = ImageDraw.Draw(img)

    for _ in range(400):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        c = random.randint(190, 225)
        draw.point((x, y), fill=(c, c, c))

    for _ in range(5):
        draw.line(
            [random.randint(0, W), random.randint(0, H),
             random.randint(0, W), random.randint(0, H)],
            fill=(random.randint(160, 200), random.randint(160, 200), random.randint(200, 220)),
            width=1,
        )

    font = _load_font(48)
    char_w = 52
    total_w = char_w * length
    x_start = max(10, (W - total_w) // 2)
    x_offset = x_start

    for ch in code:
        palette = [
            (random.randint(20, 80),  random.randint(20, 60),  random.randint(120, 180)),
            (random.randint(20, 70),  random.randint(100, 150), random.randint(20, 60)),
            (random.randint(140, 180), random.randint(20, 60),  random.randint(20, 60)),
            (random.randint(20, 60),  random.randint(20, 60),  random.randint(20, 60)),
        ]
        color = random.choice(palette)
        ch_canvas = Image.new("RGBA", (60, 72), (0, 0, 0, 0))
        ch_draw = ImageDraw.Draw(ch_canvas)
        ch_draw.text((6, 4), ch, font=font, fill=(*color, 255))
        angle = random.uniform(-15, 15)
        ch_rot = ch_canvas.rotate(angle, expand=True, resample=Image.BICUBIC)
        y_pos = random.randint(6, 20)
        img.paste(ch_rot, (x_offset, y_pos), ch_rot)
        x_offset += char_w

    img = _wave_distort(img, amplitude=random.randint(3, 5))
    buf = BytesIO()
    img.save(buf, format="PNG")
    token = str(uuid.uuid4())
    _store(token, code.upper(), "text")
    return {
        "token": token,
        "type": "text",
        "question": "Saisissez les caractères affichés (majuscules)",
        "image": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}",
    }


# ═══════════════════════════════════════════════════════════
#  5. CAPTCHA Odd-one-out  —  pioche dans le pool IA
# ═══════════════════════════════════════════════════════════

# Aucun fallback statique — tout est généré par Claude


def generate_odd_one_out_captcha() -> dict:
    _cleanup_expired()

    # 1. Piocher dans le pool principal (thread-safe)
    with _ODD_POOL_LOCK:
        pool_snapshot = list(_ODD_POOL)

    if pool_snapshot:
        item = random.choice(pool_snapshot)
    else:
        # 2. Pool vide → tentative Claude à la volée
        try:
            items = _generate_batch_via_claude(1)
            item = items[0]
            # Réalimenter le pool en arrière-plan
            threading.Thread(target=_async_refill, daemon=True).start()
        except Exception:
            # 3. Dernier recours : pool d'urgence dynamique (odd_one_out_emergency.json)
            emergency = _load_emergency()
            if not emergency:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=503,
                    detail="Service CAPTCHA temporairement indisponible. Réessayez dans quelques secondes.",
                )
            item = random.choice(emergency)

    words = item["words"].copy()
    odd = item["odd"]
    category = item.get("category", "")

    random.shuffle(words)
    token = str(uuid.uuid4())
    _store(token, odd, "odd")
    return {
        "token": token,
        "type": "odd",
        "question": f"Quel mot n'appartient pas à la catégorie « {category} » ?",
        "data": words,
    }


def _async_refill():
    """Réalimente le pool en arrière-plan sans bloquer la requête."""
    try:
        with _ODD_POOL_LOCK:
            pool = list(_ODD_POOL)
        pool = _fill_pool(pool, target=MIN_POOL_SIZE)
        _save_cache(pool)
        with _ODD_POOL_LOCK:
            _ODD_POOL[:] = pool
        print(f"[CAPTCHA] Refill asynchrone terminé : {len(_ODD_POOL)} questions")
    except Exception as e:
        print(f"[CAPTCHA] Refill asynchrone échoué : {e}")


# ═══════════════════════════════════════════════════════════
#  Entrée publique
# ═══════════════════════════════════════════════════════════

def generate_captcha() -> dict:
    choice = random.random()
    if choice < 0.20:
        return generate_math_captcha()
    elif choice < 0.40:
        return generate_image_captcha()
    elif choice < 0.60:
        return generate_order_captcha()
    elif choice < 0.80:
        return generate_text_captcha()
    else:
        return generate_odd_one_out_captcha()


def verify_captcha(token: str, answer: str) -> bool:
    entry = captcha_store.pop(token, None)
    if entry is None:
        return False
    if datetime.utcnow() > entry["expires_at"]:
        return False

    expected = entry["answer"].strip()
    given = answer.strip()
    ctype = entry.get("type", "")

    if ctype == "order":
        def normalize_order(s: str) -> list[int]:
            s = s.strip().lstrip("[").rstrip("]")
            try:
                return [int(x.strip()) for x in s.split(",") if x.strip()]
            except ValueError:
                return []
        return normalize_order(given) == normalize_order(expected)

    return given.upper() == expected.upper()