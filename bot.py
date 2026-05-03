"""
vocab_news_bot/bot.py

Chinese Mandarin News Podcast Bot for Borja.

Flow:
  /news  →  4 topic buttons (economic / political / china / egypt)
  User picks topic  →  scrape RSS for fresh headlines (≤24h old)
  →  DeepSeek writes ~5min HSK4 Mandarin podcast script (uses vocab from Google Sheet)
  →  Gemini Chirp3-HD TTS (one male, one female presenter)
  →  Send MP3 + 5 collocation buttons (Chinese / English stacked)
  →  Press button  →  save to Google Sheet col A (Chinese), col B (English), col C (date)
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
from pathlib import Path

import requests
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

# ── Env vars (loaded from .env via EnvironmentFile in systemd) ─────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY")

if not TELEGRAM_BOT_TOKEN or not DEEPSEEK_API_KEY:
    raise EnvironmentError("TELEGRAM_BOT_TOKEN and DEEPSEEK_API_KEY must be set.")

# ── Google Sheets ──────────────────────────────────────────────────────────
GOOGLE_CREDS_FILE  = os.path.join(os.path.dirname(__file__), "google-creds.json")
SPREADSHEET_URL    = "https://docs.google.com/spreadsheets/d/1H-ezqh5Vcl3_6YWJIy9KvgpDCsM3V6N4LJq0dqNbJS0/edit?gid=0#gid=0"
SHEET_NAME         = "Chinese"

# ── Chirp3-HD voices for Chinese TTS ──────────────────────────────────────
# One female, one male — both Chirp3-HD, lively and engaging
FEMALE_VOICE = "cmn-CN-Chirp3-HD-Aoede"   # female, warm and clear
MALE_VOICE   = "cmn-CN-Chirp3-HD-Puck"    # male, energetic

# ── News RSS sources ───────────────────────────────────────────────────────
# We scrape these directly — no API key needed, always fresh
RSS_FEEDS = {
    "economic": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    ],
    "political": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
    "china": [
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "https://feeds.reuters.com/reuters/CNtopNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
    "egypt": [
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://feeds.reuters.com/Reuters/worldNews",
    ],
}

# ── Allowed country/org names in Chinese (everything else simplified) ──────
KNOWN_CHINESE_NAMES = {
    "美国", "英国", "法国", "德国", "俄罗斯", "乌克兰", "中国",
    "伊朗", "加拿大", "澳大利亚", "西班牙", "意大利", "埃及",
    "特朗普", "普京",  # only these two personal names allowed
}

# ── In-memory store for pending collocations (per chat_id) ─────────────────
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
# STEP 1 — Fetch headlines from RSS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_headlines(topic: str, max_stories: int = 5) -> list:
    """
    Pull recent headlines from RSS feeds for the chosen topic.
    Returns list of dicts: {title, summary, published}
    Only stories published within the last 24 hours are returned.
    Falls back to latest available if nothing is fresh enough.
    """
    import time as _time
    cutoff = _time.time() - 86400  # 24 hours ago

    stories = []
    seen_titles = set()

    for url in RSS_FEEDS.get(topic, []):
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)

                # Check recency
                pub = entry.get("published_parsed")
                pub_ts = _time.mktime(pub) if pub else 0

                summary = entry.get("summary", entry.get("description", ""))
                # Strip HTML tags from summary
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                summary = summary[:300]

                stories.append({
                    "title": title,
                    "summary": summary,
                    "published": datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d") if pub_ts else "recent",
                    "fresh": pub_ts >= cutoff,
                })
        except Exception as e:
            log.warning(f"RSS fetch failed for {url}: {e}")

    # Prefer fresh stories; fall back to all if not enough fresh ones
    fresh = [s for s in stories if s["fresh"]]
    pool = fresh if len(fresh) >= 2 else stories

    # For "china" and "egypt" filter by keyword in title
    if topic in ("china", "egypt"):
        keyword_map = {
            "china":  ["china", "chinese", "beijing", "xi ", "taiwan", "hong kong"],
            "egypt":  ["egypt", "egyptian", "cairo", "sisi", "nile"],
        }
        keywords = keyword_map[topic]
        filtered = [s for s in pool if any(k in s["title"].lower() or k in s["summary"].lower() for k in keywords)]
        pool = filtered if filtered else pool  # fall back to all if nothing matches

    return pool[:max_stories]


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Fetch vocab collocations from Google Sheet
# ══════════════════════════════════════════════════════════════════════════════

def fetch_vocab_from_sheet(n: int = 8) -> list:
    """
    Pull the most recent n Chinese collocations from column A of the sheet.
    Returns list of Chinese strings.
    """
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        ss     = client.open_by_url(SPREADSHEET_URL)
        ws     = ss.worksheet(SHEET_NAME)

        # Column A = Chinese collocations, Column C = date
        all_rows = ws.get_all_values()
        # Skip header row if it looks like a header
        data_rows = [r for r in all_rows if r and r[0] and not r[0].startswith("中") == False or (r[0] and '\u4e00' <= r[0][0] <= '\u9fff')]
        # Grab column A values (Chinese)
        chinese_col = [r[0].strip() for r in all_rows if r and r[0].strip() and '\u4e00' <= r[0][0] <= '\u9fff']
        # Most recent n (sheet appends to bottom)
        recent = list(reversed(chinese_col))[:n]
        log.info(f"[Sheet] fetched {len(recent)} vocab items")
        return recent
    except Exception as e:
        log.error(f"Sheet fetch error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Generate podcast script via DeepSeek
# ══════════════════════════════════════════════════════════════════════════════

TOPIC_INSTRUCTIONS = {
    "economic": (
        "Cover the 2-3 most important economic or business news stories. "
        "Include any market movements, trade news, or major corporate developments."
    ),
    "political": (
        "Cover the most important geopolitical development OR the biggest story from the United States. "
        "Choose whichever is more significant today."
    ),
    "china": (
        "Cover the latest news about China — domestic policy, economy, foreign relations, or society."
    ),
    "egypt": (
        "Cover the latest news from Egypt — politics, economy, society, or regional relations."
    ),
}

# Country/leader name mappings for the script prompt
NAME_RULES = """
STRICT NAME RULES — follow exactly:
- Personal names: ONLY 特朗普 (Trump) and 普京 (Putin) may be used by name.
  For ALL other people: use their title, e.g. 埃及总统 (Egyptian president), 英伟达CEO (NVIDIA CEO).
- The first time a country is mentioned: say its Chinese name, then say the English name,
  then use only Chinese for the rest of that episode.
  e.g. "中国，也就是China，..." and from then on just 中国.
- Same rule for major companies the first time they appear.
- Allowed country names in Chinese: 美国, 英国, 法国, 德国, 俄罗斯, 乌克兰, 中国,
  伊朗, 加拿大, 澳大利亚, 西班牙, 意大利, 埃及.
  Any other country: describe it briefly rather than using a potentially unknown name.
"""

PODCAST_SYSTEM_PROMPT = """You are a Mandarin Chinese podcast scriptwriter.
You write lively, natural, engaging conversation scripts for two hosts:
- 小梅 (Xiǎo Méi) — female host, warm and curious
- 大明 (Dà Míng) — male host, calm and analytical

LANGUAGE RULES:
- ENTIRE script in Simplified Mandarin Chinese (HSK 4 level for most content).
- The vocabulary items provided by the user MUST be used naturally — woven into the dialogue.
  They can be adapted (different tense, measure word added, split across a phrase, etc.).
- If a passage would be difficult for an upper-intermediate learner, one host can briefly
  re-explain it in simpler Mandarin immediately after (NOT in English).
- No English except: the one-time introduction of a country or company name as described below.

""" + NAME_RULES + """

OUTPUT FORMAT — return only valid JSON, no markdown fences:
{
  "title": "episode title in Chinese",
  "turns": [
    {"speaker": "小梅", "text": "..."},
    {"speaker": "大明", "text": "..."},
    ...
  ],
  "collocations": [
    {"chinese": "经济下滑", "english": "economic downturn"},
    {"chinese": "贸易争端", "english": "trade dispute"},
    {"chinese": "外交关系", "english": "diplomatic relations"},
    {"chinese": "政策调整", "english": "policy adjustment"},
    {"chinese": "市场反应", "english": "market reaction"}
  ]
}

The "collocations" array: pick 5 collocations that are PARTICULARLY RELEVANT to this episode's content.
Each collocation: 2-5 Chinese characters, meaningful in the context of today's stories.
Include English translation.

Target length: ~25-35 dialogue turns (about 5 minutes of audio at natural pace).
"""


def generate_script(topic: str, headlines: list, vocab: list) -> dict:
    """
    Ask DeepSeek to write the podcast script.
    Returns the parsed JSON dict with keys: title, turns, collocations.
    """
    headline_text = "\n".join(
        f"- {s['title']}: {s['summary']}" for s in headlines
    )
    vocab_text = "、".join(vocab) if vocab else "（无）"

    topic_instruction = TOPIC_INSTRUCTIONS.get(topic, "Cover today's major news.")

    user_prompt = f"""Topic instruction: {topic_instruction}

Today's news headlines (use these as your factual source):
{headline_text}

Vocabulary from the learner's Google Sheet (MUST weave these in naturally):
{vocab_text}

Write the full podcast script now."""

    log.info("[DeepSeek] Generating script...")
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
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON object
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — TTS via Google Chirp3-HD
# ══════════════════════════════════════════════════════════════════════════════

def get_tts_client():
    """Build a Google TTS client from the credentials file."""
    import google.auth
    from google.oauth2.service_account import Credentials as SACredentials
    creds = SACredentials.from_service_account_file(
        GOOGLE_CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return texttospeech.TextToSpeechClient(credentials=creds)


def synthesize_turn(text: str, voice_name: str, tts_client) -> bytes:
    """
    Synthesize a single dialogue turn.
    Returns raw LINEAR16 PCM bytes (24kHz mono).
    """
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="cmn-CN",
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
    return response.audio_content  # raw PCM (LINEAR16)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Wrap raw PCM bytes in a WAV container."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def concatenate_wavs(wav_list: list, pause_ms: int = 500, sample_rate: int = 24000) -> bytes:
    """Stitch WAV chunks together with a short silence between turns."""
    silence_frames = int(sample_rate * pause_ms / 1000)
    silence_pcm    = b"\x00\x00" * silence_frames

    all_frames = b""
    for wav_bytes in wav_list:
        buf = BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            all_frames += wf.readframes(wf.getnframes())
        all_frames += silence_pcm

    out = BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(all_frames)
    return out.getvalue()


def wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Convert WAV bytes → MP3 bytes using ffmpeg (must be installed on VM)."""
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
    """
    Run TTS for every dialogue turn and stitch into a single MP3.
    Returns MP3 bytes.
    """
    tts_client = get_tts_client()
    wav_chunks = []

    for i, turn in enumerate(turns):
        speaker = turn["speaker"]
        text    = turn["text"]
        voice   = FEMALE_VOICE if speaker == "小梅" else MALE_VOICE
        log.info(f"[TTS] turn {i+1}/{len(turns)} — {speaker} ({voice})")
        try:
            pcm  = synthesize_turn(text, voice, tts_client)
            wav  = pcm_to_wav(pcm)
            wav_chunks.append(wav)
        except Exception as e:
            log.error(f"TTS failed for turn {i+1}: {e}")
        time.sleep(0.3)

    combined_wav = concatenate_wavs(wav_chunks, pause_ms=600)
    return wav_to_mp3(combined_wav)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Save collocation to Google Sheet
# ══════════════════════════════════════════════════════════════════════════════

def save_collocation(chinese: str, english: str) -> bool:
    """Append a row to the Chinese sheet: col A = Chinese, col B = English, col C = date."""
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        ss     = client.open_by_url(SPREADSHEET_URL)
        ws     = ss.worksheet(SHEET_NAME)
        date_str = datetime.now().strftime("%Y-%m-%d")
        ws.append_row([chinese, english, date_str], value_input_option="USER_ENTERED")
        log.info(f"[Sheet] saved: {chinese} | {english} | {date_str}")
        return True
    except Exception as e:
        log.error(f"[Sheet] save failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

TOPIC_LABELS = {
    "economic": "📈 经济 Economic",
    "political": "🌐 政治 Political",
    "china":    "🇨🇳 中国 China",
    "egypt":    "🇪🇬 埃及 Egypt",
}


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /news — show topic selection buttons."""
    keyboard = [
        [InlineKeyboardButton(TOPIC_LABELS["economic"],  callback_data="topic:economic")],
        [InlineKeyboardButton(TOPIC_LABELS["political"], callback_data="topic:political")],
        [InlineKeyboardButton(TOPIC_LABELS["china"],     callback_data="topic:china")],
        [InlineKeyboardButton(TOPIC_LABELS["egypt"],     callback_data="topic:egypt")],
    ]
    await update.message.reply_text(
        "🎙 选择今天的播客主题 / Choose a podcast topic:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！发送 /news 来生成一个新闻播客。\n"
        "Send /news to generate a Mandarin news podcast."
    )


async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called when user taps a topic button.
    Runs the full pipeline: scrape → vocab → script → TTS → send.
    """
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("topic:"):
        return

    topic    = query.data.split(":", 1)[1]
    chat_id  = query.message.chat_id
    label    = TOPIC_LABELS.get(topic, topic)

    status_msg = await query.message.reply_text(
        f"⏳ 正在为您准备《{label}》播客，请稍候...\n(Fetching news & generating script...)"
    )

    try:
        # 1. Fetch headlines
        await status_msg.edit_text(f"📰 正在获取最新新闻... ({label})")
        headlines = await asyncio.get_event_loop().run_in_executor(
            None, fetch_headlines, topic, 5
        )
        if not headlines:
            await status_msg.edit_text("❌ 未能获取新闻，请稍后再试。")
            return
        log.info(f"[{topic}] Got {len(headlines)} headlines")

        # 2. Fetch vocab
        await status_msg.edit_text("📚 正在从词汇表获取生词...")
        vocab = await asyncio.get_event_loop().run_in_executor(
            None, fetch_vocab_from_sheet, 8
        )

        # 3. Generate script
        await status_msg.edit_text("✍️ 正在生成播客脚本 (DeepSeek)...")
        script_data = await asyncio.get_event_loop().run_in_executor(
            None, generate_script, topic, headlines, vocab
        )

        turns        = script_data.get("turns", [])
        episode_title = script_data.get("title", label)
        collocations = script_data.get("collocations", [])

        if not turns:
            await status_msg.edit_text("❌ 脚本生成失败，请重试。")
            return
        log.info(f"[{topic}] Script: {len(turns)} turns, title: {episode_title}")

        # 4. TTS
        await status_msg.edit_text("🔊 正在合成语音，请稍候（约1-2分钟）...")
        mp3_bytes = await asyncio.get_event_loop().run_in_executor(
            None, build_audio, turns
        )

        # 5. Send MP3
        await status_msg.delete()
        caption = f"🎙 {episode_title}\n小梅 & 大明 — {label} 播客"
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=mp3_bytes,
            filename=f"podcast_{topic}_{datetime.now().strftime('%Y%m%d')}.mp3",
            caption=caption,
        )

        # 6. Build collocation buttons (stacked: Chinese on top, English below)
        if collocations:
            PENDING_COLLOCATIONS[chat_id] = collocations
            keyboard = []
            for idx, col in enumerate(collocations[:5]):
                zh   = col.get("chinese", "")
                en   = col.get("english", "")
                # Two rows per collocation: Chinese button + English label button
                keyboard.append([
                    InlineKeyboardButton(zh, callback_data=f"col:{idx}"),
                ])
                keyboard.append([
                    InlineKeyboardButton(en, callback_data=f"col:{idx}"),
                ])
            await context.bot.send_message(
                chat_id=chat_id,
                text="💾 点击搭配保存到词汇表 / Tap a collocation to save it:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    except Exception as e:
        log.error(f"Pipeline error: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"❌ 出错了: {e}")
        except Exception:
            pass


async def handle_collocation_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle collocation save button taps."""
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if not query.data.startswith("col:"):
        return

    try:
        idx  = int(query.data.split(":", 1)[1])
        cols = PENDING_COLLOCATIONS.get(chat_id, [])
        if not cols or idx >= len(cols):
            await query.answer("⚠️ 数据已过期，请重新生成播客。", show_alert=True)
            return

        col     = cols[idx]
        chinese = col.get("chinese", "")
        english = col.get("english", "")

        success = await asyncio.get_event_loop().run_in_executor(
            None, save_collocation, chinese, english
        )

        if success:
            await query.answer(f"✅ 已保存：{chinese}", show_alert=False)
            # Update button text to show it's saved
            # Rebuild keyboard marking saved items
            cols_updated = PENDING_COLLOCATIONS.get(chat_id, [])
            keyboard = []
            for i, c in enumerate(cols_updated[:5]):
                zh = c.get("chinese", "")
                en = c.get("english", "")
                saved_mark = " ✅" if i == idx else ""
                keyboard.append([InlineKeyboardButton(zh + saved_mark, callback_data=f"col:{i}")])
                keyboard.append([InlineKeyboardButton(en + saved_mark, callback_data=f"col:{i}")])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.answer("❌ 保存失败，请检查 Google Sheets 配置。", show_alert=True)

    except Exception as e:
        log.error(f"Collocation button error: {e}")
        await query.answer("❌ 出错了。", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news",  cmd_news))
    app.add_handler(CallbackQueryHandler(handle_topic,              pattern=r"^topic:"))
    app.add_handler(CallbackQueryHandler(handle_collocation_button, pattern=r"^col:"))

    log.info("✅ vocab-news-bot running. Send /news to generate a podcast.")
    app.run_polling()


if __name__ == "__main__":
    main()
