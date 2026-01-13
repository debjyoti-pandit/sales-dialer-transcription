# sales-dialer-transcription-service

Standalone service that:
- accepts **Twilio Media Streams** audio over WebSocket
- forwards the audio to **Deepgram realtime transcription**
- prints transcripts to the console

## Setup

```bash
cd sales-dialer-transcription-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

Edit `.env` and set:
- `DEEPGRAM_API_KEY`
- `PUBLIC_WSS_URL` (example: `wss://sales-dialer-transcription-service.jp.ngrok.io/twilio/media`)

## Run

```bash
cd sales-dialer-transcription-service
python3 main.py
```

Health check:
- `GET http://localhost:8001/health`

## Twilio

### WebSocket endpoint
- `wss://<your-public-host>/twilio/media`

### Optional TwiML helper

This service exposes `POST /twilio/twiml` to generate minimal TwiML:

```xml
<Response>
  <Connect>
    <Stream url="wss://.../twilio/media" />
  </Connect>
</Response>
```

