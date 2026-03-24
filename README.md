# WhatsApp Voice Bot (Meta Cloud API + OpenAI + Supabase)

這個專案提供可部署的 WhatsApp 語音助手，已改為 **Supabase 持久化**（不再用本地 SQLite）。

核心能力：
- WhatsApp 語音訊息 -> OpenAI STT -> LLM -> TTS -> 回語音
- 白名單控制（只回應白名單）
- 用戶語音/語言偏好（每位用戶獨立）
- 雙向對話文字記錄
- 記憶功能（用戶可要求記下/讀出）
- Admin 可為指定用戶植入記憶
- Web Admin 帳戶登入與管理

## 1. 專案結構

```text
api/index.py
app/main.py
app/config.py
app/whatsapp.py
app/openai_client.py
app/db.py
app/admin_auth.py
supabase/schema.sql
.env.example
requirements.txt
vercel.json
```

## 2. 環境需求

- Python 3.10+
- Meta Developer App + WhatsApp Cloud API
- OpenAI API Key
- Supabase 專案（Postgres）

## 3. Supabase 初始化

1. 建立 Supabase project
2. 打開 SQL Editor，貼上 [`supabase/schema.sql`](supabase/schema.sql)
3. 取得：
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## 4. 安裝與啟動

```bash
cd /Users/TY/whatsapp-openai-voice-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 5. Meta Webhook 設定

- Callback URL: `https://<your-domain>/webhook`
- Verify Token: `.env` 的 `WHATSAPP_VERIFY_TOKEN`

## 6. 主要環境變數

### Meta
- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_APP_SECRET`

### Supabase
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

### OpenAI
- `OPENAI_API_KEY`
- `OPENAI_TRANSCRIBE_MODEL`（預設 `gpt-4o-mini-transcribe`）
- `OPENAI_RESPONSE_MODEL`（預設 `gpt-4.1-mini`）
- `OPENAI_TTS_MODEL`（預設 `gpt-4o-mini-tts`）
- `OPENAI_TTS_VOICE`（預設 `alloy`）
- `OPENAI_TTS_FORMAT`（預設 `opus`）
- `OPENAI_TTS_VOICES`（語音白名單）
- `OPENAI_DEFAULT_LANGUAGE`（預設 `zh-HK`）
- `OPENAI_LANGUAGES`（語言白名單）

### Admin
- `ADMIN_SESSION_SECRET`
- `ADMIN_SESSION_HOURS`（預設 12）
- `ADMIN_BOOTSTRAP_EMAIL`（首次建立 admin）
- `ADMIN_BOOTSTRAP_PASSWORD`（首次建立 admin）

## 7. 用戶指令

### 7.1 聲音
- 查看：`voice` / `聲音`
- 切換：`voice aria`

### 7.2 語言
- 查看：`language` / `lang` / `語言`
- 切換：`language zh-HK` / `language en` / `語言 廣東話`

### 7.3 記憶
- 記下：`記低 明天早上10點同客開會`
- 記下：`remember that client A likes Wednesday morning`
- 讀出：`記憶` / `紀錄` / `memory`

## 8. Web Admin

- 後台：`GET /admin`

登入後可管理：
- 白名單
- 用戶清單
- 對話文字記錄
- 用戶記憶（新增/封存）
- Admin 帳戶

## 9. 路由

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
3. 設定所有 env
4. 部署後把 `https://<vercel-domain>/webhook` 填回 Meta

## 11. 注意事項

- 非白名單用戶訊息會忽略
- 白名單、記憶、對話紀錄、偏好都存 Supabase
- `SUPABASE_SERVICE_ROLE_KEY` 必須只放 server 環境，不可前端曝光
