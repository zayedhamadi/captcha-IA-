from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from captcha_service import (
    generate_captcha,
    verify_captcha,
    init_odd_pool,
    start_background_refresh,
)




@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────
    print("[APP] Initialisation du pool CAPTCHA odd-one-out…")
    init_odd_pool()            
    start_background_refresh()
    print("[APP] Prêt.")
    yield
    # ── Shutdown ─────────────────────────────────────────
    print("[APP] Arrêt.")




app = FastAPI(
    title="CAPTCHA Service — SkillExchange",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════
#  Schémas
# ═══════════════════════════════════════════════════════════

class CaptchaVerifyRequest(BaseModel):
    token: str
    answer: str




@app.get("/captcha/generate", summary="Génère un nouveau CAPTCHA")
async def get_captcha():

    try:
        return generate_captcha()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur génération CAPTCHA : {e}")


@app.post("/captcha/verify", summary="Vérifie la réponse au CAPTCHA")
async def check_captcha(request: CaptchaVerifyRequest):
    """
    À appeler AVANT le POST /auth/login côté Angular.
    Retourne 200 si la réponse est correcte, 400 sinon.
    """
    if verify_captcha(request.token, request.answer):
        return {"valid": True, "message": "CAPTCHA validé ✓"}
    raise HTTPException(
        status_code=400,
        detail="Réponse incorrecte ou CAPTCHA expiré",
    )