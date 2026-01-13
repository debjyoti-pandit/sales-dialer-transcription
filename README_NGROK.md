# Ngrok Setup Guide (Transcription Service)

This guide explains how to use ngrok to expose your local **Sales Dialer Transcription Service** to the internet for **Twilio Media Streams**.

## Static Domain

This service is configured to use a static ngrok domain:

- `https://sales-dialer-transcription-service.jp.ngrok.io`

Your Twilio Media Stream websocket URL will be:

- `wss://sales-dialer-transcription-service.jp.ngrok.io/twilio/media`

## Setup

1. Get your ngrok auth token from `https://dashboard.ngrok.com/get-started/your-authtoken`
2. Export it:

```bash
export NGROK_AUTH_TOKEN=your_token_here
```

## Usage

### Option 1: Bash script

```bash
./start_ngrok.sh
```

### Option 2: Python script (updates `.env`)

```bash
python3 start_ngrok.py 8001 --update-env
```

## Twilio

Point Twilio Media Streams to:

- `wss://sales-dialer-transcription-service.jp.ngrok.io/twilio/media`

Or point a Twilio webhook/TwiML Bin to:

- `POST https://sales-dialer-transcription-service.jp.ngrok.io/twilio/twiml`

