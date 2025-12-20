import discord
from discord import app_commands # スラッシュコマンド用に使う
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

# Discord Bot設定
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        super().__init__(intents=intents)
        # スラッシュコマンドの同期用
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 起動時にスラッシュコマンドをDiscordに登録
        await self.tree.sync()

client = MyBot()
active_sessions = {}

# --- 2. ユーティリティ関数 ---
def add_to_calendar(status_name, start, end):
    try:
        event = {
            'summary': f'[{status_name}]',
            'start': {'dateTime': start.isoformat() + 'Z'},
            'end': {'dateTime': end.isoformat() + 'Z'},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except Exception as e:
        print(f"Calendar API Error: {e}")

# --- 3. スラッシュコマンド ---
@client.tree.command(name="status", description="Botの稼働状況を確認します")
async def status(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Status Report", color=discord.Color.blue())
    embed.add_field(name="Google Calendar ID", value=f"`{CALENDAR_ID}`", inline=False)
    
    # 現在監視中の人数
    monitoring_count = len(active_sessions)
    embed.add_field(name="Monitoring Users", value=f"{monitoring_count}人", inline=True)
    
    # システム稼働時間
    embed.add_field(name="System", value="Online ✅", inline=True)
    
    await interaction.response.send_message(embed=embed)

# --- 4. イベント & ループ ---
@client.event
async def on_ready():
    print(f'Logged in as {client.user.name}')
    if not weekly_report_task.is_running():
        weekly_report_task.start()

@client.event
async def on_presence_update(before, after):
    now = datetime.datetime.utcnow()
    user_id = after.id

    if before.status != after.status:
        if user_id in active_sessions:
            prev_status, start_time = active_sessions[user_id]
            duration = (now - start_time).total_seconds()
            
            if duration >= 60: # 5分以上
                status_jp = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}.get(str(prev_status), "アクティブ")
                add_to_calendar(status_jp, start_time, now)
        
        if str(after.status) != "offline":
            active_sessions[user_id] = (after.status, now)
        else:
            active_sessions.pop(user_id, None)

@tasks.loop(time=datetime.time(hour=0, minute=0))
async def weekly_report_task():
    # (中身は前のコードと同じなので省略。貼り付ける際は前のコードのままでOKです)
    pass

# --- 5. Flask ---
app = Flask('')
@app.route('/')
def home(): return "OK"
def run(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run).start()

client.run(TOKEN)
