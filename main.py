import discord
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

# Google API認証設定
SCOPES = ['https://www.googleapis.com/auth/calendar']
creds_dict = json.loads(creds_json_str)
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

# Discord Bot設定
intents = discord.Intents.default()
intents.presences = True
intents.members = True
client = discord.Client(intents=intents)

# セッション管理用 {user_id: (status, start_time)}
active_sessions = {}

# --- 2. ユーティリティ関数 ---

def add_to_calendar(status_name, start, end):
    """Googleカレンダーにイベントを作成"""
    try:
        event = {
            'summary': f'[{status_name}]',
            'start': {'dateTime': start.isoformat() + 'Z'},
            'end': {'dateTime': end.isoformat() + 'Z'},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except Exception as e:
        print(f"Calendar API Error: {e}")

def get_weekly_stats(weeks_back=0):
    """指定された週の合計活動時間を取得（日曜〜土曜）"""
    now = datetime.datetime.utcnow()
    # 月曜を週の始まりとする調整
    start_of_week = (now - datetime.timedelta(days=now.weekday() + 7*weeks_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + datetime.timedelta(days=7)
    
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_of_week.isoformat() + 'Z',
        timeMax=end_of_week.isoformat() + 'Z',
        singleEvents=True
    ).execute()
    
    events = events_result.get('items', [])
    total_seconds = 0
    stats = {}

    for event in events:
        try:
            start_str = event['start'].get('dateTime', event['start'].get('date')).replace('Z', '')
            end_str = event['end'].get('dateTime', event['end'].get('date')).replace('Z', '')
            start = datetime.datetime.fromisoformat(start_str)
            end = datetime.datetime.fromisoformat(end_str)
            duration = (end - start).total_seconds()
            status = event['summary']
            stats[status] = stats.get(status, 0) + duration
            total_seconds += duration
        except:
            continue
        
    return stats, total_seconds

# --- 3. Discordイベント & ループ ---

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
            
            if duration >= 300: # 5分以上なら記録
                status_jp = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}.get(str(prev_status), "アクティブ")
                add_to_calendar(status_jp, start_time, now)
        
        if str(after.status) != "offline":
            active_sessions[user_id] = (after.status, now)
        else:
            active_sessions.pop(user_id, None)

# 日曜9時のレポートタスク (修正済み: asyncを追加)
@tasks.loop(time=datetime.time(hour=0, minute=0))
async def weekly_report_task():
    now = datetime.datetime.utcnow()
    if now.weekday() != 6: # 6 = 日曜日
        return

    channel = client.get_channel(REPORT_CHANNEL_ID)
    if not channel:
        print("Report channel not found.")
        return

    this_week_stats, this_total = get_weekly_stats(0)
    last_week_stats, last_total = get_weekly_stats(1)

    diff = (this_total - last_total) / 3600
    
    report = f"📊 **週間活動レポート (日曜日 09:00)**\n"
    report += f"今週の合計: {this_total/3600:.1f}時間\n"
    report += f"前週比: {'+' if diff >= 0 else ''}{diff:.1f}時間\n\n"
    report += "【内訳】\n"
    for k, v in this_week_stats.items():
        report += f"・{k}: {v/3600:.1f}時間\n"
    
    await channel.send(report)

# --- 4. Flask (UptimeRobot用) ---
app = Flask('')
@app.route('/')
def home(): return "OK"

def run(): app.run(host='0.0.0.0', port=8080)
threading.Thread(target=run).start()

client.run(TOKEN)
