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

# --- 1. 環境設定と認証 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID', 0))
creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds_dict = json.loads(creds_json_str)
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = MyBot()
active_sessions = {}

# --- 2. ユーティリティ関数 ---
def add_to_calendar(status_name, start, end):
    try:
        print(f"--- Calendar Writing: [{status_name}] ---")
        event = {
            'summary': f'[{status_name}]',
            'start': {'dateTime': start.isoformat() + 'Z'},
            'end': {'dateTime': end.isoformat() + 'Z'},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print("Success: Event created.")
    except Exception as e:
        print(f"Calendar API Error: {e}")

# --- 3. スラッシュコマンド（タイムアウト対策版） ---
@client.tree.command(name="status", description="Botの稼働状況を確認します")
async def status(interaction: discord.Interaction):
    # 「考え中...」の状態にしてタイムアウトを防ぐ
    await interaction.response.defer() 
    
    try:
        embed = discord.Embed(title="Bot Status Report", color=discord.Color.blue())
        embed.add_field(name="Google Calendar ID", value=f"`{CALENDAR_ID}`", inline=False)
        embed.add_field(name="Monitoring Users", value=f"{len(active_sessions)}人", inline=True)
        embed.add_field(name="System", value="Online ✅", inline=True)
        
        # 追撃でメッセージを送信
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"Status command error: {e}")

# --- 4. イベント & ループ ---
@client.event
async def on_ready():
    print(f'Logged in as {client.user.name}')
    now = datetime.datetime.now(datetime.UTC)
    for guild in client.guilds:
        for member in guild.members:
            if not member.bot and str(member.status) != "offline":
                active_sessions[member.id] = (member.status, now)
                print(f"Monitoring: {member.name}")

    if not weekly_report_task.is_running():
        weekly_report_task.start()

@client.event
async def on_presence_update(before, after):
    if before.status == after.status:
        return

    now = datetime.datetime.now(datetime.UTC)
    user_id = after.id
    print(f"Presence Change: {after.name} ({before.status} -> {after.status})")

    if user_id in active_sessions:
        prev_status, start_time = active_sessions[user_id]
        duration = (now - start_time).total_seconds()
        
        if duration >= 60: # 1分
            print(f"Writing session for {after.name} ({duration:.1f}s)")
            status_jp = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}.get(str(prev_status), "アクティブ")
            add_to_calendar(status_jp, start_time, now)
    
    if str(after.status) != "offline":
        active_sessions[user_id] = (after.status, now)
    else:
        active_sessions.pop(user_id, None)

@tasks.loop(time=datetime.time(hour=0, minute=0))
async def weekly_report_task():
    now = datetime.datetime.now(datetime.UTC)
    if now.weekday() != 6: return
    channel = client.get_channel(REPORT_CHANNEL_ID)
    if channel:
        await channel.send("📊 週間レポート作成中...")

# --- 5. Flask ---
app = Flask('')
@app.route('/')
def home(): return "OK"
def run(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run, daemon=True).start()

client.run(TOKEN)
