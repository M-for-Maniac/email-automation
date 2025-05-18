import os
import base64
import datetime
import asyncio
import logging
from flask import Flask, request
import telegram
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import openai

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

# Configure OpenRouter
openai.api_key = OPENROUTER_API_KEY
openai.api_base = "https://openrouter.ai/api/v1"

# Initialize Telegram bot
bot = telegram.Bot(token=TELEGRAM_TOKEN)

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

@app.route("/webhook", methods=["POST"])
async def webhook():
    update = request.get_json()
    logger.info(f"Received update: {update}")
    if "message" in update and update["message"]["text"] == "/checkemails":
        chat_id = update["message"]["chat"]["id"]
        logger.info(f"Processing /checkemails for chat_id: {chat_id}")
        try:
            emails = fetch_emails()
            logger.info(f"Fetched {len(emails)} emails")
            for email in emails:
                logger.info(f"Analyzing email: {email['subject']}")
                suggestion = analyze_email(email)
                logger.info(f"Sending suggestion for: {email['subject']}")
                await bot.send_message(chat_id=chat_id, text=f"Subject: {email['subject']}\nSuggested Reply: {suggestion}")
                save_to_drive(email, suggestion)
                logger.info(f"Saved suggestion for: {email['subject']}")
            await bot.send_message(chat_id=chat_id, text="All emails processed.")
            logger.info("All emails processed successfully")
        except Exception as e:
            logger.error(f"Error processing emails: {str(e)}", exc_info=True)
            await bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")
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
        response = openai.chat.completions.create(
            model="meta-llama/llama-3.1-70b-instruct:free",
            messages=[{"role": "user", "content": f"Analyze this email and suggest a professional reply: Subject: {email['subject']} Content: {email['body']}"}]
        )
        logger.info(f"OpenRouter response received")
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenRouter error: {str(e)}", exc_info=True)
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