"""
vocab_news_bot/bot.py  —  Spanish B2 News Podcast Bot (v2)

Flow:
  /news
    → Step 1: choose COUNTRY  (Argentina / Rusia / Turquía / China / USA)
    → Step 2: choose SUBTOPIC (Medicina / Psicología / Tango / Cultura / Economía / IA)
    → Bot sends 3 comprehension questions as text  ← BEFORE audio
    → Bot generates & sends MP3  (~5-6 min, Elena & Marcos, B2, very enthusiastic)
    → Bot sends 10 collocation buttons (Spanish / English stacked)
      → tap to save → Google Sheet, tab = Telegram username (auto-created)
    → Bot sends HTML file with colour-coded transcript
"""

import os
import re
import json
import time
import wave
import struct
import logging
import asyncio
import tempfile
import subprocess
from io import BytesIO
from datetime import datetime

import feedparser
import gspread
from openai import OpenAI
from google.oauth2.service_account import Credentials
from google.cloud import texttospeech

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

# ── Env vars ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY")

if not TELEGRAM_BOT_TOKEN or not DEEPSEEK_API_KEY:
    raise EnvironmentError("TELEGRAM_BOT_TOKEN and DEEPSEEK_API_KEY must be set.")

# ── Google ─────────────────────────────────────────────────────────────────
GOOGLE_CREDS_FILE = os.path.join(os.path.dirname(__file__), "google-creds.json")
SPREADSHEET_URL   = "https://docs.google.com/spreadsheets/d/1H-ezqh5Vcl3_6YWJIy9KvgpDCsM3V6N4LJq0dqNbJS0/edit"

# ── TTS voices ─────────────────────────────────────────────────────────────
FEMALE_VOICE = "es-US-Chirp3-HD-Aoede"
MALE_VOICE   = "es-US-Chirp3-HD-Puck"

# ── DeepSeek ───────────────────────────────────────────────────────────────
ds_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── In-memory state (per chat_id) ──────────────────────────────────────────
PENDING_COLLOCATIONS: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# MENU LABELS
# ══════════════════════════════════════════════════════════════════════════════

COUNTRY_LABELS = {
    "argentina": "🇦🇷 Argentina",
    "rusia":     "🇷🇺 Rusia",
    "turquia":   "🇹🇷 Turquía",
    "china":     "🇨🇳 China",
    "usa":       "🇺🇸 Estados Unidos",
}

SUBTOPIC_LABELS = {
    "medicina":   "🏥 Medicina",
    "psicologia": "🧠 Psicología",
    "tango":      "💃 Tango",
    "cultura":    "🎭 Cultura",
    "economia":   "📈 Economía",
    "ia":         "🤖 Inteligencia Artificial",
}


# ══════════════════════════════════════════════════════════════════════════════
# RSS FEEDS  (Al Jazeera · SCMP · Daily Sabah · AI News — no NATO-bias outlets)
# ══════════════════════════════════════════════════════════════════════════════

RSS_FEEDS = {
    "argentina": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "rusia": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "turquia": [
        "https://www.dailysabah.com/rss/world",
        "https://www.dailysabah.com/rss/economy",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "china": [
        "https://www.scmp.com/rss/2/feed",
        "https://www.scmp.com/rss/4/feed",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "usa": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "ia": [
        "https://www.artificialintelligence-news.com/feed/",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.scmp.com/rss/2/feed",
    ],
}

KEYWORD_MAP = {
    "argentina": ["argentina", "argentino", "buenos aires", "milei", "patagonia"],
    "rusia":     ["russia", "russian", "moscow", "kremlin", "putin", "ukraine"],
    "turquia":   ["turkey", "turkish", "erdogan", "ankara", "istanbul"],
    "china":     ["china", "chinese", "beijing", "xi ", "taiwan", "hong kong"],
    "usa":       ["trump", "united states", "congress", "washington", "american", "white house"],
    "medicina":  ["health", "medicine", "medical", "hospital", "disease", "treatment", "vaccine", "therapy"],
    "psicologia":["psychology", "mental health", "therapy", "wellbeing", "stress", "anxiety", "cognitive"],
    "tango":     ["tango", "dance", "milonga", "buenos aires", "folklore"],
    "cultura":   ["culture", "festival", "museum", "art", "literature", "music", "tradition", "heritage", "holiday"],
    "economia":  ["economy", "economic", "trade", "market", "gdp", "inflation", "investment", "finance"],
    "ia":        ["artificial intelligence", "ai model", "deepseek", "gemini", "chatgpt", "llm",
                  "openai", "anthropic", "mistral", "grok", "language model", "machine learning", "qwen"],
}


# ══════════════════════════════════════════════════════════════════════════════
# FETCH HEADLINES
# ══════════════════════════════════════════════════════════════════════════════

def fetch_headlines(country: str, subtopic: str, max_stories: int = 6) -> list:
    import time as _time
    cutoff   = _time.time() - 86400
    feed_key = "ia" if subtopic == "ia" else country
    urls     = RSS_FEEDS.get(feed_key, RSS_FEEDS["usa"])

    stories, seen = [], set()
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                pub    = entry.get("published_parsed")
                pub_ts = _time.mktime(pub) if pub else 0
                summary = re.sub(r"<[^>]+>", "",
                    entry.get("summary", entry.get("description", ""))).strip()[:300]
                stories.append({
                    "title":   title,
                    "summary": summary,
                    "fresh":   pub_ts >= cutoff,
                })
        except Exception as e:
            log.warning(f"RSS {url}: {e}")

    fresh = [s for s in stories if s["fresh"]]
    pool  = fresh if len(fresh) >= 2 else stories

    kws = KEYWORD_MAP.get(country, []) + KEYWORD_MAP.get(subtopic, [])
    if kws:
        filtered = [s for s in pool if any(
            k in s["title"].lower() or k in s["summary"].lower() for k in kws
        )]
        pool = filtered if filtered else pool

    return pool[:max_stories]


# ══════════════════════════════════════════════════════════════════════════════
# TOPIC INSTRUCTIONS
# ══════════════════════════════════════════════════════════════════════════════

COUNTRY_NAMES_ES = {
    "argentina": "Argentina",
    "rusia":     "Rusia",
    "turquia":   "Turquía",
    "china":     "China",
    "usa":       "Estados Unidos",
}


def get_topic_instruction(country: str, subtopic: str) -> str:
    cn = COUNTRY_NAMES_ES.get(country, country.capitalize())
    instructions = {
        "medicina": (
            f"Cubre las últimas noticias sobre medicina y salud en {cn}: "
            "avances médicos, políticas de salud pública, nuevos tratamientos o descubrimientos científicos."
        ),
        "psicologia": (
            f"Cubre temas de psicología y salud mental relacionados con {cn}: "
            "investigaciones recientes, tendencias sociales, bienestar o políticas de salud mental."
        ),
        "tango": (
            "Cubre noticias sobre el tango y su mundo: "
            "festivales, figuras destacadas, evolución del género, "
            "su influencia cultural en Argentina y en el mundo."
        ),
        "cultura": (
            f"Cubre noticias culturales de {cn}: festivales, museos, arte, literatura, música, "
            "tradiciones, celebraciones populares, fiestas nacionales o patrimonio cultural."
        ),
        "economia": (
            f"Cubre las noticias económicas más importantes de {cn}: "
            "mercados, comercio, política económica, empresas o situación financiera."
        ),
        "ia": (
            f"Cubre los últimos avances en inteligencia artificial relacionados con {cn}. "
            "SOLO implementación y capacidades: cómo el gobierno o empresas usan IA, "
            "startups de IA, nuevos modelos lanzados (incluyendo modelos chinos como DeepSeek, Qwen), "
            "nuevas funcionalidades, lo que ahora es posible que antes no lo era. "
            "NADA sobre inversiones, valoraciones bursátiles ni dinero."
        ),
    }
    return instructions.get(subtopic, f"Cubre las últimas noticias sobre {cn}.")


# ══════════════════════════════════════════════════════════════════════════════
# DEEPSEEK — GENERATE SCRIPT
# ══════════════════════════════════════════════════════════════════════════════

PODCAST_SYSTEM_PROMPT = """\
Eres un guionista de podcasts en español.
Escribes guiones para dos presentadores que llevan años trabajando juntos y tienen una química eléctrica e innegable:

- Elena — mujer, brillante, ingeniosa y con una energía absolutamente contagiosa. Le encanta sorprender a Marcos con datos inesperados. Tiene opiniones fuertes pero las expresa con humor. Coquetea sin darse cuenta... o quizás sí.
- Marcos — hombre, apasionado, carismático y muy divertido. Siempre tiene un comentario inesperado que hace reír. Le encanta provocar a Elena y admira su inteligencia aunque lo disimule mal. Coquetea descaradamente.

ENERGÍA Y ESTILO — ESTO ES LO MÁS IMPORTANTE:
- Los dos son INCREÍBLEMENTE entusiastas. Hablan con energía desbordante y genuina, como si cada noticia fuera fascinante.
- Usan exclamaciones naturales y frecuentes: "¡Espera, espera!", "¡Esto es alucinante!", "¡No me lo puedo creer!", "¡Exactamente!", "¡Qué interesante!", "¡Eso es clave!"
- Se interrumpen con emoción, reaccionan con sorpresa real, celebran los datos buenos.
- Dan su OPINIÓN sobre lo que cuentan — no de forma exagerada o política, sino reflexiva y personal:
  "A mí esto me parece un cambio enorme...", "Yo creo que lo más importante aquí es...", "¿Tú no crees que esto cambia todo?"
- Pueden estar de acuerdo o NO — el desacuerdo crea dinamismo real.
- La tensión coqueta es sutil pero constante: "Sabía que ibas a decir eso", "Eres imposible, Marcos", "Oye, eso ha sido muy bueno — te lo reconozco."
- El ritmo es VIVO. Frases cortas y medias. Nunca monólogos largos sin reacción del otro.

REGLAS DE IDIOMA:
- TODO en español, nivel B2. Fluido, natural, variado. Nada demasiado técnico sin explicar.
- Si algo es complejo, uno lo explica con palabras más simples de forma natural, sin que suene a clase.

REGLAS DE NOMBRES:
- Solo "Trump" y "Putin" por su nombre. Los demás: por su cargo.
- Países y empresas: directamente en español.

FORMATO DE SALIDA — solo JSON válido, sin markdown ni texto extra antes o después:
{
  "title": "título del episodio en español",
  "questions": [
    "Pregunta de comprensión 1 sobre el contenido",
    "Pregunta de comprensión 2",
    "Pregunta de comprensión 3"
  ],
  "turns": [
    {"speaker": "Elena", "text": "..."},
    {"speaker": "Marcos", "text": "..."}
  ],
  "collocations": [
    {"spanish": "expresión en español", "english": "simple English translation"},
    {"spanish": "...", "english": "..."}
  ]
}

PREGUNTAS: abiertas, que inviten a reflexionar sobre el contenido. En español. Nivel B2.

COLOCACIONES: exactamente 10. Expresiones o colocaciones clave del episodio, 2-6 palabras en español.
Traducción al inglés: usa la palabra MÁS SENCILLA posible — el oyente habla inglés bien pero no es nativo. Evita palabras C1/C2 en inglés.

Duración objetivo: ~35-42 turnos de diálogo (5-6 minutos de audio a ritmo natural).
"""


def generate_script(country: str, subtopic: str, headlines: list) -> dict:
    headline_text     = "\n".join(f"- {s['title']}: {s['summary']}" for s in headlines)
    topic_instruction = get_topic_instruction(country, subtopic)
    country_es        = COUNTRY_NAMES_ES.get(country, country)
    subtopic_es       = SUBTOPIC_LABELS.get(subtopic, subtopic).split(" ", 1)[-1]

    user_prompt = (
        f"País: {country_es} | Tema: {subtopic_es}\n"
        f"Instrucción: {topic_instruction}\n\n"
        f"Titulares de hoy (úsalos como fuente factual):\n{headline_text}\n\n"
        "Escribe el guion completo ahora."
    )

    log.info(f"[DeepSeek] {country}/{subtopic}")
    resp = ds_client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": PODCAST_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=5000,
    )

    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


# ══════════════════════════════════════════════════════════════════════════════
# HTML TRANSCRIPT
# ══════════════════════════════════════════════════════════════════════════════

def build_html_transcript(title: str, country: str, subtopic: str,
                          turns: list, collocations: list) -> bytes:
    country_es  = COUNTRY_NAMES_ES.get(country, country)
    subtopic_es = SUBTOPIC_LABELS.get(subtopic, subtopic).split(" ", 1)[-1]
    date_str    = datetime.now().strftime("%d %B %Y")

    turns_html = ""
    for turn in turns:
        css  = "elena" if turn["speaker"] == "Elena" else "marcos"
        turns_html += (
            f'<div class="turn {css}">'
            f'<span class="name">{turn["speaker"]}</span>'
            f'<p>{turn["text"]}</p>'
            f'</div>\n'
        )

    col_rows = "".join(
        f'<tr><td class="sp">{c.get("spanish","")}</td>'
        f'<td class="en">{c.get("english","")}</td></tr>\n'
        for c in collocations
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Georgia,serif;background:#f9f6f0;color:#2c2c2c;max-width:780px;margin:40px auto;padding:0 24px 60px;line-height:1.75}}
header{{text-align:center;padding:36px 0 28px;border-bottom:2px solid #d4a96a;margin-bottom:32px}}
header h1{{font-size:1.75em;color:#2c2c2c;margin-bottom:10px}}
.meta{{font-size:.9em;color:#888;font-style:italic}}
.badge{{display:inline-block;background:#d4a96a;color:#fff;border-radius:20px;padding:3px 14px;font-size:.82em;margin:0 4px;font-style:normal;font-family:sans-serif}}
.turn{{display:flex;gap:14px;margin-bottom:16px;padding:14px 18px;border-radius:10px}}
.turn.elena{{background:#fff8ee;border-left:4px solid #e8924a}}
.turn.marcos{{background:#f0f5ff;border-left:4px solid #5577cc}}
.name{{font-weight:700;font-family:sans-serif;font-size:.82em;min-width:62px;padding-top:4px;text-transform:uppercase;letter-spacing:.06em}}
.turn.elena .name{{color:#c46a20}}
.turn.marcos .name{{color:#3355aa}}
.turn p{{flex:1}}
h2{{font-size:1em;color:#666;margin:40px 0 14px;padding-bottom:6px;border-bottom:1px solid #ddd;font-family:sans-serif;text-transform:uppercase;letter-spacing:.1em}}
table{{width:100%;border-collapse:collapse;font-size:.95em}}
td{{padding:9px 14px;border-bottom:1px solid #eee}}
td.sp{{font-weight:700;color:#c46a20;width:50%}}
td.en{{color:#3355aa}}
tr:hover td{{background:#fdf3e7}}
footer{{text-align:center;margin-top:50px;font-size:.78em;color:#bbb;font-family:sans-serif}}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">
    <span class="badge">{country_es}</span><span class="badge">{subtopic_es}</span>
    &nbsp;·&nbsp; Elena &amp; Marcos &nbsp;·&nbsp; {date_str}
  </div>
</header>
<h2>🎙 Diálogo</h2>
{turns_html}
<h2>📌 Colocaciones clave</h2>
<table>
<thead><tr>
  <th style="text-align:left;padding:9px 14px;color:#999;font-family:sans-serif;font-size:.82em">ESPAÑOL</th>
  <th style="text-align:left;padding:9px 14px;color:#999;font-family:sans-serif;font-size:.82em">ENGLISH</th>
</tr></thead>
<tbody>{col_rows}</tbody>
</table>
<footer>vocab-news-bot · {date_str}</footer>
</body>
</html>""".encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS — per-user tab (auto-created by Telegram username)
# ══════════════════════════════════════════════════════════════════════════════

def get_or_create_sheet(username: str):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ss     = client.open_by_url(SPREADSHEET_URL)
    try:
        return ss.worksheet(username)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=username, rows=1000, cols=3)
        ws.append_row(["Español", "English", "Fecha"], value_input_option="USER_ENTERED")
        log.info(f"[Sheets] Created tab: {username}")
        return ws


def save_collocation(username: str, spanish: str, english: str) -> bool:
    try:
        ws = get_or_create_sheet(username)
        ws.append_row([spanish, english, datetime.now().strftime("%Y-%m-%d")],
                      value_input_option="USER_ENTERED")
        log.info(f"[Sheets] {username}: {spanish}")
        return True
    except Exception as e:
        log.error(f"[Sheets] {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TTS — synthesize, fade (no clicks), stitch, MP3
# ══════════════════════════════════════════════════════════════════════════════

def get_tts_client():
    return texttospeech.TextToSpeechClient(
        credentials=Credentials.from_service_account_file(
            GOOGLE_CREDS_FILE,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    )


def synthesize_turn(text: str, voice_name: str, client) -> bytes:
    return client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code="es-US", name=voice_name),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000,
        ),
    ).audio_content


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def fade_pcm(pcm: bytes, fade_ms: int = 15, rate: int = 24000) -> bytes:
    """Linear fade-in + fade-out on 16-bit mono PCM — eliminates boundary clicks."""
    fade_n = int(rate * fade_ms / 1000)
    n      = len(pcm) // 2
    if n < fade_n * 2:
        return pcm
    samples = list(struct.unpack(f"<{n}h", pcm))
    for i in range(fade_n):
        samples[i]               = int(samples[i] * i / fade_n)
        samples[n - fade_n + i]  = int(samples[n - fade_n + i] * (fade_n - i) / fade_n)
    return struct.pack(f"<{n}h", *samples)


def concatenate_wavs(wavs: list, pause_ms: int = 600, rate: int = 24000) -> bytes:
    silence = b"\x00\x00" * int(rate * pause_ms / 1000)
    frames  = b""
    for w in wavs:
        with wave.open(BytesIO(w), "rb") as wf:
            frames += fade_pcm(wf.readframes(wf.getnframes()))
        frames += silence
    out = BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(frames)
    return out.getvalue()


def wav_to_mp3(wav: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav); in_p = f.name
    out_p = in_p.replace(".wav", ".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_p, "-codec:a", "libmp3lame", "-qscale:a", "2", out_p],
            check=True, capture_output=True,
        )
        return open(out_p, "rb").read()
    finally:
        for p in (in_p, out_p):
            try: os.unlink(p)
            except: pass


def build_audio(turns: list) -> bytes:
    client = get_tts_client()
    chunks = []
    for i, t in enumerate(turns):
        voice = FEMALE_VOICE if t["speaker"] == "Elena" else MALE_VOICE
        log.info(f"[TTS] {i+1}/{len(turns)} {t['speaker']}")
        try:
            chunks.append(pcm_to_wav(synthesize_turn(t["text"], voice, client)))
        except Exception as e:
            log.error(f"[TTS] turn {i+1}: {e}")
        time.sleep(0.3)
    return wav_to_mp3(concatenate_wavs(chunks))


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def get_username(user) -> str:
    """Telegram username, or first name, or user_id as fallback."""
    return user.username or user.first_name or str(user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "ahí"
    await update.message.reply_text(
        f"👋 ¡Hola, {name}! Soy tu bot de podcasts en español 🎙\n"
        "Envía /news para empezar."
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 — pick country."""
    keyboard = [
        [InlineKeyboardButton(COUNTRY_LABELS["argentina"], callback_data="country:argentina"),
         InlineKeyboardButton(COUNTRY_LABELS["rusia"],     callback_data="country:rusia")],
        [InlineKeyboardButton(COUNTRY_LABELS["turquia"],   callback_data="country:turquia"),
         InlineKeyboardButton(COUNTRY_LABELS["china"],     callback_data="country:china")],
        [InlineKeyboardButton(COUNTRY_LABELS["usa"],       callback_data="country:usa")],
    ]
    await update.message.reply_text(
        "🌍 *Paso 1 — ¿De qué país?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 — country chosen, show subtopics."""
    query = update.callback_query
    await query.answer()
    country       = query.data.split(":", 1)[1]
    country_label = COUNTRY_LABELS.get(country, country)

    keyboard = [
        [InlineKeyboardButton(SUBTOPIC_LABELS["medicina"],   callback_data=f"topic:{country}:medicina"),
         InlineKeyboardButton(SUBTOPIC_LABELS["psicologia"], callback_data=f"topic:{country}:psicologia")],
        [InlineKeyboardButton(SUBTOPIC_LABELS["tango"],      callback_data=f"topic:{country}:tango"),
         InlineKeyboardButton(SUBTOPIC_LABELS["cultura"],    callback_data=f"topic:{country}:cultura")],
        [InlineKeyboardButton(SUBTOPIC_LABELS["economia"],   callback_data=f"topic:{country}:economia"),
         InlineKeyboardButton(SUBTOPIC_LABELS["ia"],         callback_data=f"topic:{country}:ia")],
    ]
    await query.edit_message_text(
        f"✅ País: *{country_label}*\n\n🎯 *Paso 2 — ¿Qué tema?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full pipeline: headlines → script → questions → audio → collocations → HTML."""
    query = update.callback_query
    await query.answer()

    _, country, subtopic = query.data.split(":", 2)
    chat_id        = query.message.chat_id
    username       = get_username(query.from_user)
    country_label  = COUNTRY_LABELS.get(country, country)
    subtopic_label = SUBTOPIC_LABELS.get(subtopic, subtopic)

    status = await query.message.reply_text("⏳ Buscando noticias...")

    try:
        # 1. Headlines
        await status.edit_text(f"📰 Obteniendo titulares — {country_label} · {subtopic_label}...")
        headlines = await asyncio.get_event_loop().run_in_executor(
            None, fetch_headlines, country, subtopic, 6
        )
        if not headlines:
            await status.edit_text("❌ No encontré noticias. Inténtalo más tarde.")
            return

        # 2. Script (includes questions + collocations)
        await status.edit_text("✍️ Generando guion (DeepSeek)...")
        script = await asyncio.get_event_loop().run_in_executor(
            None, generate_script, country, subtopic, headlines
        )

        turns        = script.get("turns", [])
        title        = script.get("title", f"{country_label} · {subtopic_label}")
        collocations = script.get("collocations", [])
        questions    = script.get("questions", [])

        if not turns:
            await status.edit_text("❌ Error generando el guion. Inténtalo de nuevo.")
            return

        # 3. Send questions BEFORE audio
        if questions:
            q_lines = "\n\n".join(f"*{i}.* {q}" for i, q in enumerate(questions[:3], 1))
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🤔 *Antes de escuchar — tres preguntas para pensar:*\n\n"
                    + q_lines
                    + "\n\n_Escucha el podcast e intenta responderlas_ 🎧"
                ),
                parse_mode="Markdown",
            )

        # 4. TTS
        await status.edit_text("🔊 Sintetizando audio (~1-2 min)...")
        mp3 = await asyncio.get_event_loop().run_in_executor(None, build_audio, turns)

        # 5. Send MP3
        await status.delete()
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=mp3,
            filename=f"podcast_{country}_{subtopic}_{datetime.now().strftime('%Y%m%d')}.mp3",
            caption=f"🎙 *{title}*\nElena & Marcos — {country_label} · {subtopic_label}",
            parse_mode="Markdown",
        )

        # 6. Collocation buttons (10, stacked Spanish / English)
        if collocations:
            PENDING_COLLOCATIONS[chat_id] = {
                "cols":     collocations,
                "username": username,
                "saved":    set(),
            }
            keyboard = []
            for idx, col in enumerate(collocations[:10]):
                sp = col.get("spanish", "")
                en = col.get("english", "")
                keyboard.append([InlineKeyboardButton(sp, callback_data=f"col:{idx}")])
                keyboard.append([InlineKeyboardButton(en, callback_data=f"col:{idx}")])
            await context.bot.send_message(
                chat_id=chat_id,
                text="💾 *Colocaciones del episodio* — toca para guardar en tu hoja:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        # 7. HTML transcript
        html_bytes = build_html_transcript(title, country, subtopic, turns, collocations)
        safe_title = re.sub(r"[^\w\s-]", "", title)[:40].strip().replace(" ", "_")
        await context.bot.send_document(
            chat_id=chat_id,
            document=html_bytes,
            filename=f"{safe_title}.html",
            caption="📄 Transcripción del episodio",
        )

    except Exception as e:
        log.error(f"Pipeline error: {e}", exc_info=True)
        try:
            await status.edit_text(f"❌ Error: {e}")
        except Exception:
            pass


async def handle_collocation_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if not query.data.startswith("col:"):
        return

    try:
        idx   = int(query.data.split(":", 1)[1])
        state = PENDING_COLLOCATIONS.get(chat_id)
        if not state or idx >= len(state["cols"]):
            await query.answer("⚠️ Datos caducados. Genera un nuevo podcast.", show_alert=True)
            return

        col      = state["cols"][idx]
        spanish  = col.get("spanish", "")
        english  = col.get("english", "")
        username = state["username"]

        ok = await asyncio.get_event_loop().run_in_executor(
            None, save_collocation, username, spanish, english
        )

        if ok:
            state["saved"].add(idx)
            await query.answer(f"✅ Guardado: {spanish}", show_alert=False)
        else:
            await query.answer("❌ Error al guardar.", show_alert=True)
            return

        # Rebuild keyboard with checkmarks
        cols = state["cols"]
        keyboard = []
        for i, c in enumerate(cols[:10]):
            sp   = c.get("spanish", "")
            en   = c.get("english", "")
            mark = " ✅" if i in state["saved"] else ""
            keyboard.append([InlineKeyboardButton(sp + mark, callback_data=f"col:{i}")])
            keyboard.append([InlineKeyboardButton(en + mark, callback_data=f"col:{i}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        log.error(f"Col button: {e}")
        await query.answer("❌ Error.", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news",  cmd_news))
    app.add_handler(CallbackQueryHandler(handle_country,            pattern=r"^country:"))
    app.add_handler(CallbackQueryHandler(handle_topic,              pattern=r"^topic:"))
    app.add_handler(CallbackQueryHandler(handle_collocation_button, pattern=r"^col:"))
    log.info("✅ vocab-news-bot v2 running.")
    app.run_polling()


if __name__ == "__main__":
    main()
