import os
import base64
import datetime
import asyncio
import logging
import requests
from flask import Flask, request
import telegram
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from telegram.request import HTTPXRequest

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Environment variables
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
SHEET_ID = os.environ["SHEET_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets"
]

# Configure Telegram bot with custom HTTPXRequest
httpx_request = HTTPXRequest(
    connection_pool_size=30,  # Increased pool size
    pool_timeout=60.0,  # Increased timeout (seconds)
    read_timeout=60.0,
    write_timeout=60.0,
    connect_timeout=60.0
)
bot = telegram.Bot(token=TELEGRAM_TOKEN, request=httpx_request)

# Cache for processed update IDs
processed_updates = set()

# Cached credentials
_creds = None

def get_credentials():
    global _creds
    if _creds is None or not _creds.valid:
        _creds = Credentials(
            None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES
        )
        if not _creds.valid:
            _creds.refresh(Request())
    return _creds

def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())

def get_sheets_service():
    return build("sheets", "v4", credentials=get_credentials())

async def send_message_with_retry(bot, chat_id, text, max_retries=5):
    for attempt in range(max_retries):
        try:
            logger.info(f"Sending message to chat_id {chat_id}: {text[:50]}...")
            await bot.send_message(chat_id=chat_id, text=text)
            logger.info(f"Message sent successfully to chat_id {chat_id}")
            return
        except telegram.error.TimedOut as e:
            logger.warning(f"Send message attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"Failed to send message after {max_retries} attempts: {str(e)}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error sending message: {str(e)}", exc_info=True)
            raise

@app.route("/webhook", methods=["POST"])
async def webhook():
    update = request.get_json()
    if not update or "update_id" not in update:
        logger.warning("Invalid update received")
        return "OK"

    update_id = update["update_id"]
    if update_id in processed_updates:
        logger.info(f"Skipping duplicate update_id: {update_id}")
        return "OK"
    processed_updates.add(update_id)
    logger.info(f"Received update: {update}")

    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]
        if text == "/start":
            logger.info(f"Processing /start for chat_id: {chat_id}")
            await send_message_with_retry(
                bot, chat_id, "Welcome to the Email Analyzer Bot!\nUse /checkemails to fetch your unread emails and get professional reply suggestions."
            )
        elif text == "/checkemails":
            logger.info(f"Processing /checkemails for chat_id: {chat_id}")
            try:
                emails = fetch_emails()
                logger.info(f"Fetched {len(emails)} emails")
                for email in emails:
                    logger.info(f"Analyzing email: {email['subject']}")
                    suggestion = analyze_email(email)
                    logger.info(f"Sending suggestion for: {email['subject']}")
                    await send_message_with_retry(
                        bot, chat_id, f"Subject: {email['subject']}\nSuggested Reply: {suggestion}"
                    )
                    save_to_drive(email, suggestion)
                    logger.info(f"Saved suggestion for: {email['subject']}")
                await send_message_with_retry(bot, chat_id, "All emails processed.")
                logger.info("All emails processed successfully")
            except Exception as e:
                logger.error(f"Error processing emails: {str(e)}", exc_info=True)
                await send_message_with_retry(bot, chat_id, f"Error: {str(e)}")
    return "OK"

def fetch_emails():
    service = get_gmail_service()
    results = service.users().messages().list(userId="me", q="is:unread", maxResults=3).execute()
    messages = results.get("messages", [])
    emails = []
    for msg in messages:
        msg_data = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        subject = headers.get("Subject", "No Subject")
        body = ""
        if "parts" in msg_data["payload"]:
            for part in msg_data["payload"]["parts"]:
                if part["mimeType"] == "text/plain":
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                    break
        else:
            body = base64.urlsafe_b64decode(msg_data["payload"]["body"]["data"]).decode("utf-8")
        emails.append({"subject": subject, "body": body})
    return emails

def analyze_email(email):
    logger.info(f"Sending to OpenRouter: Subject: {email['subject']}")
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://email-automation-mehrbodcrud285-rmkp8erf.leapcell.dev",  # Replace with your Leapcell URL
                "X-Title": "Email Analyzer"
            },
            json={
                "model": "openai/gpt-3.5-turbo",
                "messages": [
                    {
                        "role": "user",
                        "content": f"Analyze this email and suggest a professional reply: Subject: {email['subject']} Content: {email['body']}"
                    }
                ]
            }
        )
        logger.info(f"OpenRouter response status: {response.status_code}")
        logger.info(f"OpenRouter response body: {response.text}")
        response.raise_for_status()
        response_data = response.json()
        logger.info("OpenRouter response received")
        return response_data["choices"][0]["message"]["content"]
    except requests.exceptions.HTTPError as e:
        logger.error(f"OpenRouter HTTP error: {str(e)}", exc_info=True)
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"OpenRouter request error: {str(e)}", exc_info=True)
        raise
    except KeyError as e:
        logger.error(f"OpenRouter response parsing error: {str(e)}", exc_info=True)
        raise

def save_to_drive(email, suggestion):
    service = get_sheets_service()
    range_name = "Sheet1!A:C"
    timestamp = datetime.datetime.now().isoformat()
    values = [[timestamp, email['subject'], suggestion]]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=range_name,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()
    logger.info(f"Saved to Google Sheet: {email['subject']}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)