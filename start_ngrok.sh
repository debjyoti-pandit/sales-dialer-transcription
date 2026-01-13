#!/bin/bash

# Ngrok tunnel script for Sales Dialer Transcription Service
# Starts an ngrok tunnel with static domain: sales-dialer-transcription-service.jp.ngrok.io

PORT=${1:-8002}
NGROK_DOMAIN="sales-dialer-transcription-service.jp.ngrok.io"
NGROK_AUTH_TOKEN=${NGROK_AUTH_TOKEN:-""}

echo "🚀 Starting ngrok tunnel on port $PORT..."
echo "🌐 Using static domain: $NGROK_DOMAIN"

# Check if ngrok is installed
if ! command -v ngrok &> /dev/null; then
    echo "❌ Error: ngrok is not installed."
    echo "📥 Install it from: https://ngrok.com/download"
    echo "   Or via homebrew: brew install ngrok"
    exit 1
fi

# Set auth token if provided
if [ -n "$NGROK_AUTH_TOKEN" ]; then
    ngrok config add-authtoken "$NGROK_AUTH_TOKEN" 2>/dev/null
    echo "✅ Ngrok auth token configured"
else
    echo "⚠️  NGROK_AUTH_TOKEN not set (required for static domains)"
    echo "   Get your token from: https://dashboard.ngrok.com/get-started/your-authtoken"
fi

echo ""
echo "📋 Static HTTPS URL: https://$NGROK_DOMAIN"
echo "📋 Static WSS URL:   wss://$NGROK_DOMAIN/twilio/media"
echo "   PUBLIC_WSS_URL should be set to the WSS URL above"
echo ""
echo "Press Ctrl+C to stop the tunnel"
echo ""

# Start ngrok with static domain
ngrok http $PORT --domain=$NGROK_DOMAIN

