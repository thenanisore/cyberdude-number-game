import logging
import os
import re
import redis

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv('TOKEN')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = os.getenv('REDIS_PORT', 6379)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)

# Load current number from Redis or start from 0
current_number = int(r.get('current_number') or 0)
game_channel_id = r.get('game_channel_id')

num_p = re.compile(r'(\d+)!')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text('Привет! Я бот для поиска номеров. Отправь мне фото или видео с номером, который следует за последним найденным.')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")


# async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
#     """Send a message with the current stats."""
#     last_found_msg = f'Последний найденный номер: {current_number}'

#     # Fetch user stats from Redis
#     user_stats_msg = []
#     for user_id in r.scan_iter(match="user_submissions:*"):
#         user_id_str = user_id.decode("utf-8").split(":")[1]
#         user_info = await context.bot.get_chat(user_id_str)
#         username = f"@{user_info.username}" if user_info.username else f"User {user_id_str}"
#         user_numbers = r.smembers(user_id)
#         user_numbers = {int(num) for num in user_numbers}  # Convert to set of numbers
#         total = len(user_numbers)
#         user_stats_msg.append(f'⭐️ {username}: {total} ({", ".join(map(str, user_numbers))}')

#     # Fetch message history from Redis
#     message_history_msg = []
#     for num in r.scan_iter(match="message_history:*"):
#         num_str = num.decode("utf-8").split(":")[1]
#         message_id = r.get(num).decode("utf-8")
#         message_history_msg.append(f'{num_str}: {message_id}')

#     user_stats_msg = "\n".join(user_stats_msg)
#     message_history_msg = "\n".join(message_history_msg)

#     await update.message.reply_text(f"{last_found_msg}\n\n{user_stats_msg}\n\n{message_history_msg}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message with the current stats in the requested format."""
    last_found_msg = f'💎 Последний найденный номер: {current_number}'

    # Fetch user stats from Redis
    user_stats_msg = []
    for user_id in r.scan_iter(match="user_submissions:*"):
        user_id_str = user_id.decode("utf-8").split(":")[1]
        user_numbers = r.smembers(user_id)
        user_numbers = {int(num) for num in user_numbers}  # Convert to set of numbers

        # Get the user's Telegram username (might have changed)
        try:
            user_info = await context.bot.get_chat(user_id_str)
            username = f"@{user_info.username}" if user_info.username else f"User {user_id_str}"
        except Exception as e:
            username = f"User {user_id_str}"  # Fallback if the username can't be retrieved

        # Format user numbers with links to messages
        user_numbers_with_links = [
            f"[{num}](https://t.me/{update.message.chat_id}/{r.get(f'message_history:{num}').decode('utf-8')})"
            for num in sorted(user_numbers)
        ]

        user_stats_msg.append(
            f"⭐️ {username}    {len(user_numbers)} ({', '.join(user_numbers_with_links)})"
        )

    # Fetch message history from Redis
    message_history_msg = []
    for num in r.scan_iter(match="message_history:*"):
        num_str = num.decode("utf-8").split(":")[1]
        message_id = r.get(num).decode("utf-8")
        message_history_msg.append(f'{num_str}: [Message](https://t.me/{context.bot.username}/{message_id})')

    user_stats_msg = "\n".join(user_stats_msg)
    message_history_msg = "\n".join(message_history_msg)

    await update.message.reply_text(
        f"{last_found_msg}\n\n{user_stats_msg}\n\n{message_history_msg}",
        parse_mode="Markdown"  # Use Markdown to enable clickable links
    )


async def submit_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a number to be compared with the current number."""
    global current_number
    caption = update.message.caption
    if caption:
        requested_num = num_p.match(caption.strip())
        requested_num = int(requested_num.group(1)) if requested_num else None
        if requested_num and requested_num == current_number + 1:
            user_id = update.message.from_user.id
            # Store user submission in Redis set
            user_key = f"user_submissions:{user_id}"
            r.sadd(user_key, requested_num)
            # Store message history in Redis hash
            message_key = f"message_history:{requested_num}"
            r.set(message_key, update.message.message_id)
            # Increment and persist current_number in Redis
            current_number += 1
            r.set('current_number', current_number)

            await update.message.pin()
            await update.message.reply_text(f'Нашли номер {requested_num}! 🎉')
        else:
            await update.message.reply_text(f'Неправильный номер! Ищем {current_number + 1}, не {requested_num}.')


def main() -> None:
    """Start the bot."""
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(
        filters.CAPTION & (filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        submit_number
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
