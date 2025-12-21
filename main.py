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
        now = datetime.datetime.now()
        for user_id in user_hourly_data.keys():
            embed, file = create_report_data(user_id, "日報")
            await channel.send(embed=embed, file=file)
            # 日曜なら週報も
            if now.weekday() == 6:
                await channel.send("📊 【週報】今週1週間お疲れ様でした！")
        archive_data()

bot = MyBot()

# --- データ管理 ---
user_hourly_data = {} # {user_id: {hour: seconds}}
user_history = {}     # {user_id: [data_dict, ...]}
user_status_start = {}
user_total_stats = {} # {user_id: {status_str: total_seconds}}

def archive_data():
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
    if not user or user_id not in user_hourly_data: return None, None
    
    # グラフ作成
    plt.figure(figsize=(10, 5))
    hours = [f"{i}h" for i in range(24)]
    today_mins = [user_hourly_data[user_id].get(i, 0)/60 for i in range(24)]
    
    plt.bar(hours, today_mins, color='#7289da', label='Today', alpha=0.8)
    
    if user_id in user_history and user_history[user_id]:
        avg_mins = []
        for i in range(24):
            total_h = sum(day.get(i, 0) for day in user_history[user_id])
            avg_mins.append((total_h / len(user_history[user_id])) / 60)
        plt.plot(hours, avg_mins, color='#faa61a', marker='o', label='7-day Average', linewidth=2)

    plt.title(f"Activity Graph - {user.display_name}")
    plt.ylabel("Minutes Online")
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    
    stats = user_total_stats.get(user_id, {})
    embed = discord.Embed(title=f"{title_prefix}: {user.display_name}", color=discord.Color.blue())
    embed.add_field(name="🟢 合計オンライン", value=format_time(stats.get("online", 0)), inline=True)
    embed.add_field(name="🌙 合計退席中", value=format_time(stats.get("idle", 0)), inline=True)
    embed.add_field(name="⛔ 合計取り込み中", value=format_time(stats.get("dnd", 0)), inline=True)
    embed.add_field(name="⚪ 合計オフライン", value=format_time(stats.get("offline", 0)), inline=True)
    embed.set_image(url="attachment://graph.png")
    
    return embed, discord.File(buf, filename="graph.png")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
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
        
        # 統計加算
        if uid not in user_total_stats: user_total_stats[uid] = {}
        user_total_stats[uid][st_str] = user_total_stats[uid].get(st_str, 0) + dur
        
        # グラフ用（オンライン系のみ）
        if st_str in ["online", "idle", "dnd"]:
            if uid not in user_hourly_data: user_hourly_data[uid] = {i: 0 for i in range(24)}
            user_hourly_data[uid][prev['time'].hour] += dur

    user_status_start[uid] = {'status': after.status, 'time': now}

@bot.tree.command(name="report", description="今日の活動グラフを表示します")
async def report(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, file = create_report_data(interaction.user.id, "現在までのレポート")
    if embed: await interaction.followup.send(embed=embed, file=file)
    else: await interaction.followup.send("まだデータがありません。")

@bot.tree.command(name="status", description="詳細なステータス状況を表示")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(interaction.user.id, {'status': 'unknown', 'time': now})
    elapsed = now - info['time']
    embed = discord.Embed(title="📊 現在の状況", color=discord.Color.green())
    embed.add_field(name="状態", value=str(info['status']), inline=True)
    embed.add_field(name="開始時刻", value=info['time'].strftime('%H:%M:%S'), inline=True)
    embed.add_field(name="継続時間", value=format_time(elapsed.total_seconds()), inline=False)
    await interaction.followup.send(embed=embed)

keep_alive()
bot.run(TOKEN)
