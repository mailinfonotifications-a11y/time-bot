import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
import io
import traceback
import subprocess
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from flask import Flask
from threading import Thread

# --- 日本語フォント自動設定 ---
def setup_japanese_font():
    try:
        # Linux(Render)環境に日本語フォントをインストール
        # プロセスをブロックしないよう、fonts-noto-cjkを試みる
        jp_fonts = [f.name for f in fm.fontManager.ttflist if 'CJK' in f.name or 'Japanese' in f.name]
        if not jp_fonts:
            subprocess.run(["apt-get", "update"], check=False)
            subprocess.run(["apt-get", "install", "-y", "fonts-noto-cjk"], check=False)
            fm._rebuild()
        
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'DejaVu Sans', 'Arial']
    except:
        print("Font setup notice: Using system defaults.")

setup_japanese_font()

# --- Flask (Render用起動維持) ---
app = Flask('')
@app.route('/')
def home(): return "Bot Status: Online"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- 認証と初期設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)
gc = gspread.authorize(creds)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.daily_task.start()
        await self.tree.sync()

    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
    async def daily_task(self):
        try:
            sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
            records = sheet.get_all_records()
            for r in records:
                u_id, c_id = int(r['user_id']), int(r['channel_id'])
                channel = self.get_channel(c_id)
                if channel:
                    embed, file = await create_report_data(u_id, "📅 Daily Activity Report")
                    if embed: await channel.send(embed=embed, file=file)
        except Exception as e:
            print(f"❌ Daily Task Error: {e}")

bot = MyBot()
user_status_start = {}

def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"**{h}**h **{m}**m" if h > 0 else f"**{m}**m"

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    print(f"--- [LOG] Adding to Calendar: {summary} ---")
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✅ Calendar Success: {summary}")
    except Exception:
        print(f"❌ Calendar Error:\n{traceback.format_exc()}")

async def get_activity_data_from_calendar(start_dt, end_dt, target_name=None):
    try:
        events_result = service.events().list(
            calendarId=CALENDAR_ID, 
            timeMin=start_dt.isoformat(), 
            timeMax=end_dt.isoformat(), 
            singleEvents=True, 
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        hourly_data = {i: 0 for i in range(24)}
        status_totals = {"Online": 0, "Idle": 0, "DND": 0}
        active_days = set()
        
        for event in events:
            summary = event.get('summary', '')
            if target_name and f"[{target_name}]" not in summary: continue
            
            # 日本語/英語の両方のステータス表記に対応
            found_status = None
            if any(k in summary for k in ["Online", "オンライン"]): found_status = "Online"
            elif any(k in summary for k in ["Idle", "退席中"]): found_status = "Idle"
            elif any(k in summary for k in ["DND", "取り込み中"]): found_status = "DND"
            
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
    except Exception as e:
        print(f"⚠️ Calendar Read Error: {e}")
        return {i: 0 for i in range(24)}, {"Online": 0, "Idle": 0, "DND": 0}, 0

async def create_report_data(user_id, title_prefix):
    user = bot.get_user(user_id)
    if not user: return None, None
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 今日のデータ取得
    today_hourly, today_status, _ = await get_activity_data_from_calendar(today_start, now, user.display_name)
    
    # 現在のセッションをリアルタイム加算
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

    # 過去14日間の平均取得
    hist_start = today_start - datetime.timedelta(days=14)
    hist_hourly, _, active_days_count = await get_activity_data_from_calendar(hist_start, today_start, user.display_name)
    
    divisor = active_days_count
    avg_hourly = {h: sec / max(divisor, 1) for h, sec in hist_hourly.items()}
    avg_total_day = sum(avg_hourly.values())

    # --- グラフ描画 ---
    plt.figure(figsize=(10, 5))
    plt.style.use('dark_background')
    avg_label = f'{divisor}-day Avg'
    
    plt.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='Today', alpha=0.7)
    plt.plot(range(24), [avg_hourly[i]/60 for i in range(24)], color='#FEE75C', marker='o', label=avg_label, linewidth=2)
    
    plt.title(f"Activity Analysis: {user.display_name}", color='white', fontsize=14)
    plt.xlabel("Hour", color='white')
    plt.ylabel("Minutes", color='white')
    plt.xticks(range(24), [f"{i}h" for i in range(24)], color='white')
    plt.legend()
    plt.grid(axis='y', linestyle='--', alpha=0.3)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', transparent=True)
    buf.seek(0)
    plt.close()
    
    total_today = sum(today_status.values())
    eff_val = (total_today/avg_total_day*100) if avg_total_day > 0 else 0
    efficiency = f"**{eff_val:.1f}%** of average" if avg_total_day > 0 else "Data accumulating..."
    
    file = discord.File(buf, filename="graph.png")
    embed = discord.Embed(title=title_prefix, color=0x5865F2, timestamp=now)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.add_field(name="📊 Efficiency", value=efficiency, inline=False)
    embed.add_field(name="🟢 Online", value=format_time(today_status["Online"]), inline=True)
    embed.add_field(name="🌙 Idle", value=format_time(today_status["Idle"]), inline=True)
    embed.add_field(name="⛔ DND", value=format_time(today_status["DND"]), inline=True)
    embed.add_field(name="⌛ Total", value=format_time(total_today), inline=False)
    embed.set_image(url="attachment://graph.png")
    return embed, file

@bot.event
async def on_ready():
    print(f"--- [BOT STARTUP SUCCESS] ---")
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    for guild in bot.guilds:
        await guild.chunk()
        for m in guild.members:
            if not m.bot:
                user_status_start[m.id] = {'status': str(m.status), 'time': now}
    print(f"Monitoring {len(user_status_start)} users.")

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    uid = after.id

    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        configs = {int(r['user_id']): int(r['channel_id']) for r in records}
    except: return

    if uid in configs:
        if uid in user_status_start:
            prev = user_status_start[uid]
            dur = (now - prev['time']).total_seconds()
            # 1分以上の滞在のみカレンダーへ送信
            if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
                st_map = {"online": ("Online", "10"), "idle": ("Idle", "5"), "dnd": ("DND", "11")}
                st_name, cid = st_map.get(prev['status'], ("Unknown", "1"))
                event_title = f"[{after.display_name}] {st_name}"
                add_to_calendar(event_title, prev['time'], now, cid)
                
                channel = bot.get_channel(configs[uid])
                if channel: await channel.send(f"🔔 **{after.display_name}** is now **{st_name}**.")
    
    user_status_start[uid] = {'status': str(after.status), 'time': now}

# --- Discord Commands ---
@bot.tree.command(name="report", description="活動レポートを表示します")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    # 即座にレスポンス保留（404回避）
    await interaction.response.defer(thinking=True)
    try:
        target = member or interaction.user
        embed, file = await create_report_data(target.id, f"📑 Activity Report: {target.display_name}")
        if embed:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send("❌ No activity data recorded yet.")
    except Exception:
        print(traceback.format_exc())
        await interaction.followup.send("❌ Error generating report. Check logs.")

@bot.tree.command(name="status", description="現在のステータス詳細を表示します")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(thinking=True)
    target = member or interaction.user
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(target.id, {'status': str(target.status), 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 Online", "idle": "🌙 Idle", "dnd": "⛔ DND", "offline": "⚪ Offline"}
    embed = discord.Embed(title=f"👤 Status Detail: {target.display_name}", color=0x2ecc71 if info['status'] == "online" else 0x95a5a6, timestamp=now)
    embed.add_field(name="Current", value=f"**{st_map.get(info['status'], 'Unknown')}**")
    embed.add_field(name="Elapsed", value=format_time(elapsed.total_seconds()))
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="register", description="ユーザーと通知チャンネルを紐付けます")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        cell = sheet.find(str(user.id))
        if cell:
            sheet.update_cell(cell.row, 2, str(channel.id))
            sheet.update_cell(cell.row, 3, user.display_name)
        else:
            sheet.append_row([str(user.id), str(channel.id), user.display_name])
        await interaction.response.send_message(f"✅ Registered {user.display_name} to {channel.name}", ephemeral=True)
    except Exception as e:
        print(f"Register Error: {e}")
        await interaction.response.send_message("❌ Registration failed.", ephemeral=True)

keep_alive()
bot.run(TOKEN)
