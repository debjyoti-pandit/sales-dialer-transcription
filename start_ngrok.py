#!/usr/bin/env python3
"""
Ngrok tunnel script for Sales Dialer Transcription Service.
Starts an ngrok tunnel and optionally updates PUBLIC_WSS_URL in .env
"""

import subprocess
import sys
import os
import time
import requests
from pathlib import Path


def check_ngrok_installed():
    """Check if ngrok is installed"""
    try:
        subprocess.run(["ngrok", "version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_ngrok_url():
    """Get the public ngrok URL from ngrok API"""
    try:
        response = requests.get("http://localhost:4040/api/tunnels", timeout=2)
        if response.status_code == 200:
            data = response.json()
            tunnels = data.get("tunnels", [])
            if tunnels:
                return tunnels[0].get("public_url")
    except (requests.RequestException, KeyError, IndexError):
        pass
    return None


def update_env_file(public_wss_url: str):
    """Update PUBLIC_WSS_URL in .env file"""
    env_file = Path(".env")
    if not env_file.exists():
        print("⚠️  .env file not found. Creating one...")
        env_file.write_text(f"PUBLIC_WSS_URL={public_wss_url}\n", encoding="utf-8")
        return

    lines = env_file.read_text(encoding="utf-8").splitlines()

    updated = False
    new_lines = []
    for line in lines:
        if line.startswith("PUBLIC_WSS_URL="):
            new_lines.append(f"PUBLIC_WSS_URL={public_wss_url}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"PUBLIC_WSS_URL={public_wss_url}")

    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"✅ Updated PUBLIC_WSS_URL in .env file: {public_wss_url}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8001
    update_env = "--update-env" in sys.argv or "-u" in sys.argv

    # Static ngrok domain
    ngrok_domain = "sales-dialer-transcription-service.jp.ngrok.io"
    https_url = f"https://{ngrok_domain}"
    wss_url = f"wss://{ngrok_domain}/twilio/media"

    print("🚀 Sales Dialer Transcription Service - Ngrok Tunnel")
    print("=" * 60)

    if not check_ngrok_installed():
        print("❌ Error: ngrok is not installed.")
        print("📥 Install it from: https://ngrok.com/download")
        print("   Or via homebrew: brew install ngrok")
        sys.exit(1)

    auth_token = os.getenv("NGROK_AUTH_TOKEN")
    if auth_token:
        print("✅ Ngrok auth token found in environment")
    else:
        print("⚠️  NGROK_AUTH_TOKEN not set (REQUIRED for static domains)")
        print("   Get your token from: https://dashboard.ngrok.com/get-started/your-authtoken")
        print("   Set it: export NGROK_AUTH_TOKEN=your_token")
        response = input("\nContinue anyway? (y/N): ")
        if response.lower() != "y":
            sys.exit(1)

    print(f"\n🌐 Starting ngrok tunnel on port {port}...")
    print(f"📋 Using static domain: {ngrok_domain}")
    print(f"📋 Static HTTPS URL: {https_url}")
    print(f"📋 Static WSS  URL: {wss_url}")
    print("📋 Waiting for tunnel to be established...")
    print("\nPress Ctrl+C to stop the tunnel\n")

    try:
        ngrok_process = subprocess.Popen(
            ["ngrok", "http", str(port), "--domain", ngrok_domain],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        time.sleep(3)

        max_retries = 10
        for _ in range(max_retries):
            url = get_ngrok_url()
            if url:
                # enforce static URL
                if ngrok_domain not in url:
                    print(f"⚠️  Warning: Expected {https_url} but got {url}")

                print("=" * 60)
                print("✅ Tunnel established!")
                print(f"🌐 Static HTTPS URL: {https_url}")
                print(f"🌐 Static WSS  URL: {wss_url}")
                print("=" * 60)

                print("\n📝 Twilio endpoints:")
                print(f"   Websocket (Media Stream): {wss_url}")
                print(f"   TwiML helper (POST):      {https_url}/twilio/twiml")

                if update_env:
                    update_env_file(wss_url)
                else:
                    print("\n💡 Tip: Run with --update-env to write PUBLIC_WSS_URL into .env")

                print("\n⏳ Tunnel is running. Press Ctrl+C to stop...\n")

                try:
                    ngrok_process.wait()
                except KeyboardInterrupt:
                    print("\n\n🛑 Stopping ngrok tunnel...")
                    ngrok_process.terminate()
                    ngrok_process.wait()
                    print("✅ Tunnel stopped")
                    break
                break
            time.sleep(1)
        else:
            print("⚠️  Could not get ngrok URL. Check if ngrok is running correctly.")
            print("   You can manually check: http://localhost:4040")
            ngrok_process.terminate()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n🛑 Stopping ngrok tunnel...")
        if "ngrok_process" in locals():
            ngrok_process.terminate()
        print("✅ Tunnel stopped")
    except (subprocess.SubprocessError, OSError) as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

