import os
import json
import hashlib
import re
import zipfile
import time
from datetime import datetime
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from google.cloud import texttospeech
from google.oauth2 import service_account
import asyncio
from io import BytesIO
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Environment variables - SAFE FOR GITHUB/RAILWAY
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Configuration
class Config:
    MAX_TOPIC_LENGTH = 100
    MAX_VOCAB_ITEMS = 15
    TTS_TIMEOUT = 30
    API_RETRY_ATTEMPTS = 3
    RATE_LIMIT_REQUESTS = 5
    RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB for Telegram

config = Config()

# Initialize DeepSeek client
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# Rate Limiter
class RateLimiter:
    def __init__(self, max_requests=5, window=3600):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window = window
    
    def is_allowed(self, user_id):
        now = time.time()
        user_requests = self.requests[user_id]
        
        # Remove old requests outside the time window
        user_requests[:] = [req_time for req_time in user_requests 
                          if now - req_time < self.window]
        
        if len(user_requests) >= self.max_requests:
            return False
        
        user_requests.append(now)
        return True
    
    def get_reset_time(self, user_id):
        """Get time until rate limit resets"""
        if not self.requests[user_id]:
            return 0
        oldest_request = min(self.requests[user_id])
        reset_time = oldest_request + self.window - time.time()
        return max(0, int(reset_time))

rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_REQUESTS,
    window=config.RATE_LIMIT_WINDOW
)

def get_google_tts_client():
    """Initialize Google TTS client with credentials from environment variable"""
    if GOOGLE_CREDENTIALS_JSON:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return texttospeech.TextToSpeechClient(credentials=credentials)
    else:
        return texttospeech.TextToSpeechClient()

def validate_topic(topic):
    """Validate and sanitize topic input"""
    # Remove excessive whitespace
    topic = re.sub(r'\s+', ' ', topic.strip())
    
    # Check for harmful patterns (command injection, path traversal)
    if re.search(r'[<>"|&;`$()]', topic):
        raise ValueError("Topic contains invalid characters")
    
    # Basic content moderation (Chinese and English)
    inappropriate_patterns = [
        r'\b(porn|sex|violence|hate|kill|death)\b',
        r'\b(暴力|色情|仇恨|歧视|杀|死)\b',
    ]
    
    for pattern in inappropriate_patterns:
        if re.search(pattern, topic, re.IGNORECASE):
            raise ValueError("Topic contains inappropriate content")
    
    # Enforce length limit
    if len(topic) > config.MAX_TOPIC_LENGTH:
        topic = topic[:config.MAX_TOPIC_LENGTH]
    
    if not topic:
        raise ValueError("Topic cannot be empty")
    
    return topic

def split_text_into_sentences(text, max_length=150):
    """Split text into smaller sentences for Chirp3"""
    sentences = re.split(r'([。！？；])', text)
    
    result = []
    for i in range(0, len(sentences)-1, 2):
        if i+1 < len(sentences):
            result.append(sentences[i] + sentences[i+1])
        else:
            result.append(sentences[i])
    
    final_result = []
    for sentence in result:
        if len(sentence) > max_length:
            parts = re.split(r'([，、])', sentence)
            temp = ""
            for part in parts:
                if len(temp + part) > max_length and temp:
                    final_result.append(temp)
                    temp = part
                else:
                    temp += part
            if temp:
                final_result.append(temp)
        else:
            final_result.append(sentence)
    
    return [s.strip() for s in final_result if s.strip()]

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    retry=retry_if_exception_type(Exception)
)
def generate_tts_chirp_sync(text):
    """Generate Chinese TTS audio using Google Cloud Chirp3 (sync version)"""
    try:
        client = get_google_tts_client()
        
        sentences = split_text_into_sentences(text, max_length=150)
        
        all_audio = b""
        for sentence in sentences:
            synthesis_input = texttospeech.SynthesisInput(text=sentence)
            voice = texttospeech.VoiceSelectionParams(
                language_code="cmn-CN",
                name="cmn-CN-Chirp3-HD-Aoede",
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
            )
            
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )
            
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )
            
            all_audio += response.audio_content
        
        return all_audio
    
    except Exception as e:
        print(f"Chirp3 TTS Error: {str(e)}")
        # Try fallback to Wavenet
        return generate_tts_wavenet_sync(text)

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    retry=retry_if_exception_type(Exception)
)
def generate_tts_wavenet_sync(text):
    """Fallback TTS using Wavenet (sync version)"""
    try:
        client = get_google_tts_client()
        
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="cmn-CN",
            name="cmn-CN-Wavenet-A",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )
        
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.8,
        )
        
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config
        )
        
        return response.audio_content
    
    except Exception as e:
        print(f"Wavenet TTS Error: {str(e)}")
        return None

async def generate_tts_async(text, use_chirp=True):
    """Run TTS generation in thread pool"""
    loop = asyncio.get_event_loop()
    if use_chirp:
        return await loop.run_in_executor(None, generate_tts_chirp_sync, text)
    else:
        return await loop.run_in_executor(None, generate_tts_wavenet_sync, text)

def safe_filename(filename):
    """Sanitize filename to prevent path traversal (ZIP slip vulnerability)"""
    # Remove path separators and dangerous characters
    filename = re.sub(r'[^\w\s.-]', '', filename)
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    # Get just the basename to strip any path components
    filename = os.path.basename(filename)
    # Ensure reasonable length
    filename = filename[:100]
    return filename.strip('_')

def validate_deepseek_response(content):
    """Validate DeepSeek JSON response structure"""
    required_keys = ["main_text", "vocabulary", "opinion_texts", "discussion_questions"]
    
    # Check all required keys exist
    if not all(k in content for k in required_keys):
        missing = [k for k in required_keys if k not in content]
        raise ValueError(f"Missing required keys in DeepSeek response: {missing}")
    
    # Validate vocabulary is a list
    if not isinstance(content['vocabulary'], list):
        raise ValueError("vocabulary must be a list")
    
    # Limit vocabulary items
    if len(content['vocabulary']) > config.MAX_VOCAB_ITEMS:
        content['vocabulary'] = content['vocabulary'][:config.MAX_VOCAB_ITEMS]
    
    # Validate each vocabulary item has required fields
    for item in content['vocabulary']:
        if not all(k in item for k in ['english', 'chinese', 'pinyin']):
            raise ValueError("Each vocabulary item must have 'english', 'chinese', 'pinyin'")
    
    # Validate opinion_texts has all three views
    if not all(k in content['opinion_texts'] for k in ['positive', 'negative', 'balanced']):
        raise ValueError("opinion_texts must have 'positive', 'negative', 'balanced'")
    
    # Validate discussion_questions is a list
    if not isinstance(content['discussion_questions'], list):
        raise ValueError("discussion_questions must be a list")
    
    return True

@retry(
    stop=stop_after_attempt(config.API_RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((Exception,)),
    before_sleep=lambda retry_state: print(f"Retry attempt {retry_state.attempt_number} after error: {retry_state.outcome.exception()}")
)
def generate_content_with_deepseek(topic):
    """Generate all content using DeepSeek API with retry logic"""
    print(f"[DeepSeek] Generating content for topic: {topic[:50]}...")
    
    prompt = f"""You are a Chinese language teaching assistant. Create learning materials about the topic: "{topic}"

Please generate a JSON response with the following structure:
{{
  "main_text": "A text in Simplified Chinese at HSK5 level about {topic}. Should be 250 characters long, natural and engaging.",
  "vocabulary": [
    {{"english": "English translation", "chinese": "Chinese word/phrase from the text", "pinyin": "pinyin with tone marks"}},
    // 10-15 items total - must be HSK5 level collocations or phrases are preferable, expressions taken directly from the main_text
  ],
  "opinion_texts": {{
    "positive": "A natural Chinese text (HSK5 level, 100-150 characters) giving a positive perspective on the main topic. Must naturally incorporate at least 5-6 vocabulary items from the list, but adjust them to fit naturally in context. Use some of the vocabulary taken from the first text.",
    "negative": "A natural Chinese text (HSK5 level, 100-150 characters) giving a critical/negative perspective on the main topic. Must naturally incorporate at least 5-6 vocabulary items from the list, but adjust them to fit naturally in context. Use some of the vocabulary taken from the first text.",
    "balanced": "A natural Chinese text (HSK5 level, 100-150 characters) giving a balanced perspective on the main topic. Must naturally incorporate at least 5-6 vocabulary items from the list, but adjust them to fit naturally in context. Use some of the vocabulary taken from the first text."
  }},
  "discussion_questions": [
    "Question 1 in Chinese (HSK5 level) - should prompt discussion, not just comprehension",
    "Question 2 in Chinese (HSK5 level) - should prompt discussion, not just comprehension",
    "Question 3 in Chinese (HSK5 level) - should prompt discussion, not just comprehension",
    "Question 4 in Chinese (HSK5 level) - should prompt discussion, not just comprehension"
  ]
}}

Important requirements:
1. All vocabulary items MUST come from the main_text
2. Vocabulary should be HSK5 level collocations and phrases (not single words)
3. Opinion texts should use some of the vocabulary taken from the first text but should sound natural - vocabulary can be adjusted to fit context
4. Discussion questions should encourage personal opinions and deeper thinking
5. Return ONLY valid JSON, no additional text"""

    try:
        print(f"[DeepSeek] Sending request to API...")
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a Chinese language teaching expert who creates engaging, natural content at HSK5 level. Always respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=45.0
        )
        
        print(f"[DeepSeek] Received response, parsing...")
        content_text = response.choices[0].message.content
        
        # Try to extract JSON if there's extra text
        json_match = re.search(r'\{.*\}', content_text, re.DOTALL)
        if json_match:
            content_text = json_match.group()
        
        # Parse JSON
        content = json.loads(content_text)
        
        print(f"[DeepSeek] JSON parsed successfully")
        
        # Validate structure
        validate_deepseek_response(content)
        
        print(f"[DeepSeek] Validation passed, returning content")
        return content
    
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parsing error: {str(e)}")
        print(f"[ERROR] Raw content: {content_text[:200]}...")
        raise
    except ValueError as e:
        print(f"[ERROR] Validation error: {str(e)}")
        raise
    except Exception as e:
        print(f"[ERROR] DeepSeek API Error: {type(e).__name__}: {str(e)}")
        raise

async def create_vocabulary_file_with_tts(vocabulary, topic, progress_callback=None):
    """Create tab-delimited vocabulary file with TTS audio tags and return audio files"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_topic_name = safe_filename(topic)
    filename = f"{safe_topic_name}_{timestamp}_vocabulary.txt"
    
    content = ""
    audio_files = {}
    
    total_items = len(vocabulary)
    
    # Generate TTS for all vocabulary items concurrently
    tts_tasks = []
    for item in vocabulary:
        tts_tasks.append(generate_tts_async(item['chinese'], use_chirp=False))
    
    # Await all TTS generations
    audio_results = await asyncio.gather(*tts_tasks, return_exceptions=True)
    
    for idx, (item, audio_data) in enumerate(zip(vocabulary, audio_results)):
        chinese_text = item['chinese']
        
        if progress_callback:
            await progress_callback(idx + 1, total_items)
        
        # Check if audio generation succeeded
        if isinstance(audio_data, Exception) or not audio_data:
            print(f"TTS failed for '{chinese_text}': {audio_data if isinstance(audio_data, Exception) else 'No data'}")
            # Add row without audio
            content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\n"
        else:
            # Create filename using MD5 hash
            hash_object = hashlib.md5(chinese_text.encode())
            audio_filename = f"tts_{hash_object.hexdigest()}.mp3"
            
            # Sanitize filename
            audio_filename = safe_filename(audio_filename)
            
            # Store audio data
            audio_files[audio_filename] = audio_data
            
            # Create Anki sound tag
            anki_tag = f"[sound:{audio_filename}]"
            
            # Add row with 4 columns: english, chinese, pinyin, audio_tag
            content += f"{item['english']}\t{item['chinese']}\t{item['pinyin']}\t{anki_tag}\n"
    
    return filename, content, audio_files

def create_zip_package(vocab_filename, vocab_content, audio_files, topic, timestamp):
    """Create a ZIP file containing vocabulary txt and all MP3 files"""
    safe_topic_name = safe_filename(topic)
    zip_filename = f"{safe_topic_name}_{timestamp}_anki_package.zip"
    zip_buffer = BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Sanitize vocabulary filename
        safe_vocab_filename = safe_filename(vocab_filename)
        
        # Add vocabulary text file
        zip_file.writestr(safe_vocab_filename, vocab_content.encode('utf-8'))
        
        # Add all audio files with sanitized names
        for audio_filename, audio_data in audio_files.items():
            safe_audio_filename = safe_filename(audio_filename)
            zip_file.writestr(safe_audio_filename, audio_data)
    
    zip_buffer.seek(0)
    
    # Check file size
    file_size = zip_buffer.getbuffer().nbytes
    if file_size > config.MAX_FILE_SIZE:
        raise ValueError(f"ZIP file too large: {file_size / 1024 / 1024:.1f}MB (max: {config.MAX_FILE_SIZE / 1024 / 1024}MB)")
    
    return zip_filename, zip_buffer


def create_html_document(topic, content, timestamp):
    """Create a beautiful HTML document with all learning materials"""
    safe_topic = safe_filename(topic)
    html_filename = f"{safe_topic}_{timestamp}_materials.html"
    
    # Build vocabulary table HTML
    vocab_rows = ""
    for i, item in enumerate(content['vocabulary'], 1):
        vocab_rows += f"""
        <tr>
            <td>{i}</td>
            <td class="chinese">{item['chinese']}</td>
            <td class="pinyin">{item['pinyin']}</td>
            <td>{item['english']}</td>
        </tr>
        """
    
    # Build discussion questions HTML
    questions_html = ""
    for i, question in enumerate(content['discussion_questions'], 1):
        questions_html += f"""
        <div class="question">
            <span class="question-number">{i}</span>
            <span class="question-text">{question}</span>
        </div>
        """
    
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chinese Learning Materials: {topic}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
            line-height: 1.8;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        
        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}
        
        .header h1 {{
            font-size: 2em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.2);
        }}
        
        .header .subtitle {{
            font-size: 0.9em;
            opacity: 0.9;
        }}
        
        .content {{
            padding: 40px;
        }}
        
        .section {{
            margin-bottom: 50px;
        }}
        
        .section-title {{
            font-size: 1.8em;
            color: #667eea;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #667eea;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .section-icon {{
            font-size: 1.2em;
        }}
        
        .main-text {{
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            padding: 30px;
            border-radius: 15px;
            font-size: 1.3em;
            line-height: 2;
            color: #2c3e50;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        
        .chinese {{
            font-size: 1.2em;
            font-weight: 600;
            color: #2c3e50;
        }}
        
        .pinyin {{
            color: #7f8c8d;
            font-style: italic;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            border-radius: 10px;
            overflow: hidden;
        }}
        
        thead {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }}
        
        th {{
            padding: 15px;
            text-align: left;
            font-weight: 600;
        }}
        
        tbody tr:nth-child(even) {{
            background: #f8f9fa;
        }}
        
        tbody tr:hover {{
            background: #e9ecef;
            transition: background 0.3s;
        }}
        
        td {{
            padding: 15px;
            border-bottom: 1px solid #dee2e6;
        }}
        
        .opinion-card {{
            background: white;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            border-left: 5px solid;
        }}
        
        .opinion-positive {{
            border-left-color: #2ecc71;
        }}
        
        .opinion-negative {{
            border-left-color: #e74c3c;
        }}
        
        .opinion-balanced {{
            border-left-color: #f39c12;
        }}
        
        .opinion-header {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 15px;
            font-size: 1.3em;
            font-weight: 600;
        }}
        
        .opinion-text {{
            font-size: 1.1em;
            line-height: 2;
            color: #2c3e50;
        }}
        
        .question {{
            background: #f8f9fa;
            padding: 20px;
            margin-bottom: 15px;
            border-radius: 10px;
            display: flex;
            gap: 15px;
            align-items: start;
            box-shadow: 0 3px 10px rgba(0,0,0,0.05);
        }}
        
        .question-number {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            width: 35px;
            height: 35px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            flex-shrink: 0;
        }}
        
        .question-text {{
            font-size: 1.1em;
            line-height: 1.8;
            color: #2c3e50;
        }}
        
        .footer {{
            background: #f8f9fa;
            padding: 30px;
            text-align: center;
            color: #6c757d;
            border-top: 1px solid #dee2e6;
        }}
        
        @media print {{
            body {{
                background: white;
                padding: 0;
            }}
            .container {{
                box-shadow: none;
            }}
        }}
        
        @media (max-width: 768px) {{
            .content {{
                padding: 20px;
            }}
            .header {{
                padding: 30px 20px;
            }}
            .main-text {{
                font-size: 1.1em;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎓 Chinese Learning Materials</h1>
            <div class="subtitle">Topic: {topic}</div>
            <div class="subtitle">Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
        </div>
        
        <div class="content">
           
            
            <!-- Vocabulary -->
            <div class="section">
                <h2 class="section-title">
                    <span class="section-icon">📚</span>
                    Vocabulary List
                </h2>
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Chinese</th>
                            <th>Pinyin</th>
                            <th>English</th>
                        </tr>
                    </thead>
                    <tbody>
                        {vocab_rows}
                    </tbody>
                </table>
            </div>
             <!-- Main Text -->
            <div class="section">
                <h2 class="section-title">
                    <span class="section-icon">📖</span>
                    Main Text
                </h2>
                <div class="main-text">{content['main_text']}</div>
            </div>
            
            <!-- Opinion Texts -->
            <div class="section">
                <h2 class="section-title">
                    <span class="section-icon">💭</span>
                    Different Perspectives
                </h2>
                
                <div class="opinion-card opinion-positive">
                    <div class="opinion-header">
                        <span>😊</span>
                        <span>Positive View</span>
                    </div>
                    <div class="opinion-text">{content['opinion_texts']['positive']}</div>
                </div>
                
                <div class="opinion-card opinion-negative">
                    <div class="opinion-header">
                        <span>🤔</span>
                        <span>Critical View</span>
                    </div>
                    <div class="opinion-text">{content['opinion_texts']['negative']}</div>
                </div>
                
                <div class="opinion-card opinion-balanced">
                    <div class="opinion-header">
                        <span>⚖️</span>
                        <span>Balanced View</span>
                    </div>
                    <div class="opinion-text">{content['opinion_texts']['balanced']}</div>
                </div>
            </div>
            
            <!-- Discussion Questions -->
            <div class="section">
                <h2 class="section-title">
                    <span class="section-icon">💬</span>
                    Discussion Questions
                </h2>
                {questions_html}
            </div>
        </div>
        
        <div class="footer">
            <p>Generated by Chinese Learning Bot 🤖</p>
            <p>HSK5 Level Materials</p>
        </div>
    </div>
</body>
</html>"""
    
    return html_filename, html_content

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        "欢迎! Enter your topic,detailed without being too long! 🎓\n\n"
        
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    user_id = update.effective_user.id
    reset_time = rate_limiter.get_reset_time(user_id)
    
    help_text = (
        "📖 **How to Use:**\n\n"
        "1. Send me any topic (max 100 characters)\n"
        "2. Wait 30-60 seconds for generation\n"
        "3. Receive comprehensive materials:\n"
        "   • Beautiful HTML document\n"
        "   • Vocabulary file with TTS tags\n"
        "   • 3 audio files (different perspectives)\n"
        "   • Discussion questions\n"
        "   • Complete ZIP package\n\n"
        "📦 **For Anki Import:**\n"
        "1. Download the ZIP file\n"
        "2. Extract MP3 files to your Anki collection.media folder\n"
        "3. Import the .txt file into Anki\n"
        "4. See Anki docs for your platform's media folder location\n\n"
        "⚡ **Rate Limit:** 5 requests per hour\n"
    )
    
    if reset_time > 0:
        help_text += f"⏱️ Your limit resets in {reset_time // 60} minutes\n\n"
    
    help_text += (
        "💡 **Example Topics:**\n"
        "• 社交媒体的影响\n"
        "• work-life balance\n"
        "• 环境保护\n"
        "• modern technology\n"
        "• 城市生活的压力"
    )
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle topic message and generate all materials"""
    user_id = update.effective_user.id
    topic_raw = update.message.text.strip()
    
    # Check rate limit
    if not rate_limiter.is_allowed(user_id):
        reset_time = rate_limiter.get_reset_time(user_id)
        await update.message.reply_text(
            f"⏱️ Rate limit reached!\n\n"
            f"You've used your 5 requests for this hour.\n"
            f"Please try again in {reset_time // 60} minutes.\n\n"
            f"This helps manage API costs. Thank you for understanding! 🙏"
        )
        return
    
    # Validate and sanitize topic
    try:
        topic = validate_topic(topic_raw)
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid topic: {str(e)}\n\nPlease try a different topic.")
        return
    
    # Send initial message with typing action
    await update.message.chat.send_action(action="typing")
    
    progress_msg = await update.message.reply_text(
        f"📚 Creating materials about '{topic}'...\n\n"
        f"⏳ Progress: 0/5\n"
        f"⬜⬜⬜⬜⬜\n"
        f"Initializing..."
    )
    
    # Progress tracking
    async def update_progress(step, message):
        progress_bar = "🟩" * step + "⬜" * (5 - step)
        try:
            await progress_msg.edit_text(
                f"📚 Creating materials about '{topic}'...\n\n"
                f"⏳ Progress: {step}/5\n"
                f"{progress_bar}\n"
                f"{message}"
            )
        except:
            pass  # Ignore edit errors
    
    try:
        # Step 1: Generate content with DeepSeek
        await update_progress(1, "🤖 Generating content with AI...")
        await update.message.chat.send_action(action="typing")
        
        print(f"[Bot] Starting content generation for user {user_id}, topic: {topic[:50]}")
        
        try:
            content = generate_content_with_deepseek(topic)
        except Exception as e:
            print(f"[Bot] Content generation failed: {type(e).__name__}: {str(e)}")
            raise
        
        if not content:
            await update.message.reply_text(
                "❌ Failed to generate content. Please try again with a different topic.\n\n"
                "If the problem persists, the topic might be too complex or controversial."
            )
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_topic = safe_filename(topic)
        
        # Step 2: Create and send HTML document
        await update_progress(2, "📄 Creating HTML document...")
        
        html_filename, html_content = create_html_document(topic, content, timestamp)
        html_file = BytesIO(html_content.encode('utf-8'))
        html_file.name = html_filename
        
        await update.message.reply_document(
            document=html_file,
            filename=html_filename,
            caption="HTML"
        )
        
       
        
        # Step 3: Create vocabulary file with TTS
        await update_progress(3, "🎵 Generating TTS audio for vocabulary...")
        await update.message.chat.send_action(action="record_voice")
        
        async def vocab_progress(current, total):
            if current % 3 == 0:  # Update every 3 items
                await update_progress(3, f"🎵 Generating TTS audio... ({current}/{total})")
        
        vocab_filename, vocab_content, audio_files = await create_vocabulary_file_with_tts(
            content['vocabulary'], 
            safe_topic,
            progress_callback=vocab_progress
        )
        
        if not audio_files:
            await update.message.reply_text("⚠️ Warning: Could not generate TTS audio for vocabulary.")
        
        # Step 4: Create ZIP package (now includes HTML file)
        await update_progress(4, "📦 Creating complete package...")
        
        try:
            # Get HTML content for ZIP
            html_filename, html_content = create_html_document(topic, content, timestamp)
            
            # Create enhanced ZIP with HTML
            zip_filename = f"{safe_topic}_{timestamp}_complete_package.zip"
            zip_buffer = BytesIO()
            
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                # Add vocabulary text file
                safe_vocab_filename = safe_filename(vocab_filename)
                zip_file.writestr(safe_vocab_filename, vocab_content.encode('utf-8'))
                
                # Add HTML document
                safe_html_filename = safe_filename(html_filename)
                zip_file.writestr(safe_html_filename, html_content.encode('utf-8'))
                
                # Add all audio files
                for audio_filename, audio_data in audio_files.items():
                    safe_audio_filename = safe_filename(audio_filename)
                    zip_file.writestr(safe_audio_filename, audio_data)
            
            zip_buffer.seek(0)
            
            # Check file size
            file_size = zip_buffer.getbuffer().nbytes
            if file_size > config.MAX_FILE_SIZE:
                raise ValueError(f"ZIP file too large: {file_size / 1024 / 1024:.1f}MB")
            
            # Send ZIP file
            zip_buffer.name = zip_filename
            await update.message.reply_document(
                document=zip_buffer, 
                filename=zip_filename,
                caption=f"📦 ZIP "
                       
            )
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {str(e)}")
            # Send files separately if ZIP is too large
            html_file = BytesIO(html_content.encode('utf-8'))
            html_file.name = html_filename
            await update.message.reply_document(document=html_file, filename=html_filename)
            
            vocab_file = BytesIO(vocab_content.encode('utf-8'))
            vocab_file.name = vocab_filename
            await update.message.reply_document(document=vocab_file, filename=vocab_filename)
        
        # Step 5: Generate and send opinion texts with audio
        await update_progress(5, "🎤 Generating opinion texts with audio...")
        
        perspectives = [
            ("positive", "Positive View", "😊"),
            ("negative", "Critical View", "🤔"),
            ("balanced", "Balanced View", "⚖️")
        ]
        
        # Generate all opinion audio concurrently
        opinion_tasks = []
        for key, name, emoji in perspectives:
            opinion_tasks.append(generate_tts_async(content['opinion_texts'][key], use_chirp=True))
        
        opinion_audios = await asyncio.gather(*opinion_tasks, return_exceptions=True)
        
        for (key, name, emoji), audio_data in zip(perspectives, opinion_audios):
            opinion_text = content['opinion_texts'][key]
            
            # Send text
            await update.message.reply_text(f"{emoji} **{name}:**\n\n{opinion_text}", parse_mode='Markdown')
            
            # Send audio if generation succeeded
            if not isinstance(audio_data, Exception) and audio_data:
                audio_filename = f"{safe_topic}_{timestamp}_{key}.mp3"
                audio_file = BytesIO(audio_data)
                audio_file.name = audio_filename
                await update.message.reply_audio(audio=audio_file, filename=audio_filename)
            else:
                await update.message.reply_text(f"⚠️ Could not generate audio for {name}.")
        
        # Send discussion questions (keeping in chat for quick reference)
        questions_text = "💬 **Discussion Questions:**\n\n"
        for i, question in enumerate(content['discussion_questions'], 1):
            questions_text += f"{i}. {question}\n"
        
        await update.message.reply_text(questions_text, parse_mode='Markdown')
        
        # Final success message
        await progress_msg.edit_text(
            f"✅ **Complete!**\n\n"
            
        )
        
        await update.message.reply_text(
           
            "Need help? Send /help"
        )
        
    except Exception as e:
        error_msg = str(e)
        await update.message.reply_text(
            f"❌ **Error occurred:**\n{error_msg}\n\n"
            f"Please try again with a different topic or contact support if the issue persists.\n\n"
            f"Suggestions:\n"
            f"• Try a simpler or more specific topic\n"
            f"• Avoid very long or complex phrases\n"
            f"• Check that your topic is appropriate"
        )
        print(f"Error for user {user_id}, topic '{topic}': {error_msg}")

def main():
    """Start the bot"""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment variables")
        return
    
    if not DEEPSEEK_API_KEY:
        print("Error: DEEPSEEK_API_KEY not found in environment variables")
        return
    
    print("Bot configuration:")
    print(f"- Rate limit: {config.RATE_LIMIT_REQUESTS} requests per {config.RATE_LIMIT_WINDOW // 3600} hour(s)")
    print(f"- Max topic length: {config.MAX_TOPIC_LENGTH} characters")
    print(f"- API retry attempts: {config.API_RETRY_ATTEMPTS}")
    print(f"- Content level: HSK5 (250 character main text)")
    print(f"- Vocabulary focus: Collocations and phrases")
    
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic))
    
    # Run the bot
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
