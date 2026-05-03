# vocab-news-bot

Mandarin Chinese news podcast bot for personal use.

## What it does

Send `/news` → pick a topic → receive a ~5 min Mandarin HSK4 podcast as MP3.
After the podcast, 5 relevant collocations appear as buttons.
Tap any button → saves to Google Sheet (col A: Chinese, col B: English, col C: date).

Topics:
- 📈 Economic — top 2-3 business/economy stories
- 🌐 Political — biggest geopolitical or US story
- 🇨🇳 China — latest China news
- 🇪🇬 Egypt — latest Egypt news

News is scraped live from BBC/Reuters/NYT RSS feeds (≤24h old, no API key needed).
Vocab is pulled from your Google Sheet (most recent 8 items from column A).

---

## Local setup (Windows — first time)

```powershell
cd C:\Users\John\YandexDisk\Python\innovative vocab\AI magic\yandex\vocab_news_bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your tokens:
```
TELEGRAM_BOT_TOKEN=...
DEEPSEEK_API_KEY=...
```

Install ffmpeg if not already: https://ffmpeg.org/download.html  (already at C:\ffmpeg on your machine)

---

## Deploy to Yandex VM

### 1. Push to GitHub
```powershell
cd "C:\Users\John\YandexDisk\Python\innovative vocab\AI magic\yandex\vocab_news_bot"
git init
git remote add origin https://github.com/johnaaiton-art/vocab-news-bot.git
git add .
git commit -m "initial commit"
git push -u origin main
```

### 2. Clone on VM
```bash
ssh -i C:\Users\John\.ssh\id_rsa yc-user@178.154.198.14
cd ~
git clone https://github.com/johnaaiton-art/vocab-news-bot.git vocab-news-bot
cd ~/vocab-news-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Install ffmpeg on VM (if not already):
```bash
sudo apt-get install -y ffmpeg
```

### 3. Upload google-creds.json (from PowerShell on your PC)
```powershell
scp -i C:\Users\John\.ssh\id_rsa "C:\Users\John\YandexDisk\Python\innovative vocab\AI magic\yandex\google-creds.json" yc-user@178.154.198.14:~/vocab-news-bot/google-creds.json
```

### 4. Create .env on VM
```bash
cat > ~/vocab-news-bot/.env << 'EOF'
TELEGRAM_BOT_TOKEN=your_token_here
DEEPSEEK_API_KEY=your_deepseek_key_here
EOF
```

### 5. Create systemd service
```bash
cat << 'EOF' | sudo tee /etc/systemd/system/vocab-news-bot.service
[Unit]
Description=Vocab News Bot (Chinese Mandarin podcast)
After=network.target

[Service]
Type=simple
User=yc-user
WorkingDirectory=/home/yc-user/vocab-news-bot
EnvironmentFile=/home/yc-user/vocab-news-bot/.env
ExecStart=/home/yc-user/vocab-news-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vocab-news-bot.service
sudo systemctl start vocab-news-bot.service
sudo systemctl status vocab-news-bot.service
```

### 6. Check logs
```bash
sudo journalctl -u vocab-news-bot.service -f
```

---

## Update workflow (after making changes on PC)

```powershell
# On PC:
cd "C:\Users\John\YandexDisk\Python\innovative vocab\AI magic\yandex\vocab_news_bot"
git add .
git commit -m "your message"
git push origin main
```

```bash
# On VM:
cd ~/vocab-news-bot
git pull origin main
sudo systemctl restart vocab-news-bot.service
sudo systemctl status vocab-news-bot.service
```

---

## Google Sheet format

Sheet: **Collocations** → tab: **Chinese**
- Column A: Chinese collocation (搭配)
- Column B: English translation
- Column C: Date (YYYY-MM-DD)

The bot reads column A for vocab input and writes A+B+C when you save a collocation.
