# Deployment Guide — going live (free tier)

Goal: a public HTTPS URL so Telegram + WhatsApp webhooks work and the bot is actually live.
Deploy config already present: `Procfile`, `railway.toml`, `.python-version` (3.10).

---

## ⚠️ READ FIRST — the one architecture decision: embeddings & torch

The bot embeds every search query locally with `sentence-transformers` + **torch** (~1–2 GB).
On a free tier this often fails to build or runs out of memory (512 MB).

**Pick one before deploying:**

| Option | What | Trade-off |
|--------|------|-----------|
| **A. Hosted embeddings (recommended)** | Swap `embed_text()` to call a free embedding API for the SAME model (`all-MiniLM-L6-v2`, 384-dim) — e.g. HuggingFace Inference API. Drop torch from `requirements.txt`. | Tiny image, fast cold start, fits free tier. Needs a free HF token. Vectors stay identical so existing data still matches. |
| **B. Heavier host** | Keep local torch, deploy on **Render** (free, 512 MB but slower builds) or **HuggingFace Spaces** (handles torch natively). | No code change, but bigger/slower; may still OOM on the smallest tiers. |

> If unsure, do **A** — it's the clean free-tier path. (Ask me and I'll implement the HF-Inference swap; ~20 lines in `embeddings/embedding_model.py`, no change to stored data.)

---

## Step 1 — Push (already done)
Code is on `github.com/sarcasticpanda/real-estate-ai-agent` (main). `.env` is gitignored — never pushed.

## Step 2 — Create the Railway project
1. railway.app → sign in with GitHub
2. **New Project → Deploy from GitHub repo** → pick `sarcasticpanda/real-estate-ai-agent`
3. Railway auto-detects `railway.toml` (nixpacks, healthcheck `/health`)

## Step 3 — Set environment variables
Railway → your service → **Variables** → add each key below, copying the **value from your local `.env`**
(do NOT paste secrets anywhere public):

```
SUPABASE_URL
SUPABASE_KEY
GEMINI_API_KEY
GROQ_API_KEY
OPENROUTER_API_KEY
BROKER_TOKEN
TELEGRAM_BOT_TOKEN
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
WHATSAPP_ACCESS_TOKEN          ← see Step 6 (dev token expires in ~24h)
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_VERIFY_TOKEN
WHATSAPP_BUSINESS_ACCOUNT_ID
TARGET_CITY
TARGET_AREAS
```
Plus, if you choose embedding Option A: `HF_API_TOKEN`.

## Step 4 — Deploy & verify
- Railway builds and gives a URL like `https://real-estate-ai-agent-production.up.railway.app`
- Open `<URL>/health` → should return `{"status":"ok"}`
- Open `<URL>/` (buyer chat) and `<URL>/broker` (dashboard — paste your `BROKER_TOKEN`)

## Step 5 — Point Telegram at the deployment
Run once (replace placeholders):
```
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<RAILWAY-URL>/webhook/telegram"
```
Then stop any local `interfaces/telegram_bot.py` polling — the hosted server now handles Telegram.
Verify: `curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"`

## Step 6 — WhatsApp (the headline channel)
WhatsApp is code-complete and verified locally; it just needs the public URL.
1. **Permanent token** (dev token rotates every ~24h): business.facebook.com → Settings → System Users →
   add a System User (Admin) → assign the app → **Generate Token** with `whatsapp_business_messaging` +
   `whatsapp_business_management` → put it in Railway `WHATSAPP_ACCESS_TOKEN`.
2. **Webhook**: Meta dashboard → WhatsApp → Configuration → Callback URL = `https://<RAILWAY-URL>/webhook/whatsapp`,
   Verify Token = your `WHATSAPP_VERIFY_TOKEN` → **Verify and Save** → subscribe to `messages`.
3. **Test recipients** (dev mode only messages verified numbers): WhatsApp → API Setup → "To" → Manage →
   add your phone. (To message anyone, the app needs Meta review later.)
4. Send yourself a message from the test number, then reply — the bot should respond.

## Step 7 — Post-deploy checks
- [ ] `/health` ok
- [ ] Web chat returns properties
- [ ] Telegram bot replies (via webhook)
- [ ] WhatsApp: verified-number round trip works
- [ ] Broker dashboard loads with the token
- [ ] A booked visit shows under `/broker/meetings`

---

## Production caveats (known, plan for them)
- **LLM rate limits** — free Gemini (daily requests) + Groq (daily tokens). Fine for light traffic;
  under load, add the OpenRouter fallback or a paid tier. Each turn = 2–3 LLM calls.
- **WhatsApp dev mode** — limited to test recipients + 24h token until Meta business verification.
- **n8n workflows** — if used, their webhook URLs must point at the deployment, not localhost.
- **Embeddings** — see the Option A/B decision above; don't skip it.
- **Secrets** — only in Railway Variables and local `.env`; never commit.
