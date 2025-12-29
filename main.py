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
import gspread
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
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)
gc = gspread.authorize(creds)
try:
    sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
except:
    print("SPREADSHEET ERROR")

class MyBot(commands.Bot):
    def __init__(self):
        # 複数人を監視するために全権限（Intents.all）を必須化
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.daily_task.start()
        await self.tree.sync()

    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
    async def daily_task(self):
        try:
            records = sheet.get_all_records()
            for r in records:
                u_id, c_id = int(r['user_id']), int(r['channel_id'])
                channel = self.get_channel(c_id)
                if channel:
                    embed, file = await create_report_data(u_id, "📅 本日の最終活動レポート")
                    if embed: await channel.send(embed=embed, file=file)
        except: pass

bot = MyBot()

# --- データ管理 (ユーザーごとの個別辞書) ---
user_status_start = {}

def save_user_config(user_id, channel_id, name):
    try:
        cell = sheet.find(str(user_id))
        if cell:
            sheet.update_cell(cell.row, 2, str(channel_id))
            sheet.update_cell(cell.row, 3, name)
        else:
            sheet.append_row([str(user_id), str(channel_id), name])
    except: pass

def get_user_configs():
    try:
        records = sheet.get_all_records()
        return {int(r['user_id']): int(r['channel_id']) for r in records}
    except: return {}

def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"**{h}**時間**{m}**分" if h > 0 else f"**{m}**分"

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"CALENDAR SUCCESS: {summary}")
    except Exception as e:
        print(f"CALENDAR ERROR: {e}")

async def get_activity_data_from_calendar(start_dt, end_dt):
    try:
        events_result = service.events().list(calendarId=CALENDAR_ID, timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(), singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        hourly_data = {i: 0 for i in range(24)}
        status_totals = {"オンライン": 0, "退席中": 0, "取り込み中": 0}
        active_days = set()
        for event in events:
            summary = event.get('summary', '')
            found_status = next((k for k in status_totals.keys() if k in summary), None)
            if not found_status: continue
            s = datetime.datetime.fromisoformat(event['start'].get('dateTime').replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            e = datetime.datetime.fromisoformat(event['end'].get('dateTime').replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            curr, limit = max(s, start_dt), min(e, end_dt)
            if curr < limit:
                active_days.add(curr.date())
                status_totals[found_status] += (limit - curr).total_seconds()
                it = curr
                while it < limit:
                    next_h = (it + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    hourly_data[it.hour] += (min(limit, next_h) - it).total_seconds()
                    it = min(limit, next_h)
        return hourly_data, status_totals, len(active_days)
    except: return {i: 0 for i in range(24)}, {"オンライン": 0, "退席中": 0, "取り込み中": 0}, 0

async def create_report_data(user_id, title_prefix):
    user = bot.get_user(user_id)
    if not user: return None, None
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_hourly, today_status, _ = await get_activity_data_from_calendar(today_start, now)
    
    # リアルタイム分を合算
    if user_id in user_status_start:
        info = user_status_start[user_id]
        st_map = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}
        st_jp = st_map.get(info['status'])
        if st_jp:
            eff_start = max(info['time'], today_start)
            if eff_start < now:
                dur = (now - eff_start).total_seconds()
                today_status[st_jp] += dur
                it = eff_start
                while it < now:
                    next_h = (it + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    today_hourly[it.hour] += (min(now, next_h) - it).total_seconds()
                    it = min(now, next_h)

    hist_start = today_start - datetime.timedelta(days=14)
    hist_hourly, _, active_days_count = await get_activity_data_from_calendar(hist_start, today_start)
    divisor = max(active_days_count, 1)
    avg_hourly = {h: sec / divisor for h, sec in hist_hourly.items()}
    avg_total_day = sum(avg_hourly.values())

    # --- グラフデザイン (元データを完全踏襲) ---
    plt.figure(figsize=(10, 5))
    plt.style.use('dark_background')
    plt.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='Today', alpha=0.7)
    plt.plot(range(24), [avg_hourly[i]/60 for i in range(24)], color='#FEE75C', marker='o', label=f'{divisor}-day Avg', linewidth=2)
    plt.title(f"Activity Analysis: {user.display_name}", color='white', fontsize=14)
    plt.xlabel("Hour (0-23)", color='white')
    plt.ylabel("Minutes", color='white')
    plt.xticks(range(24), [f"{i}h" for i in range(24)], color='white')
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.3)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', transparent=True)
    buf.seek(0)
    plt.close()
    
    total_today = sum(today_status.values())
    efficiency = f"平均の **{(total_today/avg_total_day*100):.1f}%**" if avg_total_day > 0 else "データ蓄積中"
    file = discord.File(buf, filename="graph.png")
    embed = discord.Embed(title=f"{title_prefix}", color=0x5865F2, timestamp=now)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.add_field(name="📊 活動効率", value=efficiency, inline=False)
    embed.add_field(name="🟢 オンライン", value=format_time(today_status["オンライン"]), inline=True)
    embed.add_field(name="🌙 退席中", value=format_time(today_status["退席中"]), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_time(today_status["取り込み中"]), inline=True)
    embed.add_field(name="⌛ 合計", value=format_time(total_today), inline=False)
    embed.set_image(url="attachment://graph.png")
    return embed, file

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    for guild in bot.guilds:
        await guild.chunk()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    for guild in bot.guilds:
        for m in guild.members:
            if not m.bot:
                user_status_start[m.id] = {'status': str(m.status), 'time': now}

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    uid = after.id

    configs = get_user_configs()
    if uid in configs:
        if uid in user_status_start:
            prev = user_status_start[uid]
            dur = (now - prev['time']).total_seconds()
            if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
                st_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
                summary, cid = st_map[prev['status']]
                add_to_calendar(f"{summary} [{after.display_name}]", prev['time'], now, cid)
                channel = bot.get_channel(configs[uid])
                if channel: await channel.send(f"🔔 **{after.display_name}** が **{summary}** になりました。")
    
    user_status_start[uid] = {'status': str(after.status), 'time': now}

@bot.tree.command(name="register", description="通知先を登録します")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    save_user_config(user.id, channel.id, user.display_name)
    await interaction.response.send_message(f"✅ {user.display_name} を登録しました。", ephemeral=True)

@bot.tree.command(name="report", description="活動レポートを表示します")
@app_commands.describe(member="対象のユーザー")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    embed, file = await create_report_data(target.id, f"📑 {target.display_name} の活動状況レポート")
    if embed: await interaction.followup.send(embed=embed, file=file)
    else: await interaction.followup.send("❌ データがありません。")

@bot.tree.command(name="status", description="ステータス詳細を表示します")
@app_commands.describe(member="対象のユーザー")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(target.id, {'status': str(target.status), 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    embed = discord.Embed(title=f"👤 {target.display_name} の状態", color=0x2ecc71 if info['status'] == "online" else 0x95a5a6, timestamp=now)
    embed.add_field(name="現在", value=f"**{st_map.get(info['status'], '不明')}**")
    embed.add_field(name="継続時間", value=format_time(elapsed.total_seconds()))
    await interaction.followup.send(embed=embed)

keep_alive()
bot.run(TOKEN)
