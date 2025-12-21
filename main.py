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

# --- Flask (Renderの起動維持とURL表示用) ---
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

    # 毎日23:59に自動レポート
    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
    async def daily_task(self):
        channel = self.get_channel(REPORT_CHANNEL_ID)
        if not channel: return
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        for user_id in list(user_status_start.keys()):
            embed, file = create_report_data(user_id, "📅 本日の活動日報")
            if embed:
                await channel.send(embed=embed, file=file)
                if now.weekday() == 6: # 日曜日の場合
                    await channel.send("📊 【週報】今週1週間、大変お疲れ様でした！")
        archive_data()

bot = MyBot()

# --- データ管理用 ---
user_hourly_data = {} # {user_id: {hour: seconds}}
user_history = {}     # {user_id: [data_dict, ...]}
user_status_start = {}
user_total_stats = {} # {user_id: {status_str: total_seconds}}

def archive_data():
    """1日の終わりにデータを履歴に移動し、当日分をリセット"""
    for uid, data in user_hourly_data.items():
        if uid not in user_history: user_history[uid] = []
        user_history[uid].append(data.copy())
        if len(user_history[uid]) > 7: user_history[uid].pop(0)
    user_hourly_data.clear()
    user_total_stats.clear()

def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"{h}時間{m}分" if h > 0 else f"{m}分"

def create_report_data(user_id, title_prefix):
    user = bot.get_user(user_id)
    if not user: return None, None
    
    # --- グラフ用の一時データ作成（現在進行中の時間も加算） ---
    temp_hourly = user_hourly_data.get(user_id, {i: 0 for i in range(24)}).copy()
    temp_total = user_total_stats.get(user_id, {}).copy()
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if user_id in user_status_start:
        info = user_status_start[user_id]
        dur = (now - info['time']).total_seconds()
        st = str(info['status'])
        # 統計に加算
        temp_total[st] = temp_total.get(st, 0) + dur
        # グラフ用（オンライン系のみ）に加算
        if st in ["online", "idle", "dnd"]:
            temp_hourly[info['time'].hour] += dur

    # グラフの描画
    plt.figure(figsize=(10, 5))
    hours_labels = [f"{i}h" for i in range(24)]
    today_mins = [temp_hourly.get(i, 0)/60 for i in range(24)]
    
    plt.bar(hours_labels, today_mins, color='#7289da', label='Today', alpha=0.8)
    
    # 過去7日間の平均線を表示
    if user_id in user_history and user_history[user_id]:
        avg_mins = []
        for i in range(24):
            total_s = sum(day.get(i, 0) for day in user_history[user_id])
            avg_mins.append((total_s / len(user_history[user_id])) / 60)
        plt.plot(hours_labels, avg_mins, color='#faa61a', marker='o', label='7-day Average', linewidth=2)

    plt.title(f"Activity Graph - {user.display_name}")
    plt.xlabel("Time (Hour)")
    plt.ylabel("Minutes Online")
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.5)
    
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
    uid = after.id
    
    if uid in user_status_start:
        prev = user_status_start[uid]
        dur = (now - prev['time']).total_seconds()
        st_str = str(prev['status'])
        
        # 統計確定（辞書に保存）
        if uid not in user_total_stats: user_total_stats[uid] = {}
        user_total_stats[uid][st_str] = user_total_stats[uid].get(st_str, 0) + dur
        
        # グラフ用データ確定
        if st_str in ["online", "idle", "dnd"]:
            if uid not in user_hourly_data: user_hourly_data[uid] = {i: 0 for i in range(24)}
            user_hourly_data[uid][prev['time'].hour] += dur

    user_status_start[uid] = {'status': after.status, 'time': now}

@bot.tree.command(name="report", description="今日の活動グラフと合計時間を表示します")
async def report(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, file = create_report_data(interaction.user.id, "📊 現在までの活動レポート")
    if embed:
        await interaction.followup.send(embed=embed, file=file)
    else:
        await interaction.followup.send("データの取得に失敗しました。")

@bot.tree.command(name="status", description="現在のステータス詳細を表示します")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(interaction.user.id, {'status': 'unknown', 'time': now})
    elapsed = now - info['time']
    
    st_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    
    embed = discord.Embed(title="📊 現在のステータス", color=discord.Color.green(), timestamp=now)
    embed.add_field(name="状態", value=st_map.get(str(info['status']), "不明"), inline=True)
    embed.add_field(name="開始時刻", value=info['time'].strftime('%H:%M:%S'), inline=True)
    embed.add_field(name="継続時間", value=format_time(elapsed.total_seconds()), inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

keep_alive()
bot.run(TOKEN)
