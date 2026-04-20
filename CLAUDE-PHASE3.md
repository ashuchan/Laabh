# CLAUDE-PHASE3.md — Whisper Pipeline & Advanced Signal Intelligence

## Overview
Phase 3 adds **audio intelligence**: real-time and batch transcription of YouTube
financial channels and podcasts via OpenAI Whisper, plus advanced signal convergence
that combines news, TV, and price action into high-confidence recommendations.

## Prerequisites
- Phase 1 + Phase 2 fully functional
- NVIDIA GPU with 8GB+ VRAM (RTX 3060 minimum) for Whisper large-v3
- OR use Whisper API (cloud) if no local GPU available
- At least 2 weeks of signal + analyst data for convergence scoring

## New Components

### 1. Whisper Transcription Pipeline (`src/whisper_pipeline/`)
```
src/whisper_pipeline/
├── __init__.py
├── stream_recorder.py     # yt-dlp live stream audio capture
├── vod_downloader.py      # Download completed YouTube videos for batch processing
├── transcriber.py         # Whisper model wrapper (local GPU or API)
├── chunk_processor.py     # Split transcripts into processable chunks
├── financial_filter.py    # Filter chunks: only keep those with stock mentions
├── podcast_collector.py   # RSS-based podcast discovery and download
└── pipeline.py            # Orchestrates: download → transcribe → extract → store
```

#### Live Stream Recording (Market Hours Only)
1. On market open (9:10 AM IST), start recording from configured YouTube channels
2. Use `yt-dlp` with `--live-from-start -f bestaudio` piped to segmented output
3. Each segment = 60 seconds of audio saved as `.webm` file
4. Segments written to `/data/whisper/live/{channel}/{date}/chunk_{timestamp}.webm`
5. On market close (3:35 PM IST), stop all recorders gracefully
6. Cleanup: delete audio files older than 7 days

Implementation detail — use subprocess with yt-dlp:
```python
import subprocess
proc = subprocess.Popen([
    "yt-dlp",
    "--live-from-start",
    "-f", "bestaudio",
    "--downloader", "ffmpeg",
    "--downloader-args", "ffmpeg:-f segment -segment_time 60",
    "-o", f"/data/whisper/live/{channel}/chunk_%(epoch)s.%(ext)s",
    stream_url
], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
```

For robustness: wrap in a supervisor loop that restarts on exit (streams drop frequently).

#### Batch VOD Processing (Post-Market)
1. At 4:00 PM IST, find today's completed streams from each YouTube channel
2. Use `youtube-transcript-api` to pull auto-generated captions first (free, fast)
3. If captions are unavailable or low quality: download audio via yt-dlp, transcribe with Whisper
4. Store full transcript in `transcription_jobs` table

#### Whisper Configuration
```python
# Local GPU mode (preferred):
import whisper
model = whisper.load_model("large-v3", device="cuda")
result = model.transcribe(
    audio_path,
    language="hi",           # or None for auto-detect
    task="transcribe",       # not "translate" — keep original language
    word_timestamps=True,    # for precise segment alignment
    condition_on_previous_text=True,
    initial_prompt="Stock market analysis. Nifty, Sensex, BSE, NSE. "
                   "RELIANCE, TCS, HDFC, INFOSYS, ICICI, ADANI, TATA."
                   # Prime the model with financial vocabulary
)

# Cloud API mode (if no GPU):
from openai import OpenAI
client = OpenAI()
with open(audio_path, "rb") as f:
    result = client.audio.transcriptions.create(
        model="whisper-1", file=f, language="hi",
        prompt="Indian stock market analysis..."
    )
```

#### Financial Filter (Cost Optimization)
Most TV audio is noise (ads, anchor banter, unrelated segments). Filter before LLM:
1. Run Whisper on 60s chunk → get transcript text
2. Check against keyword set: stock symbols (all 500+ from instruments table),
   financial terms ("target", "buy", "sell", "bullish", "bearish", "nifty", "sensex",
   "resistance", "support", "breakout", "stop loss", Hindi equivalents)
3. If NO keywords found → skip LLM extraction entirely (saves 80% of API costs)
4. If keywords found → send to Claude for structured extraction
5. Store filter result in `transcript_chunks.contains_stock_mention`

#### Podcast Support
```python
# Discover podcasts via RSS:
PODCAST_FEEDS = [
    {"name": "The Market Podcast", "url": "https://feed.podbean.com/marketpodcast/feed.xml"},
    {"name": "Paisa Vaisa", "url": "...",  "language": "hi-en"},
    {"name": "Marcellus Podcast", "url": "..."},
]
# Download → Whisper → Extract. Same pipeline as YouTube VOD.
```

### 2. Signal Convergence Engine (`src/analytics/convergence.py`)

The convergence engine is the core intelligence layer. It answers: "Should I actually
pay attention to this signal?"

#### Convergence Algorithm
When a new signal is created:
1. Find all other active signals for the SAME instrument within the last 24 hours
2. Check agreement: do multiple sources agree on direction (BUY/SELL)?
3. Calculate convergence_score:
   ```
   score = 0
   for each agreeing signal:
       if source_type is different from existing → score += 2  (cross-source)
       if source_type is same → score += 1                    (same source confirmation)
       if analyst credibility > 0.6 → score += 1              (trusted analyst bonus)
   ```
4. Update `signals.convergence_score` for all related signals
5. Link them via `signals.related_signal_ids`
6. If convergence_score >= 4 → create HIGH priority notification

#### Technical Confirmation Layer
Before surfacing any signal, cross-check with price action:
```python
async def validate_signal_with_technicals(signal, price_data):
    """
    Returns (is_confirmed, conflicts, technical_summary)
    """
    rsi = calculate_rsi(price_data, period=14)
    macd_line, signal_line = calculate_macd(price_data)
    sma_20 = price_data['close'].rolling(20).mean().iloc[-1]
    sma_50 = price_data['close'].rolling(50).mean().iloc[-1]
    current_price = price_data['close'].iloc[-1]
    
    conflicts = []
    confirmations = []
    
    if signal.action == 'BUY':
        if rsi > 70: conflicts.append("RSI overbought (>70)")
        if rsi < 30: confirmations.append("RSI oversold — good entry")
        if current_price > sma_20 > sma_50: confirmations.append("Price above 20 & 50 SMA")
        if macd_line > signal_line: confirmations.append("MACD bullish crossover")
    
    elif signal.action == 'SELL':
        if rsi < 30: conflicts.append("RSI oversold (<30)")
        if rsi > 70: confirmations.append("RSI overbought — supports sell")
        if current_price < sma_20: confirmations.append("Price below 20 SMA")
    
    is_confirmed = len(confirmations) > len(conflicts)
    return is_confirmed, conflicts, confirmations
```

### 3. Smart Notification System Updates

#### Notification Priority Rules
- **Critical**: convergence_score >= 5, OR watchlist stock hits price alert
- **High**: convergence_score >= 3, OR trusted analyst (credibility > 0.7) gives call
- **Medium**: any new signal on watchlist stock
- **Low**: general market news, non-watchlist signals

#### Watchlist-Focused Analysis
When a signal is generated for a stock in the user's watchlist:
1. Enrich the notification with additional context:
   - Current holding status (if any position exists)
   - Technical indicators snapshot (RSI, MACD, SMA)
   - Recent signals history for this stock (last 7 days)
   - Analyst track record for this specific stock
2. If user has a target buy price set on watchlist and current price is near it:
   create a CRITICAL notification: "RELIANCE near your target buy of ₹2,400"

#### Telegram Message Formats
```
🔴 SELL Signal — TATAMOTORS @ ₹782
Convergence: 4/5 (3 sources agree)
├─ CNBC-TV18: Sudarshan Sukhani (credibility: 72%)
├─ Moneycontrol article: "Tata Motors faces headwinds"
├─ RSI: 74 (overbought) ✓ confirms
Target: ₹740 | Stop Loss: ₹805
Your holding: 100 qty @ ₹720 (P&L: +₹6,200)
→ /trade sell TATAMOTORS 100
```

### 4. Scheduler Updates
Add to APScheduler:
- `start_live_recorders` — 9:10 AM IST (Mon-Fri, non-holidays)
- `stop_live_recorders` — 3:35 PM IST
- `process_whisper_chunks` — every 2 min during market hours (process new audio chunks)
- `batch_vod_transcription` — 4:00 PM IST (process completed YouTube streams)
- `run_convergence_check` — every 15 min during market hours
- `update_convergence_scores` — triggered on each new signal (event-driven)

## Storage Management
- Audio chunks: 60s of audio ≈ 1 MB. 4 channels × 6 hours = ~1.4 GB/day
- Keep 7 days of audio → ~10 GB rolling. Auto-delete older files.
- Transcripts in DB: ~500 KB/day. Negligible.
- Separate data partition: mount `/data/whisper` on a secondary drive if needed

## Testing Phase 3
- `pytest tests/test_whisper.py` — test transcription with sample audio
- `pytest tests/test_convergence.py` — test convergence scoring with mock signals
- `pytest tests/test_technicals.py` — test RSI, MACD, SMA calculations
- Manual: record 5 min of CNBC live → verify transcript → verify signal extraction

## GPU Requirements
- Whisper large-v3: ~6 GB VRAM, processes 60s audio in ~15 seconds on RTX 3060
- 4 channels × 60 chunks/hour = 240 chunks/hour → needs ~1 hour of GPU time per hour
- This is tight on a single GPU. Solutions:
  a. Use `medium` model (3 GB VRAM, 2x faster, 90% accuracy) during live
  b. Use `large-v3` only for batch VOD processing (better accuracy, no time pressure)
  c. Run financial_filter BEFORE transcription: skip non-speech segments entirely
  d. Only transcribe segments where voice activity is detected (WebRTC VAD)

## Rules for Claude Code
- All rules from Phase 1 + Phase 2 still apply
- Whisper pipeline must gracefully handle: stream drops, audio corruption, 
  GPU OOM errors, yt-dlp version changes
- Audio processing must never block the main event loop (use process pool executor)
- All audio files must be cleaned up after processing (7-day retention)
- Technical indicators must match standard financial definitions exactly
- Convergence scores must be recalculated atomically (no partial updates)
- Live recorder processes must be supervised: auto-restart on crash
