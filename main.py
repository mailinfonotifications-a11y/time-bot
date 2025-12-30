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
FONT_PATH = 'font.ttf' # プロジェクト内に置いたフォントファイル名

def setup_font_background():
    try:
        if os.path.exists(FONT_PATH):
            # フォントをmatplotlibに追加
            fm.fontManager.addfont(FONT_PATH)
            prop = fm.FontProperties(fname=FONT_PATH)
            plt.rcParams['font.family'] = prop.get_name()
            print(f"✅ 日本語フォントを適用しました: {prop.get_name()}")
        else:
            print("⚠️ font.ttf が見つかりません。標準フォントを使用します。")
            plt.rcParams['font.family'] = 'sans-serif'
    except Exception as e:
        print(f"Font setup notice: {e}")

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

# --- グローバル変数 ---
user_status_start = {}  # {user_id: {'status': str, 'time': datetime}}
user_configs = {}       # {user_id: channel_id}

# --- Utility ---
def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"**{h}**時間 **{m}**分" if h > 0 else f"**{m}**分"

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    event = {
        'summary': summary,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Asia/Tokyo'},
        'colorId': color_id,
    }
    try:
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✅ カレンダー記録完了: {summary}")
    except Exception:
        print(f"❌ カレンダーエラー: {traceback.format_exc()}")

async def load_configs_from_sheets():
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        global user_configs
        user_configs = {int(r['user_id']): int(r['channel_id']) for r in records}
        print(f"✅ 設定ロード完了: {len(user_configs)} 名のユーザーを監視中")
    except Exception as e:
        print(f"❌ スプレッドシート読み込み失敗: {e}")

async def get_activity_data_from_calendar(start_dt, end_dt, user_id):
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
            if "オンライン" in summary: found_status = "Online"
            elif "退席中" in summary: found_status = "Idle"
            elif "取り込み中" in summary: found_status = "DND"
            
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
    
    # 現在のセッションも加算
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

    # グラフ描画
    plt.figure(figsize=(10, 5))
    plt.style.use('dark_background')
    plt.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='今日', alpha=0.7)
    plt.plot(range(24), [avg_hourly[i]/60 for i in range(24)], color='#FEE75C', marker='o', label=f'{active_days_count}日間平均', linewidth=2)
    plt.title(f"活動分析: {display_name}", color='white')
    plt.xlabel("時間帯", color='white')
    plt.ylabel("分", color='white')
    plt.xticks(range(24), [f"{i}時" for i in range(24)])
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
    embed.set_footer(text="Asia/Tokyo")
    return embed, file

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f"--- [STARTUP] ---")
    await load_configs_from_sheets()
    await bot.tree.sync()
    print(f"✅ 起動完了: {len(bot.guilds)} サーバーで動作中")

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    if after.id not in user_configs: return

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    prev = user_status_start.get(after.id)
    user_status_start[after.id] = {'status': str(after.status), 'time': now}

    if prev:
        dur = (now - prev['time']).total_seconds()
        # 1分以上の滞在のみ記録
        if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
            st_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            st_name, cid = st_map.get(prev['status'], ("不明", "1"))
            
            # [user id]ステータス名/開始時刻
            start_str = prev['time'].strftime('%H:%M:%S')
            event_title = f"[{after.id}] {st_name} / {start_str}"
            
            add_to_calendar(event_title, prev['time'], now, cid)
            
            channel_id = user_configs.get(after.id)
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if channel:
                try: await channel.send(f"🔔 **{after.display_name}** は **{st_name}** になりました。")
                except: pass

# --- Commands ---
@bot.tree.command(name="report", description="活動レポートを表示")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(thinking=True)
    target = member or interaction.user
    embed, file = await create_report_data(target.id, f"📑 活動レポート: {target.display_name}", target.display_name)
    await interaction.followup.send(embed=embed, file=file)

@bot.tree.command(name="status", description="今のステータスを確認")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(target.id, {'status': str(target.status), 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    embed = discord.Embed(title=f"👤 ステータス: {target.display_name}", color=0x5865F2)
    embed.add_field(name="現在の状態", value=st_map.get(info['status'], info['status']))
    embed.add_field(name="経過時間", value=format_time(elapsed.total_seconds()))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="register", description="通知対象として登録")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        cell = sheet.find(str(user.id))
        if cell:
            sheet.update_cell(cell.row, 2, str(channel.id))
        else:
            sheet.append_row([str(user.id), str(channel.id), user.display_name])
        
        user_configs[user.id] = channel.id
        await interaction.followup.send(f"✅ {user.display_name} を登録しました。通知先: {channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ 登録に失敗しました: {e}")

# --- 実行 ---
if __name__ == "__main__":
    Thread(target=run_flask).start()
    setup_font_background() # グラフ用フォント設定
    bot.run(TOKEN)
