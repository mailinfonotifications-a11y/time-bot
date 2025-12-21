import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from flask import Flask
from threading import Thread

# --- Flask設定（RenderのURL表示用） ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- 各種設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
REPORT_CHANNEL_ID = 1319766946648784969  # 前設定したID
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.daily_report_loop.start() # 自動レポート開始
        await self.tree.sync()

    # 毎日23:59に自動投稿するタスク
    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
    async def daily_report_loop(self):
        channel = self.get_channel(REPORT_CHANNEL_ID)
        if not channel: return
        
        for user_id in user_daily_stats.keys():
            embed = create_report_embed(user_id)
            if embed:
                await channel.send(content="🌙 本日の活動まとめです！", embed=embed)
        
        # 翌日のためにデータをリセット
        user_daily_stats.clear()

bot = MyBot()

# --- データ管理用 ---
user_status_start = {} # 現在の開始時刻
user_daily_stats = {}  # 今日の合計時間 {user_id: {"online": 秒, "idle": 秒, "dnd": 秒}}

def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}時間{m}分" if h > 0 else f"{m}分"

def create_report_embed(user_id):
    if user_id not in user_daily_stats: return None
    user = bot.get_user(user_id)
    if not user: return None
    
    stats = user_daily_stats[user_id]
    embed = discord.Embed(title=f"📅 活動レポート: {user.display_name}", color=discord.Color.gold(), timestamp=datetime.datetime.now())
    embed.add_field(name="🟢 オンライン", value=format_duration(stats.get("online", 0)), inline=True)
    embed.add_field(name="🌙 退席中", value=format_duration(stats.get("idle", 0)), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_duration(stats.get("dnd", 0)), inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except Exception as e: print(f"Calendar Error: {e}")

@bot.event
async def on_ready():
    print(f"SYSTEM: Logged in as {bot.user.name}")
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                user_status_start[member.id] = {'status': member.status, 'time': now}

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    user_id = after.id
    
    if user_id in user_status_start:
        prev = user_status_start[user_id]
        duration = (now - prev['time']).total_seconds()
        
        if duration >= 60:
            status_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            prev_status_str = str(prev['status'])
            if prev_status_str in status_map:
                status_jp, color_id = status_map[prev_status_str]
                # カレンダー登録
                add_to_calendar(status_jp, prev['time'], now - datetime.timedelta(seconds=1), color_id)
                # レポート用統計に加算
                if user_id not in user_daily_stats: user_daily_stats[user_id] = {}
                user_daily_stats[user_id][prev_status_str] = user_daily_stats[user_id].get(prev_status_str, 0) + duration

    user_status_start[user_id] = {'status': after.status, 'time': now}

@bot.tree.command(name="status", description="現在のステータスと継続時間を表示します")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(interaction.user.id, {'status': 'unknown', 'time': now})
    
    display_map = {
        "online": ("🟢 オンライン", discord.Color.green()),
        "idle": ("🌙 退席中", discord.Color.gold()),
        "dnd": ("⛔ 取り込み中", discord.Color.red()),
        "offline": ("⚪ オフライン", discord.Color.light_grey())
    }
    st_text, color = display_map.get(str(info['status']), ("不明", discord.Color.greyple()))
    elapsed = now - info['time']
    
    embed = discord.Embed(title="📊 活動ステータス", color=color, timestamp=now)
    embed.add_field(name="状態", value=st_text, inline=True)
    embed.add_field(name="いつから", value=f"🕒 {info['time'].strftime('%H:%M:%S')}", inline=True)
    embed.add_field(name="経過時間", value=f"⏳ {format_duration(elapsed.total_seconds())}経過", inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="report", description="今日の活動時間の合計を表示します")
async def report(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = create_report_embed(interaction.user.id)
    if embed:
        await interaction.followup.send(content="📋 今日のこれまでの集計結果です！", embed=embed)
    else:
        await interaction.followup.send(content="まだ今日のデータが蓄積されていません。1分以上ステータスを維持すると計測されます。")

keep_alive()
bot.run(TOKEN)
