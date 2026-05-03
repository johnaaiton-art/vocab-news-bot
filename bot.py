"""
vocab_news_bot/bot.py  —  Spanish B2 News Podcast Bot for Borja.

/news  →  6 topic buttons
→  scrape RSS for fresh headlines
→  DeepSeek writes ~5min B2 Spanish podcast script
→  Gemini Chirp3-HD TTS (one male, one female Spanish presenter)
→  Send MP3 + 5 collocation buttons (Spanish / English stacked)
→  Press button  →  no sheet saving (no vocab list needed)
"""

import os
import re
import json
import time
import wave
import logging
import asyncio
import tempfile
import subprocess
from io import BytesIO
from datetime import datetime

import requests
import feedparser
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

# ── Google TTS credentials ─────────────────────────────────────────────────
GOOGLE_CREDS_FILE = os.path.join(os.path.dirname(__file__), "google-creds.json")

# ── Chirp3-HD voices for Spanish TTS ──────────────────────────────────────
# es-US voices — lively and engaging
FEMALE_VOICE = "es-US-Chirp3-HD-Aoede"   # female, warm
MALE_VOICE   = "es-US-Chirp3-HD-Puck"    # male, energetic

# ── RSS feeds per topic ────────────────────────────────────────────────────
# Sources: Al Jazeera, SCMP, Daily Sabah, The Hindu, plus specialist feeds
RSS_FEEDS = {
    "economic": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.scmp.com/rss/2/feed",
        "https://www.dailysabah.com/rss/economy",
    ],
    "political": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.scmp.com/rss/2/feed",
        "https://www.dailysabah.com/rss/world",
    ],
    "china": [
        "https://www.scmp.com/rss/2/feed",
        "https://www.scmp.com/rss/4/feed",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "egypt": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "spain": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "usa": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "australia": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.scmp.com/rss/4/feed",
        "https://www.dailysabah.com/rss/world",
    ],
    "latam": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.scmp.com/rss/2/feed",
        "https://www.dailysabah.com/rss/world",
    ],
    "turkey": [
        "https://www.dailysabah.com/rss/world",
        "https://www.dailysabah.com/rss/economy",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "india": [
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "https://www.thehindu.com/news/international/feeder/default.rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.scmp.com/rss/4/feed",
    ],
    "middleeast": [
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.dailysabah.com/rss/world",
        "https://www.scmp.com/rss/2/feed",
    ],
    "ai": [
        "https://www.artificialintelligence-news.com/feed/",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.scmp.com/rss/2/feed",
    ],
}

# ── In-memory collocation store ────────────────────────────────────────────
PENDING_COLLOCATIONS: dict = {}

# ── DeepSeek client ────────────────────────────────────────────────────────
ds_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Fetch headlines
# ══════════════════════════════════════════════════════════════════════════════

def fetch_headlines(topic: str, max_stories: int = 5) -> list:
    import time as _time
    cutoff = _time.time() - 86400

    stories = []
    seen_titles = set()

    keyword_map = {
        "china":      ["china", "chinese", "beijing", "xi ", "taiwan", "hong kong"],
        "egypt":      ["egypt", "egyptian", "cairo", "nile"],
        "spain":      ["spain", "spanish", "madrid", "barcelona", "sanchez", "iberia"],
        "usa":        ["trump", "united states", "congress", "washington", "american", "white house"],
        "australia":  ["australia", "australian", "sydney", "melbourne", "canberra", "albanese"],
        "latam":      ["latin america", "brazil", "mexico", "argentina", "colombia", "venezuela", "chile", "peru", "cuba"],
        "turkey":     ["turkey", "turkish", "erdogan", "ankara", "istanbul", "lira"],
        "india":      ["india", "indian", "modi", "delhi", "mumbai", "pakistan", "kashmir"],
        "middleeast": ["middle east", "israel", "gaza", "iran", "saudi", "lebanon", "syria", "iraq", "yemen", "qatar", "kuwait"],
        "ai":         ["artificial intelligence", "ai model", "chatgpt", "deepseek", "gemini", "claude", "llm", "openai", "anthropic", "mistral", "grok", "language model", "machine learning"],
    }

    for url in RSS_FEEDS.get(topic, []):
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)

                pub = entry.get("published_parsed")
                pub_ts = _time.mktime(pub) if pub else 0

                summary = entry.get("summary", entry.get("description", ""))
                summary = re.sub(r"<[^>]+>", "", summary).strip()[:300]

                stories.append({
                    "title":     title,
                    "summary":   summary,
                    "published": datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d") if pub_ts else "recent",
                    "fresh":     pub_ts >= cutoff,
                })
        except Exception as e:
            log.warning(f"RSS fetch failed {url}: {e}")

    fresh = [s for s in stories if s["fresh"]]
    pool  = fresh if len(fresh) >= 2 else stories

    if topic in keyword_map:
        keywords = keyword_map[topic]
        filtered = [s for s in pool if any(
            k in s["title"].lower() or k in s["summary"].lower() for k in keywords
        )]
        pool = filtered if filtered else pool

    return pool[:max_stories]


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Generate podcast script via DeepSeek
# ══════════════════════════════════════════════════════════════════════════════

TOPIC_INSTRUCTIONS = {
    "economic": (
        "Cubre las 2-3 noticias económicas o empresariales más importantes del día. "
        "Incluye movimientos del mercado, comercio internacional o grandes novedades corporativas."
    ),
    "political": (
        "Cubre el desarrollo geopolítico más importante O la noticia más destacada a nivel internacional. "
        "Elige la más relevante del día."
    ),
    "china": (
        "Cubre las últimas noticias sobre China: política interior, economía, relaciones exteriores o sociedad."
    ),
    "egypt": (
        "Cubre las últimas noticias de Egipto: política, economía, sociedad o relaciones regionales."
    ),
    "spain": (
        "Cubre las noticias más importantes de España: política, economía, sociedad o cultura."
    ),
    "usa": (
        "Cubre la noticia más importante de los Estados Unidos hoy: política, economía o sociedad."
    ),
    "australia": (
        "Cubre las últimas noticias de Australia: política, economía, relaciones con Asia o sociedad."
    ),
    "latam": (
        "Cubre las noticias más importantes de América Latina: política, economía, conflictos sociales "
        "o relaciones regionales. Puede ser de cualquier país latinoamericano."
    ),
    "turkey": (
        "Cubre las últimas noticias de Turquía: política interior, economía, papel en Oriente Medio "
        "o relaciones con la OTAN y Rusia."
    ),
    "india": (
        "Cubre las últimas noticias de India: política, economía, relaciones con China y Pakistán, "
        "o tecnología y sociedad."
    ),
    "middleeast": (
        "Cubre las noticias más importantes de Oriente Medio: conflictos, diplomacia, petróleo, "
        "o cambios políticos en la región."
    ),
    "ai": (
        "Cubre los últimos avances en inteligencia artificial — SOLO aspectos técnicos y de capacidades: "
        "nuevos modelos lanzados (incluyendo modelos chinos como DeepSeek, Qwen, etc.), "
        "nuevas funcionalidades, comparaciones entre modelos, lo que ahora es posible hacer con IA "
        "que antes no lo era. NO hables de inversiones, valoraciones ni dinero. "
        "El enfoque es: ¿qué puede hacer la IA ahora que no podía hacer antes?"
    ),
}

PODCAST_SYSTEM_PROMPT = """Eres un guionista de podcasts en español.
Escribes conversaciones animadas y naturales para dos presentadores que llevan años trabajando juntos y tienen una química innegable:
- Elena — presentadora femenina, inteligente y con mucho carácter. Le gusta provocar a Marcos con comentarios irónicos y sabe exactamente cómo sacarle de quicio. Pero también le admira, aunque nunca lo admita del todo.
- Marcos — presentador masculino, seguro de sí mismo y con sentido del humor seco. Le encanta picar a Elena y coquetear sutilmente con ella. Se toma las noticias en serio pero no se toma a sí mismo demasiado en serio.

SU DINÁMICA:
- Se hacen comentarios irónicos el uno al otro mientras dan las noticias. Pequeños piques, bromas internas, miradas cómplices que se notan en el tono.
- Hay una tensión coqueta sutil — no cursi ni exagerada, solo la química natural de dos personas que se gustan y lo saben.
- Se interrumpen de vez en cuando, se corrigen con cariño, se ríen de algo que dijo el otro.
- Ejemplos del tipo de intercambio:
    Elena: "...y según los analistas, la situación podría empeorar."
    Marcos: "¿Ves? Por eso yo nunca invierto en bolsa. Tú tampoco deberías, Elena."
    Elena: "Marcos, tú no inviertes porque gastas todo en café."
- O:
    Marcos: "Bueno, esto lo explico yo porque sé que a ti la geopolítica te da sueño."
    Elena: "Me da sueño cuando la explicas tú, que no es lo mismo."
- Que fluya natural — no forzado, no cada turno, pero sí repartido por todo el episodio.

REGLAS DE IDIOMA:
- TODO el guion en español (nivel B2 — fluido pero accesible).
- Vocabulario variado y natural. Nada de lenguaje excesivamente técnico sin explicación.
- Si un concepto es complejo, uno de los presentadores lo explica brevemente con palabras más simples, en español.

REGLAS DE NOMBRES:
- Nombres de personas: SOLO se puede decir "Trump" y "Putin" por su nombre.
  Para los demás usa el cargo: "el presidente de Egipto", "el CEO de NVIDIA", etc.
- Países y empresas: usa sus nombres en español directamente, sin introducirlos en inglés.

FORMATO DE SALIDA — solo JSON válido, sin marcadores markdown:
{
  "title": "título del episodio en español",
  "turns": [
    {"speaker": "Elena", "text": "..."},
    {"speaker": "Marcos", "text": "..."},
    ...
  ],
  "collocations": [
    {"spanish": "caída económica", "english": "economic downturn"},
    {"spanish": "disputa comercial", "english": "trade dispute"},
    {"spanish": "relaciones diplomáticas", "english": "diplomatic relations"},
    {"spanish": "ajuste de política", "english": "policy adjustment"},
    {"spanish": "reacción del mercado", "english": "market reaction"}
  ]
}

El array "collocations": 5 colocaciones especialmente relevantes para este episodio.
Cada una: 2-5 palabras en español, con traducción al inglés.

Duración objetivo: ~25-35 turnos de diálogo (unos 5 minutos de audio a ritmo natural).
"""


def generate_script(topic: str, headlines: list) -> dict:
    headline_text = "\n".join(
        f"- {s['title']}: {s['summary']}" for s in headlines
    )
    topic_instruction = TOPIC_INSTRUCTIONS.get(topic, "Cubre las noticias más importantes del día.")

    user_prompt = f"""Instrucción del tema: {topic_instruction}

Titulares de hoy (úsalos como fuente factual):
{headline_text}

Escribe ahora el guion completo del podcast."""

    log.info("[DeepSeek] Generating Spanish script...")
    response = ds_client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": PODCAST_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=4000,
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — TTS via Google Chirp3-HD (Spanish)
# ══════════════════════════════════════════════════════════════════════════════

def get_tts_client():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return texttospeech.TextToSpeechClient(credentials=creds)


def synthesize_turn(text: str, voice_name: str, tts_client) -> bytes:
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="es-US",
        name=voice_name,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=24000,
    )
    response = tts_client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )
    return response.audio_content


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def fade_pcm(pcm_bytes: bytes, fade_ms: int = 15, sample_rate: int = 24000) -> bytes:
    """Apply a short linear fade-in and fade-out to raw 16-bit mono PCM to kill boundary clicks."""
    import struct
    fade_frames = int(sample_rate * fade_ms / 1000)
    n_frames    = len(pcm_bytes) // 2
    if n_frames < fade_frames * 2:
        return pcm_bytes  # too short to fade safely

    samples = list(struct.unpack(f"<{n_frames}h", pcm_bytes))

    # fade in
    for i in range(fade_frames):
        samples[i] = int(samples[i] * i / fade_frames)
    # fade out
    for i in range(fade_frames):
        idx = n_frames - fade_frames + i
        samples[idx] = int(samples[idx] * (fade_frames - i) / fade_frames)

    return struct.pack(f"<{n_frames}h", *samples)


def concatenate_wavs(wav_list: list, pause_ms: int = 600, sample_rate: int = 24000) -> bytes:
    silence_frames = int(sample_rate * pause_ms / 1000)
    silence_pcm    = b"\x00\x00" * silence_frames
    all_frames = b""
    for wav_bytes in wav_list:
        buf = BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        all_frames += fade_pcm(raw)
        all_frames += silence_pcm
    out = BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(all_frames)
    return out.getvalue()


def wav_to_mp3(wav_bytes: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        tmp_in.write(wav_bytes)
        in_path = tmp_in.name
    out_path = in_path.replace(".wav", ".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-codec:a", "libmp3lame", "-qscale:a", "2", out_path],
            check=True, capture_output=True,
        )
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except Exception:
                pass


def build_audio(turns: list) -> bytes:
    tts_client = get_tts_client()
    wav_chunks = []
    for i, turn in enumerate(turns):
        speaker = turn["speaker"]
        text    = turn["text"]
        voice   = FEMALE_VOICE if speaker == "Elena" else MALE_VOICE
        log.info(f"[TTS] turn {i+1}/{len(turns)} — {speaker} ({voice})")
        try:
            pcm = synthesize_turn(text, voice, tts_client)
            wav_chunks.append(pcm_to_wav(pcm))
        except Exception as e:
            log.error(f"TTS failed turn {i+1}: {e}")
        time.sleep(0.3)
    combined = concatenate_wavs(wav_chunks, pause_ms=600)
    return wav_to_mp3(combined)


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

TOPIC_LABELS = {
    "economic":   "📈 Economía",
    "political":  "🌐 Geopolítica",
    "china":      "🇨🇳 China",
    "egypt":      "🇪🇬 Egipto",
    "spain":      "🇪🇸 España",
    "usa":        "🇺🇸 Estados Unidos",
    "australia":  "🦘 Australia",
    "latam":      "🌎 América Latina",
    "turkey":     "🇹🇷 Turquía",
    "india":      "🇮🇳 India",
    "middleeast": "🕌 Oriente Medio",
    "ai":         "🤖 Inteligencia Artificial",
}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Envía /news para generar un podcast de noticias en español (nivel B2)."
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(TOPIC_LABELS["economic"],   callback_data="topic:economic"),
         InlineKeyboardButton(TOPIC_LABELS["political"],  callback_data="topic:political")],
        [InlineKeyboardButton(TOPIC_LABELS["china"],      callback_data="topic:china"),
         InlineKeyboardButton(TOPIC_LABELS["india"],      callback_data="topic:india")],
        [InlineKeyboardButton(TOPIC_LABELS["usa"],        callback_data="topic:usa"),
         InlineKeyboardButton(TOPIC_LABELS["latam"],      callback_data="topic:latam")],
        [InlineKeyboardButton(TOPIC_LABELS["middleeast"], callback_data="topic:middleeast"),
         InlineKeyboardButton(TOPIC_LABELS["egypt"],      callback_data="topic:egypt")],
        [InlineKeyboardButton(TOPIC_LABELS["turkey"],     callback_data="topic:turkey"),
         InlineKeyboardButton(TOPIC_LABELS["spain"],      callback_data="topic:spain")],
        [InlineKeyboardButton(TOPIC_LABELS["australia"],  callback_data="topic:australia"),
         InlineKeyboardButton(TOPIC_LABELS["ai"],         callback_data="topic:ai")],
    ]
    await update.message.reply_text(
        "🎙 Elige el tema del podcast de hoy:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("topic:"):
        return

    topic   = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    label   = TOPIC_LABELS.get(topic, topic)

    status_msg = await query.message.reply_text(
        f"⏳ Preparando el podcast «{label}»... (obteniendo noticias)"
    )

    try:
        await status_msg.edit_text(f"📰 Obteniendo titulares... ({label})")
        headlines = await asyncio.get_event_loop().run_in_executor(
            None, fetch_headlines, topic, 5
        )
        if not headlines:
            await status_msg.edit_text("❌ No se pudieron obtener noticias. Inténtalo más tarde.")
            return
        log.info(f"[{topic}] {len(headlines)} headlines")

        await status_msg.edit_text("✍️ Generando guion (DeepSeek)...")
        script_data = await asyncio.get_event_loop().run_in_executor(
            None, generate_script, topic, headlines
        )

        turns         = script_data.get("turns", [])
        episode_title = script_data.get("title", label)
        collocations  = script_data.get("collocations", [])

        if not turns:
            await status_msg.edit_text("❌ Error al generar el guion. Inténtalo de nuevo.")
            return
        log.info(f"[{topic}] {len(turns)} turns — {episode_title}")

        await status_msg.edit_text("🔊 Sintetizando audio (~1-2 min)...")
        mp3_bytes = await asyncio.get_event_loop().run_in_executor(
            None, build_audio, turns
        )

        await status_msg.delete()
        caption = f"🎙 {episode_title}\nElena & Marcos — {label}"
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=mp3_bytes,
            filename=f"podcast_{topic}_{datetime.now().strftime('%Y%m%d')}.mp3",
            caption=caption,
        )

        # Collocation buttons — Spanish on top row, English below
        if collocations:
            PENDING_COLLOCATIONS[chat_id] = collocations
            keyboard = []
            for idx, col in enumerate(collocations[:5]):
                sp = col.get("spanish", "")
                en = col.get("english", "")
                keyboard.append([InlineKeyboardButton(sp, callback_data=f"col:{idx}")])
                keyboard.append([InlineKeyboardButton(en, callback_data=f"col:{idx}")])
            await context.bot.send_message(
                chat_id=chat_id,
                text="📌 Colocaciones del episodio — toca para guardar:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    except Exception as e:
        log.error(f"Pipeline error: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"❌ Error: {e}")
        except Exception:
            pass


async def handle_collocation_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if not query.data.startswith("col:"):
        return

    try:
        idx  = int(query.data.split(":", 1)[1])
        cols = PENDING_COLLOCATIONS.get(chat_id, [])
        if not cols or idx >= len(cols):
            await query.answer("⚠️ Datos caducados. Genera un nuevo podcast.", show_alert=True)
            return

        col     = cols[idx]
        spanish = col.get("spanish", "")
        english = col.get("english", "")

        # Mark as saved in the keyboard
        cols_updated = PENDING_COLLOCATIONS.get(chat_id, [])
        keyboard = []
        for i, c in enumerate(cols_updated[:5]):
            sp = c.get("spanish", "")
            en = c.get("english", "")
            mark = " ✅" if i == idx else ""
            keyboard.append([InlineKeyboardButton(sp + mark, callback_data=f"col:{i}")])
            keyboard.append([InlineKeyboardButton(en + mark, callback_data=f"col:{i}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer(f"✅ {spanish} — {english}", show_alert=False)

    except Exception as e:
        log.error(f"Collocation button error: {e}")
        await query.answer("❌ Error.", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news",  cmd_news))
    app.add_handler(CallbackQueryHandler(handle_topic,              pattern=r"^topic:"))
    app.add_handler(CallbackQueryHandler(handle_collocation_button, pattern=r"^col:"))
    log.info("✅ Spanish news podcast bot running. Send /news.")
    app.run_polling()


if __name__ == "__main__":
    main()
