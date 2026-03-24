# WhatsApp Voice Bot (Meta Cloud API + OpenAI)

這個專案提供一個可運行的 WhatsApp 語音對話 MVP：
- 接收 WhatsApp 語音訊息（Meta Cloud API Webhook）
- 用 OpenAI 轉錄語音成文字
- 用 OpenAI 生成回覆
- 用 OpenAI TTS 轉成語音
- 回傳語音訊息到 WhatsApp

## 1. 專案結構

```text
api/index.py
app/main.py
app/config.py
app/whatsapp.py
app/openai_client.py
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

Webhook URL 例子：
- `https://<your-domain>/webhook`

Verify Token：
- 使用 `.env` 的 `WHATSAPP_VERIFY_TOKEN`

Webhook 驗證：
- Meta 會呼叫 `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`

訊息接收：
- Meta 會呼叫 `POST /webhook`

## 5. 本地測試

可用 ngrok 暴露本地端口：

```bash
ngrok http 8000
```

把 ngrok HTTPS URL 填入 Meta Webhook。

## 6. 必要環境變數

- `WHATSAPP_ACCESS_TOKEN`: WhatsApp Cloud API token
- `WHATSAPP_PHONE_NUMBER_ID`: 你的 phone number id
- `WHATSAPP_VERIFY_TOKEN`: webhook 驗證字串
- `WHATSAPP_APP_SECRET`: 用來驗 `x-hub-signature-256`（建議設定）
- `OPENAI_API_KEY`: OpenAI API key

可調整模型：
- `OPENAI_TRANSCRIBE_MODEL`（預設 `gpt-4o-mini-transcribe`）
- `OPENAI_RESPONSE_MODEL`（預設 `gpt-4.1-mini`）
- `OPENAI_TTS_MODEL`（預設 `gpt-4o-mini-tts`）
- `OPENAI_TTS_VOICE`（預設 `alloy`）
- `OPENAI_TTS_FORMAT`（預設 `opus`，建議保持）

## 7. API 路由

- `GET /`: service info
- `GET /healthz`: 檢查 WhatsApp / OpenAI 連線
- `GET /webhook`: Meta webhook verify
- `POST /webhook`: 處理入站 WhatsApp 訊息

## 8. 部署到 Vercel（可選）

這個 repo 已附 `vercel.json`。

步驟：
1. Push 到 GitHub
2. Vercel 匯入 repo
3. 設定所有環境變數
4. 部署後把 `https://<vercel-domain>/webhook` 填回 Meta

## 9. 注意事項

- 目前只處理 `audio` 訊息，文字訊息會忽略
- 單請求串行處理語音；高流量建議改成 queue / worker
- 請確認你的 WhatsApp Cloud API 帳號已開啟對應權限
