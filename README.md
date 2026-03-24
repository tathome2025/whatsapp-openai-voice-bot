# WhatsApp Voice Bot (Meta Cloud API + OpenAI)

這個專案提供可部署的 WhatsApp 語音助手，包含 Web Admin 後台。

核心能力：
- 收取 WhatsApp 語音訊息（Meta Cloud API Webhook）
- OpenAI STT 轉文字、生成回覆、TTS 回語音
- 白名單控制（只回應白名單用戶）
- 每位用戶獨立語音偏好與語言偏好
- 對話文字記錄（雙向）
- 記憶功能（用戶可要求記下/讀出記憶）
- Admin 可為指定用戶植入記憶
- Web Admin 登入與管理

## 1. 專案結構

```text
api/index.py
app/main.py
app/config.py
app/whatsapp.py
app/openai_client.py
app/voice_store.py
app/language_store.py
app/db.py
app/admin_auth.py
.env.example
requirements.txt
vercel.json
```

## 2. 環境需求

- Python 3.10+
- Meta Developer App + WhatsApp Cloud API
- OpenAI API Key

## 3. 安裝與啟動

```bash
cd /Users/TY/whatsapp-openai-voice-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 4. Meta Webhook 設定

- Callback URL: `https://<your-domain>/webhook`
- Verify Token: `.env` 的 `WHATSAPP_VERIFY_TOKEN`

Meta 驗證會打：
- `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`

訊息事件會打：
- `POST /webhook`

## 5. 本地測試

```bash
ngrok http 8000
```

把 ngrok HTTPS URL 填入 Meta Webhook。

## 6. 主要環境變數

必要：
- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_APP_SECRET`
- `OPENAI_API_KEY`

模型與語音：
- `OPENAI_TRANSCRIBE_MODEL`（預設 `gpt-4o-mini-transcribe`）
- `OPENAI_RESPONSE_MODEL`（預設 `gpt-4.1-mini`）
- `OPENAI_TTS_MODEL`（預設 `gpt-4o-mini-tts`）
- `OPENAI_TTS_VOICE`（預設 `alloy`）
- `OPENAI_TTS_FORMAT`（預設 `opus`）
- `OPENAI_TTS_VOICES`（可切換語音白名單）

語言：
- `OPENAI_DEFAULT_LANGUAGE`（預設 `zh-HK`）
- `OPENAI_LANGUAGES`（可切換語言白名單）

儲存：
- `DB_PATH`（預設 `/tmp/wa_voice_bot.sqlite3`）
- `VOICE_STORE_PATH`
- `LANGUAGE_STORE_PATH`

Admin：
- `ADMIN_SESSION_SECRET`
- `ADMIN_SESSION_HOURS`
- `ADMIN_BOOTSTRAP_EMAIL`（首次建立 admin 用）
- `ADMIN_BOOTSTRAP_PASSWORD`（首次建立 admin 用）

## 7. 用戶指令

### 7.1 聲音切換
- 查看：`voice` / `聲音`
- 切換：`voice aria`

### 7.2 語言切換
- 查看：`language` / `lang` / `語言`
- 切換：`language zh-HK` / `language en` / `語言 廣東話`

### 7.3 記憶功能
- 記下：`記低 明天早上10點同客開會`
- 記下：`remember that client A likes Wednesday morning`
- 讀出：`記憶` / `紀錄` / `memory`

## 8. Web Admin 後台

- 頁面：`GET /admin`

登入後可管理：
- 白名單（新增/刪除可回應用戶）
- 用戶清單
- 每位用戶對話文字記錄
- 每位用戶記憶（新增/封存）
- Admin 帳戶（新增/更新）

首次 admin 建議流程：
1. 設定 `ADMIN_SESSION_SECRET`
2. 設定 `ADMIN_BOOTSTRAP_EMAIL` + `ADMIN_BOOTSTRAP_PASSWORD`
3. 重啟服務後登入 `/admin`
4. 建立正式 admin 後可移除 bootstrap 變數

## 9. API 路由

- `GET /`
- `GET /healthz`
- `GET /privacy`
- `GET /data-deletion`
- `GET /webhook`
- `POST /webhook`
- `GET /admin`
- `POST /admin/auth/login`
- `POST /admin/auth/logout`
- `GET /admin/auth/me`
- `GET /admin/api/users`
- `GET/POST/DELETE /admin/api/whitelist`
- `GET /admin/api/conversations`
- `GET/POST/DELETE /admin/api/memories`
- `GET/POST /admin/api/admin-users`

## 10. 部署到 Vercel

1. Push 到 GitHub
2. 在 Vercel 匯入 repo
3. 設定上述環境變數
4. 部署後把 `https://<vercel-domain>/webhook` 填回 Meta

## 11. 注意事項

- 非白名單用戶訊息會被忽略（不回應）
- 文字訊息目前主要處理 `voice` / `language` / `memory` 類指令
- 預設使用 SQLite；Vercel serverless 的 `/tmp` 屬短暫儲存，建議之後改外部 DB（例如 Supabase）
