# Live deployment and messaging checklist

Complete this only after the current code is committed and deployed.

## 1. Supabase

- Run `supabase/migrations/002_broker_confirmations.sql`.
- Run `supabase/migrations/003_storage_setup.sql`.
- Confirm `property-images` and `property-documents` are public buckets.
- Put the server-only service-role key in Railway as `SUPABASE_SERVICE_ROLE_KEY`.
- Never expose the service-role key in browser JavaScript or a public repository.

## 2. Railway variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `GROQ_API_KEY`
- `BROKER_TOKEN` (long random value)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_ACCESS_TOKEN` (permanent token)
- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_APP_SECRET`
- `WHATSAPP_BUSINESS_ACCOUNT_ID`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`

## 3. Smoke checks

- Open `/health` and expect `{"status":"ok"}`.
- Open `/broker`, enter `BROKER_TOKEN`, and load leads.
- Add a property and confirm coordinates and POI distances are stored.
- Upload, reorder, and delete property images.
- Edit the address and confirm coordinates/distances change.
- Upload a CSV and wait for the visible success/failure summary.

## 4. Telegram

- Register `/webhook/telegram` using `TELEGRAM_WEBHOOK_SECRET` as Telegram's `secret_token`.
- Send `/start`, complete onboarding, search, shortlist, and request a visit.
- Confirm repeated updates use the hosted webhook without running the polling bot.

## 5. WhatsApp

- Register `/webhook/whatsapp` and subscribe to `messages`.
- Use the configured `WHATSAPP_VERIFY_TOKEN` during Meta verification.
- Confirm Meta POST requests pass `X-Hub-Signature-256` validation.
- Request a visit as a buyer and verify the broker receives YES/NO.
- Reply YES and verify one meeting becomes confirmed, without a duplicate.
- Reply NO and verify the buyer can send a replacement time.
- Reschedule a confirmed meeting and verify buyer WhatsApp plus updated `.ics` email.

## 6. Final audit

- Check Railway logs for webhook, email, Storage, geocoder, or POI errors.
- Confirm sold properties no longer appear in chat or public browse results.
- Rotate any temporary or previously exposed credentials.
