import discord
from discord import app_commands
from discord.ext import commands
import datetime
import os
import json
import io
import traceback
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from flask import Flask
from threading import Thread

# --- Flask (Render維持用) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- API連携設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# --- ボット初期設定 ---
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 状態管理・通知管理用
user_status_start = {}  
user_configs = {}       
last_notifications = {} 

def format_time_jp(seconds):
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
    except:
        print(f"❌ カレンダー記録失敗: {traceback.format_exc()}")

async def load_configs_from_sheets():
    """再起動後も設定を復元するための読み込み処理"""
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        global user_configs
        new_configs = {}
        for r in records:
            u_id, g_id, c_id = r.get('user_id'), r.get('guild_id'), r.get('channel_id')
            if u_id and g_id and c_id:
                new_configs[f"{u_id}-{g_id}"] = int(c_id)
        user_configs = new_configs
        print(f"✅ 設定復元完了: {len(user_configs)}件")
    except Exception as e:
        print(f"❌ シートロード失敗: {e}")

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
            found_status = "Online" if "オンライン" in summary else "Idle" if "退席中" in summary else "DND" if "取り込み中" in summary else None
            if not found_status: continue
            st_str, en_str = event['start'].get('dateTime'), event['end'].get('dateTime')
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
    except: return {i: 0 for i in range(24)}, {"Online": 0, "Idle": 0, "DND": 0}, 0

async def create_report_data(user, title_prefix):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_hourly, today_status, _ = await get_activity_data_from_calendar(today_start, now, user.id)
    
    # 現在のセッションも反映
    if user.id in user_status_start:
        info = user_status_start[user.id]
        st_eng = {"online": "Online", "idle": "Idle", "dnd": "DND"}.get(info['status'])
        if st_eng:
            eff_s = max(info['time'], today_start)
            if eff_s < now:
                today_status[st_eng] += (now - eff_s).total_seconds()
                it = eff_s
                while it < now:
                    next_h = (it + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    today_hourly[it.hour] += (min(now, next_h) - it).total_seconds()
                    it = min(now, next_h)

    hist_start = today_start - datetime.timedelta(days=14)
    hist_hourly, _, active_days_count = await get_activity_data_from_calendar(hist_start, today_start, user.id)
    divisor = max(active_days_count, 1)
    avg_hourly = {h: sec / divisor for h, sec in hist_hourly.items()}
    avg_total_day = sum(avg_hourly.values())

    # --- グラフ描画（画像内は完全英語） ---
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#0b0e14')
    ax.set_facecolor('#0b0e14')
    plt.rcParams['font.family'] = 'sans-serif'
    
    ax.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='Today', width=0.7, alpha=0.9, zorder=3)
    ax.plot(range(24), [avg_hourly[i]/60 for i in range(24)], color='#FEE75C', marker='o', label='Average (14d)', linewidth=2, markersize=5, zorder=4)
    
    ax.set_title(f"ACTIVITY ANALYSIS: @{user.name}", color='white', pad=20, fontsize=16, fontweight='bold')
    ax.set_xlabel("Time (24h)", color='#b9bbbe', fontsize=11)
    ax.set_ylabel("Stay Time (min)", color='#b9bbbe', fontsize=11)
    
    ax.set_xticks(range(24))
    ax.tick_params(axis='both', colors='#b9bbbe', labelsize=9)
    ax.grid(axis='y', color='#2f3136', linestyle='-', alpha=0.3, zorder=0)
    for spine in ax.spines.values(): spine.set_visible(False)
    ax.legend(frameon=False, loc='upper left')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', facecolor='#0b0e14', bbox_inches='tight', dpi=120)
    buf.seek(0)
    plt.close()
    
    # --- レポート本文（ここは日本語） ---
    total_today = sum(today_status.values())
    eff_val = (total_today/avg_total_day*100) if avg_total_day > 0 else 0
    file = discord.File(buf, filename="graph.png")
    
    embed = discord.Embed(title=title_prefix, color=0x5865F2, timestamp=now)
    embed.add_field(name="📊 活動効率", value=f"平均の **{eff_val:.1f}%**", inline=False)
    embed.add_field(name="🟢 オンライン", value=format_time_jp(today_status["Online"]), inline=True)
    embed.add_field(name="🌙 退席中", value=format_time_jp(today_status["Idle"]), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_time_jp(today_status["DND"]), inline=True)
    embed.add_field(name="⏱️ 今日これまでの合計", value=format_time_jp(total_today), inline=False)
    embed.set_image(url="attachment://graph.png")
    return embed, file

@bot.event
async def on_ready():
    await load_configs_from_sheets()
    await bot.tree.sync()
    print(f"✅ Bot Online - All functions active")

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    
    # カレンダー記録
    prev = user_status_start.get(after.id)
    user_status_start[after.id] = {'status': str(after.status), 'time': now}
    
    if prev:
        dur = (now - prev['time']).total_seconds()
        if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
            st_map_cal = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            st_name_prev, cid = st_map_cal.get(prev['status'], ("不明", "1"))
            add_to_calendar(f"[{after.id}] {st_name_prev}", prev['time'], now, cid)

    # 通知送信（ニックネーム & 日本語）
    st_display = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    current_status_text = st_display.get(str(after.status), "⚪ オフライン")
    
    for guild in bot.guilds:
        member = guild.get_member(after.id)
        if not member: continue
        
        config_key = f"{after.id}-{guild.id}"
        target_channel_id = user_configs.get(config_key)
        if not target_channel_id: continue
        
        lock_key = f"notify-{config_key}"
        last = last_notifications.get(lock_key)
        
        if last is None or (now - last).total_seconds() >= 3:
            last_notifications[lock_key] = now
            channel = bot.get_channel(target_channel_id)
            if channel:
                await channel.send(f"🔔 **{after.display_name}** は **{current_status_text}** になりました。")

@bot.tree.command(name="register", description="通知先を登録")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        row_idx = None
        for i, r in enumerate(records, 2):
            if str(r.get('user_id')) == str(user.id) and str(r.get('guild_id')) == str(interaction.guild_id):
                row_idx = i
                break
        if row_idx: sheet.update_cell(row_idx, 3, str(channel.id))
        else: sheet.append_row([str(user.id), str(interaction.guild_id), str(channel.id), user.display_name])
        user_configs[f"{user.id}-{interaction.guild_id}"] = channel.id
        await interaction.followup.send(f"✅ {user.display_name} の通知先を設定しました。")
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}")

@bot.tree.command(name="report", description="活動レポートを表示")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    try:
        target = member or interaction.user
        embed, file = await create_report_data(target, f"📑 今日の活動状況レポート: {target.display_name}")
        await interaction.followup.send(embed=embed, file=file)
    except Exception:
        await interaction.followup.send("レポート作成エラー。")

if __name__ == "__main__":
    Thread(target=run_flask).start()
    bot.run(TOKEN)
