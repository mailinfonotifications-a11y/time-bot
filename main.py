import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
import io
import traceback
import subprocess
import threading
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from flask import Flask
from threading import Thread

# --- Flask (Render用) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- 日本語フォント設定 ---
def setup_font_background():
    try:
        subprocess.run(["apt-get", "update"], check=False)
        subprocess.run(["apt-get", "install", "-y", "fonts-noto-cjk"], check=False)
        fm._rebuild()
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans']
        print("✅ Font setup completed.")
    except Exception as e:
        print(f"Font notice: {e}")

# --- API Settings ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# --- Intents ---
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- グローバル変数 (メモリ管理) ---
user_status_start = {}  # {user_id: {'status': str, 'time': datetime}}
user_configs = {}       # {user_id: channel_id} (API節約のためのキャッシュ)

# --- Utility ---
def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"**{h}**h **{m}**m" if h > 0 else f"**{m}**m"

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✅ Calendar Recorded: {summary}")
    except Exception:
        print(f"❌ Calendar Error:\n{traceback.format_exc()}")

async def load_configs_from_sheets():
    """スプレッドシートから設定をキャッシュに読み込む"""
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        global user_configs
        user_configs = {int(r['user_id']): int(r['channel_id']) for r in records}
        print(f"✅ Configs loaded: {len(user_configs)} users")
    except Exception as e:
        print(f"❌ Failed to load configs: {e}")

async def get_activity_data_from_calendar(start_dt, end_dt, user_id):
    """ユーザーIDを含むイベントのみを抽出して集計"""
    try:
        events_result = calendar_service.events().list(
            calendarId=CALENDAR_ID, timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(), 
            singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        
        hourly_data = {i: 0 for i in range(24)}
        status_totals = {"Online": 0, "Idle": 0, "DND": 0}
        active_days = set()
        
        target_marker = f"[{user_id}]"
        
        for event in events:
            summary = event.get('summary', '')
            if target_marker not in summary: continue
            
            found_status = None
            if "オンライン" in summary or "Online" in summary: found_status = "Online"
            elif "退席中" in summary or "Idle" in summary: found_status = "Idle"
            elif "取り込み中" in summary or "DND" in summary: found_status = "DND"
            
            if not found_status: continue
            
            st_str = event['start'].get('dateTime') or event['start'].get('date')
            en_str = event['end'].get('dateTime') or event['end'].get('date')
            s = datetime.datetime.fromisoformat(st_str.replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            e = datetime.datetime.fromisoformat(en_str.replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            
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
    except:
        return {i: 0 for i in range(24)}, {"Online": 0, "Idle": 0, "DND": 0}, 0

async def create_report_data(user_id, title_prefix, display_name):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    today_hourly, today_status, _ = await get_activity_data_from_calendar(today_start, now, user_id)
    
    # 現在のセッション時間も加算
    if user_id in user_status_start:
        info = user_status_start[user_id]
        st_map = {"online": "Online", "idle": "Idle", "dnd": "DND"}
        st_eng = st_map.get(info['status'])
        if st_eng:
            eff_start = max(info['time'], today_start)
            if eff_start < now:
                dur = (now - eff_start).total_seconds()
                today_status[st_eng] += dur
                it = eff_start
                while it < now:
                    next_h = (it + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    today_hourly[it.hour] += (min(now, next_h) - it).total_seconds()
                    it = min(now, next_h)

    hist_start = today_start - datetime.timedelta(days=14)
    hist_hourly, _, active_days_count = await get_activity_data_from_calendar(hist_start, today_start, user_id)
    divisor = max(active_days_count, 1)
    avg_hourly = {h: sec / divisor for h, sec in hist_hourly.items()}
    avg_total_day = sum(avg_hourly.values())

    plt.figure(figsize=(10, 5))
    plt.style.use('dark_background')
    plt.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='Today', alpha=0.7)
    plt.plot(range(24), [avg_hourly[i]/60 for i in range(24)], color='#FEE75C', marker='o', label=f'{active_days_count}-day Avg', linewidth=2)
    plt.title(f"Activity Analysis: {display_name}", color='white')
    plt.xticks(range(24), [f"{i}h" for i in range(24)])
    plt.legend()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', transparent=True)
    buf.seek(0)
    plt.close()
    
    total_today = sum(today_status.values())
    eff_val = (total_today/avg_total_day*100) if avg_total_day > 0 else 0
    file = discord.File(buf, filename="graph.png")
    embed = discord.Embed(title=title_prefix, color=0x5865F2, timestamp=now)
    embed.add_field(name="📊 稼働効率", value=f"平均の **{eff_val:.1f}%**", inline=False)
    embed.add_field(name="🟢 オンライン", value=format_time(today_status["Online"]), inline=True)
    embed.add_field(name="🌙 退席中", value=format_time(today_status["Idle"]), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_time(today_status["DND"]), inline=True)
    embed.set_image(url="attachment://graph.png")
    return embed, file

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f"--- [STARTUP] ---")
    await load_configs_from_sheets()
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    count = 0
    for guild in bot.guilds:
        try:
            await guild.chunk()
            for m in guild.members:
                if not m.bot and m.id in user_configs:
                    user_status_start[m.id] = {'status': str(m.status), 'time': now}
                    count += 1
        except: continue
    await bot.tree.sync()
    print(f"✅ Monitoring {count} users across {len(bot.guilds)} guilds.")

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    if after.id not in user_configs: return

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    prev = user_status_start.get(after.id)
    user_status_start[after.id] = {'status': str(after.status), 'time': now}

    if prev:
        dur = (now - prev['time']).total_seconds()
        if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
            st_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            st_name, cid = st_map.get(prev['status'], ("不明", "1"))
            
            # [user id]ステータス名/開始時刻
            start_str = prev['time'].strftime('%H:%M:%S')
            event_title = f"[{after.id}] {st_name} / {start_str}"
            
            add_to_calendar(event_title, prev['time'], now, cid)
            
            # 通知
            channel_id = user_configs.get(after.id)
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if channel:
                try: await channel.send(f"🔔 **{after.display_name}** は現在 **{after.status}** です。")
                except: pass

# --- Commands ---
@bot.tree.command(name="report", description="活動レポートを表示")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(thinking=True)
    target = member or interaction.user
    embed, file = await create_report_data(target.id, f"📑 Report: {target.display_name}", target.display_name)
    await interaction.followup.send(embed=embed, file=file)

@bot.tree.command(name="status", description="今のステータスを確認")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(target.id, {'status': str(target.status), 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    embed = discord.Embed(title=f"👤 {target.display_name}", color=0x5865F2)
    embed.add_field(name="状態", value=st_map.get(info['status'], info['status']))
    embed.add_field(name="経過", value=format_time(elapsed.total_seconds()))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="register", description="このチャンネルに通知を登録します")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        cell = sheet.find(str(user.id))
        if cell:
            sheet.update_cell(cell.row, 2, str(channel.id))
        else:
            sheet.append_row([str(user.id), str(channel.id), user.display_name])
        
        user_configs[user.id] = channel.id # キャッシュ更新
        await interaction.followup.send(f"✅ {user.display_name} の通知を {channel.mention} に登録しました。")
    except Exception as e:
        await interaction.followup.send(f"❌ 登録エラー: {e}")

# --- Execution ---
Thread(target=run_flask).start()
Thread(target=setup_font_background).start()
bot.run(TOKEN)
