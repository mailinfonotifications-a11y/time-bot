import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
import io
import asyncio
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from flask import Flask
from threading import Thread

# --- Flask (サーバー維持用) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is Online"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Google API設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))
SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# --- Discord Bot設定 ---
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

user_status_start = {}  
user_configs = {}       
last_notifications = {} 

def format_time_jp(seconds):
    h, m = divmod(int(seconds // 60), 60)
    return f"**{h}**時間 **{m}**分" if h > 0 else f"**{m}**分"

async def load_configs_from_sheets():
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        global user_configs
        user_configs = {f"{r['user_id']}-{r['guild_id']}": int(r['channel_id']) for r in records if r.get('user_id')}
        print(f"✅ スプレッドシートから設定を復元しました（{len(user_configs)}件）")
    except Exception as e:
        print(f"❌ シート読み込み失敗: {e}")

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
        target = f"[{user_id}]"
        for event in events:
            summary = event.get('summary', '')
            if target not in summary: continue
            st = "Online" if "オンライン" in summary else "Idle" if "退席中" in summary else "DND"
            s = datetime.datetime.fromisoformat(event['start']['dateTime'].replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            e = datetime.datetime.fromisoformat(event['end']['dateTime'].replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            curr, limit = max(s, start_dt), min(e, end_dt)
            if curr < limit:
                active_days.add(curr.date())
                status_totals[st] += (limit - curr).total_seconds()
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
    avg_total_day = sum(hist_hourly.values()) / divisor

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#0b0e14')
    ax.set_facecolor('#0b0e14')
    plt.rcParams['font.family'] = 'sans-serif'
    ax.bar(range(24), [today_hourly[i]/60 for i in range(24)], color='#5865F2', label='Today', width=0.7, alpha=0.9, zorder=3)
    ax.plot(range(24), [(hist_hourly[i]/divisor)/60 for i in range(24)], color='#FEE75C', marker='o', label='Average (14d)', linewidth=2, markersize=5, zorder=4)
    ax.set_title(f"ACTIVITY ANALYSIS: @{user.name}", color='white', pad=20, fontsize=16, fontweight='bold')
    ax.set_xlabel("Time (24h)", color='#b9bbbe', fontsize=11)
    ax.set_ylabel("Stay Time (min)", color='#b9bbbe', fontsize=11)
    ax.set_xticks(range(24)); ax.tick_params(axis='both', colors='#b9bbbe', labelsize=9)
    ax.grid(axis='y', color='#2f3136', linestyle='-', alpha=0.3, zorder=0)
    for spine in ax.spines.values(): spine.set_visible(False)
    ax.legend(frameon=False, loc='upper left')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', facecolor='#0b0e14', bbox_inches='tight', dpi=120); buf.seek(0); plt.close()

    total_today = sum(today_status.values())
    eff_val = (total_today/avg_total_day*100) if avg_total_day > 0 else 0
    embed = discord.Embed(title=title_prefix, color=0x5865F2, timestamp=now)
    embed.add_field(name="📊 活動効率", value=f"平均の **{eff_val:.1f}%**", inline=False)
    embed.add_field(name="🟢 オンライン", value=format_time_jp(today_status["Online"]), inline=True)
    embed.add_field(name="🌙 退席中", value=format_time_jp(today_status["Idle"]), inline=True)
    embed.add_field(name="⛔ 取り込み中", value=format_time_jp(today_status["DND"]), inline=True)
    embed.add_field(name="⏱️ 今日これまでの合計", value=format_time_jp(total_today), inline=False)
    embed.set_image(url="attachment://graph.png")
    return embed, discord.File(buf, filename="graph.png")

@tasks.loop(seconds=10)
async def daily_report_task():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if now.hour == 23 and now.minute == 57 and 30 <= now.second < 40:
        print("⏰ 自動レポート一括送信を開始します...")
        for key, channel_id in user_configs.items():
            try:
                u_id, g_id = map(int, key.split('-'))
                guild = bot.get_guild(g_id)
                member = guild.get_member(u_id) if guild else None
                channel = bot.get_channel(channel_id)
                if member and channel:
                    embed, file = await create_report_data(member, f"📑 定期レポート: {member.display_name}")
                    await channel.send(embed=embed, file=file)
                    await asyncio.sleep(5)
            except Exception as e: print(f"❌ 自動レポート送信失敗: {e}")
        await asyncio.sleep(60)

@bot.event
async def on_ready():
    await load_configs_from_sheets()
    await bot.tree.sync()
    if not daily_report_task.is_running(): daily_report_task.start()
    print(f"✅ Bot Ready: {bot.user.name} / ステータス監視を開始しました")

@bot.event
async def on_presence_update(before, after):
    if after.bot or before.status == after.status: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    
    prev = user_status_start.get(after.id)
    user_status_start[after.id] = {'status': str(after.status), 'time': now}
    
    # --- ここがカレンダー記録の核心部分 ---
    if prev:
        dur = (now - prev['time']).total_seconds()
        # 1分（60秒）以上の滞在があった場合のみ記録
        if prev['status'] in ["online", "idle", "dnd"] and dur >= 60:
            st_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            st_n, cid = st_map.get(prev['status'], ("不明", "1"))
            try:
                event = {
                    'summary': f"[{after.id}] {st_n}",
                    'start': {'dateTime': prev['time'].isoformat(), 'timeZone': 'Asia/Tokyo'},
                    'end': {'dateTime': now.isoformat(), 'timeZone': 'Asia/Tokyo'},
                    'colorId': cid
                }
                calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                print(f"✅ カレンダー記録成功: {after.display_name} ({st_n} / {int(dur/60)}分)")
            except Exception as e:
                print(f"❌ カレンダー記録失敗: {e}")
    
    # --- Discord通知 ---
    st_d = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    for guild in bot.guilds:
        c_id = user_configs.get(f"{after.id}-{guild.id}")
        if not c_id: continue
        lock_key = f"{after.id}-{guild.id}"
        if lock_key in last_notifications and (now - last_notifications[lock_key]).total_seconds() < 3: continue
        last_notifications[lock_key] = now
        channel = bot.get_channel(c_id)
        if channel: await channel.send(f"🔔 **{after.display_name}** は **{st_d.get(str(after.status), '⚪ オフライン')}** になりました。")

@bot.tree.command(name="register", description="通知先登録")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1
        records = sheet.get_all_records()
        row = next((i for i, r in enumerate(records, 2) if str(r['user_id']) == str(user.id) and str(r['guild_id']) == str(interaction.guild_id)), None)
        if row: sheet.update_cell(row, 3, str(channel.id))
        else: sheet.append_row([str(user.id), str(interaction.guild_id), str(channel.id), user.display_name])
        user_configs[f"{user.id}-{interaction.guild_id}"] = channel.id
        await interaction.followup.send(f"✅ {user.display_name} の設定完了")
    except Exception as e: await interaction.followup.send(f"❌ エラー: {e}")

@bot.tree.command(name="status", description="現在のステータス")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    c_map = {"online": 0x57F287, "idle": 0xFEE75C, "dnd": 0xED4245, "offline": 0x95A5A6}
    l_map = {"online": "🟢 オンライン", "idle": "🌙 退席中", "dnd": "⛔ 取り込み中", "offline": "⚪ オフライン"}
    embed = discord.Embed(title=f"Status: {target.display_name}", color=c_map.get(str(target.status), 0x95A5A6))
    embed.add_field(name="現在", value=l_map.get(str(target.status), "⚪ オフライン"), inline=True)
    if target.id in user_status_start:
        info = user_status_start[target.id]
        embed.add_field(name="開始時刻", value=info['time'].strftime("%H:%M:%S"), inline=True)
        embed.add_field(name="継続時間", value=format_time_jp((now - info['time']).total_seconds()), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="report", description="活動レポート")
async def report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    try:
        target = member or interaction.user
        embed, file = await create_report_data(target, f"📑 レポート: {target.display_name}")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as e: await interaction.followup.send(f"❌ レポート作成エラー")

if __name__ == "__main__":
    Thread(target=run_flask).start()
    bot.run(TOKEN)
