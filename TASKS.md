# Real Estate AI Agent — Master Task List
> Everything to complete before building UI and hosting permanently.
> Status: [x] done | [~] in progress | [ ] pending | [!] needs your action

---

## CURRENT SYSTEM STATE
| Component | Status | Notes |
|-----------|--------|-------|
| Supabase DB | [x] | 110 props, 1 broker, sessions, leads tables |
| Property images | [x] | All 110 assigned by price tier (Unsplash CDN) |
| Embeddings | [x] | ivfflat index, 384-dim all-MiniLM-L6-v2 |
| RAG search | [x] | Hybrid SQL + pgvector, tested working |
| Groq LLM | [x] | llama-3.1-8b-instant, intent extractor + agent |
| Lead capture | [x] | Saves name+phone+area+budget to Supabase |
| Broker coverage | [x] | 32 Lucknow areas covered |
| Gmail SMTP | [x] | Credentials set in .env |
| Telegram bot code | [x] | Onboarding flow + voice support written |
| WhatsApp code | [x] | Outbound notifier written |
| Telegram webhook | [x] | /webhook/telegram endpoint in main.py |
| WhatsApp webhook | [x] | /webhook/whatsapp endpoint in main.py |
| Deployment config | [x] | Procfile + railway.toml created |

---

## PHASE 1 — Tests (Local, before deploy)

### Task 1.1 — Email Notification Test
**Goal:** Confirm emails actually arrive at temp2saubhagya@gmail.com
**How:** `python scripts/test_email.py` — sends a fake lead alert + buyer confirm
**Verify:** Check Gmail inbox for 2 emails
**Status:** [ ] — run when ready, check inbox

### Task 1.2 — Telegram Bot Test
**Goal:** Full onboarding + search + lead capture via Telegram
**How:**
1. Start server: `uvicorn api.main:app --port 8000`
2. Start bot: `python interfaces/telegram_bot.py`
3. Open @helper_panda_realesatet2bot → /start
4. Complete onboarding (name → phone → email)
5. Send: "3 BHK in Gomti Nagar under 1.5 crore near metro"
6. Reply: "I want to visit, book a site visit"
7. Send name + phone (should pre-fill from profile)
**Verify:** Lead appears in Supabase leads table, emails received
**Status:** [ ] — needs bot process running

### Task 1.3 — WhatsApp Permanent Token
**Goal:** Replace the 24hr temporary token with one that never expires
**How:** Follow steps in WHATSAPP_PERMANENT_TOKEN.md (see below)
**Status:** [!] NEEDS YOUR ACTION

### Task 1.4 — WhatsApp Outbound Test
**Goal:** Confirm broker gets WhatsApp alert when a lead is captured
**How:** After getting permanent token, trigger a test lead via Telegram
**Verify:** WhatsApp message received on 9936659513
**Status:** [ ] — after Task 1.3

---

## PHASE 2 — Deployment on Railway (Free Tier)

### Task 2.1 — Push Code to GitHub
**How:**
```
git add -A -- ':!.env'
git commit -m "feat: add WhatsApp, onboarding, webhooks, deployment config"
git push origin main
```
**Status:** [ ]

### Task 2.2 — Create Railway Project
**How:**
1. Go to railway.app → Sign up with GitHub
2. New Project → Deploy from GitHub repo → select this repo
3. Railway auto-detects Python + Procfile
**Status:** [!] NEEDS YOUR ACTION

### Task 2.3 — Set Environment Variables on Railway
**How:** Railway dashboard → your project → Variables tab → add all from .env
**Variables to add:**
```
SUPABASE_URL
SUPABASE_KEY
GROQ_API_KEY
TELEGRAM_BOT_TOKEN
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_ACCESS_TOKEN
WHATSAPP_VERIFY_TOKEN
WHATSAPP_BUSINESS_ACCOUNT_ID
```
**Status:** [ ] — after Task 2.2

### Task 2.4 — Verify Deployment
**How:** Railway shows a public URL like `https://real-estate-bot-production.up.railway.app`
Open it → should see Riya chat UI
Hit `/health` → should return `{"status":"ok"}`
**Status:** [ ]

### Task 2.5 — Set Telegram Webhook (permanent)
**How:** Run this once after deployment (replace URL with your Railway URL):
```
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR-RAILWAY-URL/webhook/telegram"
```
_(Replace `$TELEGRAM_BOT_TOKEN` with your actual token from .env)_
After this: stop the local `python interfaces/telegram_bot.py` — the hosted server handles it
**Status:** [ ] — after Task 2.4

### Task 2.6 — Set WhatsApp Webhook (permanent)
**How:**
1. Meta Dashboard → RealEstateBot → WhatsApp → Configuration → Webhooks
2. Callback URL: `https://YOUR-RAILWAY-URL/webhook/whatsapp`
3. Verify Token: `realestate_webhook_2026`
4. Click Verify → subscribe to `messages`
**Status:** [ ] — after Task 2.4

---

## PHASE 3 — Broker Admin UI

### Task 3.1 — Leads Dashboard Page
- Table: customer name, phone, area, BHK, budget, status, date
- Status dropdown (new → contacted → visiting → closed)
- Click row → full lead detail

### Task 3.2 — Properties Manager
- List all 110 properties with thumbnail
- Toggle availability (available / sold / rented)
- Edit price

### Task 3.3 — Basic Analytics
- Leads this week / this month
- Conversion rate
- Top areas by interest

**Stack:** FastAPI + Jinja2 HTML templates (no extra framework, stays free)

---

## PHASE 4 — Buyer Web UI Polish

### Task 4.1 — Property Cards
- Image gallery carousel
- Price, BHK, area clearly shown
- "Book Site Visit" button

### Task 4.2 — Mobile Responsive
- Current UI works on desktop, needs mobile polish

---

## WHAT NEEDS YOUR ACTION RIGHT NOW

### 1. WhatsApp Permanent Token (do this first)

1. Go to **business.facebook.com**
2. Settings (gear, bottom left) → **Users** → **System Users** → **Add**
3. Name: `RealEstateBotSystem`, Role: `Admin` → Create
4. Click the new user → **Add Assets** → Apps → check `RealEstateBot` → Save
5. Click **Generate Token** → App: `RealEstateBot`
6. Permissions: check `whatsapp_business_messaging` AND `whatsapp_business_management`
7. **Generate Token** → copy the token (it says "never expires")
8. Open `.env` → replace `WHATSAPP_ACCESS_TOKEN=...` with the new token
9. Tell me when done → I'll verify it works

### 2. Run Telegram test (after server + bot are started)
```powershell
# Terminal 1 — API server
.\venv\Scripts\python.exe -m uvicorn api.main:app --port 8000

# Terminal 2 — Telegram bot
.\venv\Scripts\python.exe interfaces/telegram_bot.py
```
Then message @helper_panda_realesatet2bot on Telegram.

---

## SUGGESTED NEXT ORDER
1. [YOU] Get WhatsApp permanent token
2. [YOU] Start server + bot, test Telegram
3. [ME] Once you confirm Telegram works → git commit + push
4. [YOU] Create Railway account, connect repo
5. [ME] Guide Railway env var setup
6. [YOU] Deploy → share Railway URL
7. [ME] Set Telegram webhook → test hosted bot
8. [YOU] Set WhatsApp webhook in Meta dashboard
9. [ME] Build broker admin UI
10. [ME] Polish buyer web UI
