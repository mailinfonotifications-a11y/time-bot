import discord
from discord import app_commands
from discord.ext import commands
import datetime
import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- 設定 ---
TOKEN = os.getenv('DISCORD_TOKEN')
CALENDAR_ID = os.getenv('CALENDAR_ID')

# サービスアカウントのキー（環境変数から読み込む）
service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON'))

SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
service = build('calendar', 'v3', credentials=creds)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # スラッシュコマンドを同期
        await self.tree.sync()

bot = MyBot()

# ステータス開始時間を記録する辞書
user_status_start = {}

def add_to_calendar(summary, start_time, end_time, color_id="1"):
    """Googleカレンダーに予定を追加（色指定付き）"""
    event = {
        'summary': summary,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'Asia/Tokyo',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'Asia/Tokyo',
        },
        'colorId': color_id,
    }
    try:
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"DEBUG: Success! Event created: {summary} (Color: {color_id})")
    except Exception as e:
        print(f"Calendar API Error: {e}")

@bot.event
async def on_ready():
    print(f"SYSTEM: Logged in as {bot.user.name}")
    # 起動時の全メンバーのステータスを初期値としてセット
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                user_status_start[member.id] = {
                    'status': member.status,
                    'time': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
                }
    print(f"SYSTEM: Monitoring {len(user_status_start)} users.")

@bot.event
async def on_presence_update(before, after):
    if after.bot:
        return

    # ステータスが変わったかチェック
    if before.status != after.status:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        user_id = after.id
        
        # 前回の記録がある場合
        if user_id in user_status_start:
            prev_info = user_status_start[user_id]
            prev_status = prev_info['status']
            start_time = prev_info['time']
            
            # 滞在時間を計算（秒）
            duration = (now - start_time).total_seconds()
            
            # 1分（60秒）以上の場合のみカレンダーに記録
            if duration >= 60:
                # 日本語名と色の割り当て (10:緑, 5:黄, 11:赤)
                status_map = {
                    "online": ("オンライン", "10"),
                    "idle": ("退席中", "5"),
                    "dnd": ("取り込み中", "11")
                }
                status_jp, color_id = status_map.get(str(prev_status), ("アクティブ", "1"))
                
                # 重なり防止のため終了時間を1秒引く
                end_time = now - datetime.timedelta(seconds=1)
                
                add_to_calendar(status_jp, start_time, end_time, color_id)

        # 新しいステータスを記録
        user_status_start[user_id] = {'status': after.status, 'time': now}

@bot.tree.command(name="status", description="現在のモニタリング状況を確認します")
async def status(interaction: discord.Interaction):
    """現在の状況をEmbedで表示"""
    user_id = interaction.user.id
    status_text = "未記録（ステータスを変えると開始されます）"
    start_time_text = "-"
    embed_color = discord.Color.blue()
    
    if user_id in user_status_start:
        info = user_status_start[user_id]
        # 見た目用のマップ
        display_map = {
            "online": ("🟢 オンライン", discord.Color.green()),
            "idle": ("🌙 退席中", discord.Color.gold()),
            "dnd": ("⛔ 取り込み中", discord.Color.red())
        }
        status_text, embed_color = display_map.get(str(info['status']), ("不明", discord.Color.light_grey()))
        start_time_text = info['time'].strftime('%H:%M:%S')

    # Embedの作成
    embed = discord.Embed(
        title="📊 活動モニタリング状況",
        description=f"{interaction.user.display_name}さんの現在の記録状態です。",
        color=embed_color,
        timestamp=datetime.datetime.now()
    )
    
    embed.add_field(name="現在のステータス", value=status_text, inline=True)
    embed.add_field(name="計測開始時刻", value=start_time_text, inline=True)
    embed.add_field(name="ヒント", value="1分以上同じステータスを維持するとカレンダーに記録されます。", inline=False)
    
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"ID: {interaction.user.id}")

    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)
