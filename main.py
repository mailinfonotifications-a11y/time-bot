import discord
from discord import app_commands
from discord.ext import tasks
import datetime
import os
import json
import threading
from flask import Flask
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 1. 環境設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID', 0))
creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')

# Google Calendar Setup
SCOPES = ['https://www.googleapis.com/auth/calendar']
creds_dict = json.loads(creds_json_str)
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

# --- 2. Bot設定 (Intentsを強化) ---
intents = discord.Intents.all() # すべての権限を明示的に有効化
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

active_sessions = {}

def add_to_calendar(status_name, start, end):
    try:
        print(f"DEBUG: Attempting to write to Calendar: {status_name}")
        event = {
            'summary': f'[{status_name}]',
            'start': {'dateTime': start.isoformat()},
            'end': {'dateTime': end.isoformat()},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print("DEBUG: Success! Event created.")
    except Exception as e:
        print(f"DEBUG: Calendar API Error: {e}")

# --- 3. イベント ---
@client.event
async def on_ready():
    print(f'SYSTEM: Logged in as {client.user.name}')
    await tree.sync()
    
    # 起動時の状態確認
    now = datetime.datetime.now(datetime.UTC)
    for guild in client.guilds:
        for member in guild.members:
            if not member.bot and str(member.status) != "offline":
                active_sessions[member.id] = (member.status, now)
    print(f"SYSTEM: Monitoring {len(active_sessions)} users.")

@client.event
async def on_presence_update(before, after):
    # ステータス変更を即座にログに出力（これが重要）
    if before.status == after.status:
        return

    print(f"LOG: Presence Changed - {after.name} ({before.status} -> {after.status})")
    now = datetime.datetime.now(datetime.UTC)
    user_id = after.id

    if user_id in active_sessions:
        prev_status, start_time = active_sessions[user_id]
        duration = (now - start_time).total_seconds()
        
        if duration >= 60: # 1分以上
            status_jp = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}.get(str(prev_status), "アクティブ")
            add_to_calendar(status_jp, start_time, now)
    
    if str(after.status) != "offline":
        active_sessions[user_id] = (after.status, now)
    else:
        active_sessions.pop(user_id, None)

@tree.command(name="status", description="稼働状況確認")
async def status(interaction: discord.Interaction):
    await interaction.response.send_message(f"Monitoring: {len(active_sessions)}人\nCalendar: `{CALENDAR_ID}`")

# --- 4. Flask & Execution ---
app = Flask('')
@app.route('/')
def home(): return "OK"
def run(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run, daemon=True).start()

client.run(TOKEN)
