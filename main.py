import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
import io
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from flask import Flask
from threading import Thread

# --- Flask (Render用) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- 設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
REPORT_CHANNEL_ID = 1319766946648784969
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
        self.daily_task.start()
        await self.tree.sync()

    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
    async def daily_task(self):
        channel = self.get_channel(REPORT_CHANNEL_ID)
        if not channel: return
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        for user_id in list(user_status_start.keys()):
            embed, file = create_report_data(user_id, "📅 本日の活動日報")
            if embed:
                await channel.send(embed=embed, file=file)
        # 23:59の投稿後にデータを履歴へ移動しリセット
        archive_data()

bot = MyBot()

# --- データ管理 ---
user_hourly_data = {} # 今日の1時間ごとの秒数
user_history = {}     # 過去14日分のデータのリスト
user_status_start = {}
user_total_stats = {} # 今日のステータス別合計秒数
last_reset_date = {}  # 最後にリセットした日付

def archive_data():
    """1日のデータを履歴(14日間)に保存し、当日分を空にする"""
    for uid in list(user_hourly_data.keys()):
        if uid not in user_history: user_history[uid] = []
        user_history[uid].append(user_hourly_data[uid].copy())
        if len(user_history[uid]) > 14: # 2週間分保持
            user_history[uid].pop(0)
    
    user_hourly_data.clear()
    user_total_stats.clear()

def check_day_reset(user_id):
    """日付が変わっていたらデータをリセットする"""
    now_date = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
    if user_id not in last_reset_date:
        last_reset_date[user_id] = now_date
        return

    if last_reset_date[user_id] != now_date:
        # 日付が変わったタイミングで前日分をアーカイブ
        archive_data()
        last_reset_date[user_id] = now_date

def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"{h}時間{m}分" if h > 0 else f"{m}分"

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except Exception as e:
        print(f"CALENDAR ERROR: {e}")

def create_report_data(user_id, title_prefix):
    user = bot.get_user(user_id)
    if not user: return None, None
    
    # 日付リセット確認
    check_day_reset(user_id)

    temp_hourly = user_hourly_data.get(user_id, {i: 0 for i in range(24)}).copy()
    temp_total = user_total_stats.get(user_id, {}).copy()
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if user_id in user_status_start:
        info = user_status_start[user_id]
        dur = (now - info['time']).total_seconds()
        st = str(info['status'])
        temp_total[st] = temp_total.get(st, 0) + dur
        if st in ["online", "idle", "dnd"]:
            temp_hourly[info['time'].hour] = temp_hourly.get(info['time'].hour, 0) + dur

    plt.figure(figsize=(10, 5))
    hours_labels = [f"{i}h" for i in range(24)]
    today_mins = [temp_hourly.get(i, 0)/60 for i in range(24)]
    
    plt.bar(hours_labels, today_mins, color='#7289da', label='Today', alpha=0.8)
    
    # 過去14日間の平均線を表示
    if user_id in user_history and user_history[user_id]:
        avg_mins = []
        for i in range(24):
            total_s = sum(day.get(i, 0) for day in user_history[user_id])
            avg_mins.append((total_s / len(user_history[user_id])) / 60)
        plt.plot(hours_labels, avg_mins, color='#faa61a', marker='o', label='14-day Average', linewidth=2)

    plt.title(f"Activity Graph (Today) - {user.display_name}")
    plt.ylabel("Minutes")
    plt.legend()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    
    file = discord.File(buf, filename="graph.png")
    embed = discord.Embed(title=f"{title_prefix}: {user.display_name}", color=discord.Color.blue(), timestamp=now)
    embed.add_field(name="🟢 オンライン", value=format_time(temp_total.get("online", 0)), inline=True)
    embed.add_field(name="🌙 退席中", value=format_time(temp_total.get("idle", 0)), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_time(temp_total.get("dnd", 0)), inline=True)
    embed.add_field(name="⚪ オフライン", value=format_time(temp_total.get("offline", 0)), inline=True)
    embed.set_image(url="attachment://graph.png")
    return embed, file

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                user_status_start[member.id] = {'status': member.status, 'time': now}
                last_reset_date[member.id] = now.date()

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    uid = after.id
    
    # 日付が変わっていたら前日分をクリア
    check_day_reset(uid)
    
    if uid in user_status_start:
        prev = user_status_start[uid]
        dur = (now - prev['time']).total_seconds()
        st_str = str(prev['status'])
        
        # 統計加算
        if uid not in user_total_stats: user_total_stats[uid] = {}
        user_total_stats[uid][st_str] = user_total_stats[uid].get(st_str, 0) + dur
        
        # グラフ用データ
        if st_str in ["online", "idle", "dnd"]:
            if uid not in user_hourly_data: user_hourly_data[uid] = {i: 0 for i in range(24)}
            user_hourly_data[uid][prev['time'].hour] = user_hourly_data[uid].get(prev['time'].hour, 0) + dur

            if dur >= 60:
                status_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
                if st_str in status_map:
                    summary, color_id = status_map[st_str]
                    add_to_calendar(summary, prev['time'], now, color_id)

    user_status_start[uid] = {'status': after.status, 'time': now}

@bot.tree.command(name="report", description="今日の活動レポートを表示")
async def report(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    embed, file = create_report_data(interaction.user.id, "📊 今日の活動")
    if embed: await interaction.followup.send(embed=embed, file=file)

@bot.tree.command(name="status", description="ステータス詳細を表示")
async def status(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(interaction.user.id, {'status': 'unknown', 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    embed = discord.Embed(title="📊 ステータス", color=discord.Color.green(), timestamp=now)
    embed.add_field(name="状態", value=st_map.get(str(info['status']), "不明"), inline=True)
    embed.add_field(name="経過", value=format_time(elapsed.total_seconds()), inline=True)
    await interaction.followup.send(embed=embed)

keep_alive()
bot.run(TOKEN)
