import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import os
import json
import io
import matplotlib.pyplot as plt
import gspread  # 追加
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
# スプレッドシートIDを環境変数から取得
SPREADSHEET_KEY = os.getenv('SPREADSHEET_KEY')
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets'  # スコープ追加
]
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

# gspreadの初期化
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SPREADSHEET_KEY).sheet1

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
        # スプレッドシートから全員の通知先を取得してレポートを送る
        try:
            records = sheet.get_all_records()
            for r in records:
                u_id = int(r['user_id'])
                c_id = int(r['channel_id'])
                channel = self.get_channel(c_id)
                if channel:
                    embed, file = await create_report_data(u_id, "📅 本日の最終活動レポート")
                    if embed:
                        await channel.send(embed=embed, file=file)
        except Exception as e:
            print(f"DAILY TASK ERROR: {e}")

bot = MyBot()

# --- データ管理（スプレッドシート操作用） ---
user_status_start = {}

def save_user_config(user_id, channel_id, name):
    """ユーザーごとの通知先をスプレッドシートに保存"""
    try:
        cell = sheet.find(str(user_id))
        if cell:
            sheet.update_cell(cell.row, 2, str(channel_id))
            sheet.update_cell(cell.row, 3, name)
        else:
            sheet.append_row([str(user_id), str(channel_id), name])
    except Exception as e:
        print(f"SHEET SAVE ERROR: {e}")

def get_user_configs():
    """スプレッドシートから全設定を取得"""
    try:
        records = sheet.get_all_records()
        return {int(r['user_id']): int(r['channel_id']) for r in records}
    except:
        return {}

# --- 共通ロジック (変更なし) ---
def format_time(seconds):
    h, m = divmod(int(seconds // 60), 60)
    if h > 0: return f"**{h}**時間**{m}**分"
    return f"**{m}**分"

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

async def get_activity_data_from_calendar(start_dt, end_dt):
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
        status_totals = {"オンライン": 0, "退席中": 0, "取り込み中": 0}
        active_days = set()
        for event in events:
            summary = event.get('summary', '')
            # 「ステータス [名前]」形式からステータス部分を抽出
            found_status = None
            for key in status_totals.keys():
                if key in summary:
                    found_status = key
                    break
            if not found_status: continue
            s_str = event['start'].get('dateTime', event['start'].get('date'))
            e_str = event['end'].get('dateTime', event['end'].get('date'))
            s = datetime.datetime.fromisoformat(s_str.replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            e = datetime.datetime.fromisoformat(e_str.replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            curr = max(s, start_dt)
            limit = min(e, end_dt)
            if curr < limit:
                active_days.add(curr.date())
                dur = (limit - curr).total_seconds()
                status_totals[found_status] += dur
                it = curr
                while it < limit:
                    next_h = (it + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    hourly_data[it.hour] += (min(limit, next_h) - it).total_seconds()
                    it = min(limit, next_h)
        return hourly_data, status_totals, len(active_days)
    except Exception as e:
        print(f"FETCH ERROR: {e}")
        return {i: 0 for i in range(24)}, {"オンライン": 0, "退席中": 0, "取り込み中": 0}, 0

async def create_report_data(user_id, title_prefix):
    user = bot.get_user(user_id)
    if not user: return None, None
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_hourly, today_status, _ = await get_activity_data_from_calendar(today_start, now)
    if user_id in user_status_start:
        info = user_status_start[user_id]
        st_map = {"online": "オンライン", "idle": "退席中", "dnd": "取り込み中"}
        st_jp = st_map.get(str(info['status']))
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
    divisor = active_days_count if active_days_count > 0 else 1
    avg_hourly = {h: sec / divisor for h, sec in hist_hourly.items()}
    avg_total_day = sum(avg_hourly.values())
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
    embed.add_field(name="⌛ 今日これまでの合計", value=format_time(total_today), inline=False)
    embed.set_image(url="attachment://graph.png")
    embed.set_footer(text=f"Past {divisor} active days used for average")
    return embed, file

# --- イベント ---
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

    # 登録されているユーザーの通知先を取得
    configs = get_user_configs()
    if uid not in configs: return
    target_channel_id = configs[uid]

    if uid in user_status_start:
        prev = user_status_start[uid]
        dur = (now - prev['time']).total_seconds()
        if str(prev['status']) in ["online", "idle", "dnd"] and dur >= 60:
            st_map = {"online": ("オンライン", "10"), "idle": ("退席中", "5"), "dnd": ("取り込み中", "11")}
            summary, cid = st_map[str(prev['status'])]
            
            # ステータス + [人]
            event_title = f"{summary} [{after.display_name}]"
            add_to_calendar(event_title, prev['time'], now, cid)
            
            # 通知
            channel = bot.get_channel(target_channel_id)
            if channel:
                await channel.send(f"🔔 **{after.display_name}** が **{summary}** になりました。")
                
    user_status_start[uid] = {'status': after.status, 'time': now}

# --- コマンド ---
@bot.tree.command(name="register", description="人と言語チャンネルを登録します")
@app_commands.describe(user="登録する人", channel="通知するチャンネル")
async def register(interaction: discord.Interaction, user: discord.Member, channel: discord.TextChannel):
    save_user_config(user.id, channel.id, user.display_name)
    await interaction.response.send_message(f"✅ {user.display_name} の通知先を {channel.mention} に登録しました。", ephemeral=True)

@bot.tree.command(name="report", description="今日の活動詳細レポートを表示します")
async def report(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    embed, file = await create_report_data(interaction.user.id, "📑 今日の活動状況レポート")
    if embed: await interaction.followup.send(embed=embed, file=file)

@bot.tree.command(name="status", description="現在のステータスを詳細に表示します")
async def status(interaction: discord.Interaction):
    try: await interaction.response.defer()
    except: return
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    info = user_status_start.get(interaction.user.id, {'status': 'unknown', 'time': now})
    elapsed = now - info['time']
    st_map = {"online": "🟢 オンライン (活動中)", "idle": "🌙 退席中 (離席)", "dnd": "⛔ 取り込み中 (集中)", "offline": "⚪ オフライン"}
    embed = discord.Embed(title="👤 現在のステータス詳細", color=0x2ecc71 if str(info['status']) == "online" else 0x95a5a6, timestamp=now)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="現在の状態", value=f"**{st_map.get(str(info['status']), '不明')}**", inline=False)
    embed.add_field(name="状態開始時刻", value=info['time'].strftime('%Y/%m/%d %H:%M:%S'), inline=True)
    embed.add_field(name="継続時間", value=format_time(elapsed.total_seconds()), inline=True)
    embed.set_footer(text=f"Request by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)

keep_alive()
bot.run(TOKEN)
