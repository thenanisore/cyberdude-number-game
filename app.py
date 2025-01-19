import logging
import os
import re
import sys
import redis

from dotenv import load_dotenv
from telegram import ReplyKeyboardRemove, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from urllib.parse import urlparse

from log_utils import ContextFilter, LoggerContext

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - [group_id=%(group_id)s] - %(message)s"))
handler.addFilter(ContextFilter(['group_id']))

logger.addHandler(handler)

# Load environment variables
load_dotenv()

TOKEN = os.getenv('TOKEN')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = os.getenv('REDIS_PORT', 6379)

def setup_redis():
    if os.environ.get("REDIS_URL"):
        # Heroku Redis setup
        url = urlparse(os.environ.get("REDIS_URL"))
        logger.info(f"Connecting to Heroku Redis at {url.hostname}:{url.port}")
        return redis.Redis(host=url.hostname, port=url.port, password=url.password, ssl=(url.scheme == "rediss"), ssl_cert_reqs=None)
    else:
        # Local Redis setup
        logger.info(f"Connecting to local Redis at {REDIS_HOST}:{REDIS_PORT}")
        return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)

r = setup_redis()

# Load current number from Redis or start from 0
num_p = re.compile(r'(\d+)!')

PUBLIC_CHANNEL = 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user to associate a public channel with the current group."""
    group_id = update.message.chat_id
    with LoggerContext(logger, {"group_id": update.message.chat_id}):
        # Check if already initialized
        channel_id = r.get(f"group:{group_id}:channel_id")
        if channel_id:
            logger.info(f"The game has already been initialized, ending the conversation.")
            await update.message.reply_text('The game has already been initialized for this group.')
            return ConversationHandler.END

        logger.info(f"Starting the initialization for group {group_id}")

        await update.message.reply_text(
            'Please associate a public channel with this group to start the game.'
            'The channel will be used to post the submissions.'
        )

        return PUBLIC_CHANNEL


async def is_bot_admin_in_channel(group_id: int, channel_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the bot has admin permissions in a public channel."""
    with LoggerContext(logger, {"group_id": group_id}):
        try:
            # Check if the bot can send a message to the channel
            test_message = await context.bot.send_message(
                chat_id=channel_id,
                text="Checking admin permissions. This message will self-delete."
            )
            # If the message is sent successfully, delete it
            await test_message.delete()
            return True
        except Exception as e:
            logger.error(f"Bot admin check failed for channel {channel_id}: {e}")
            return False


async def public_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initialize the game with a public channel for posting submissions."""
    group_id = update.message.chat_id
    with LoggerContext(logger, {"group_id": update.message.chat_id}):
        logger.info(f"Initializing the game")

        channel_id = update.message.text

        # Check if the channel exists and the bot is an admin of the channel
        try:
            logger.info(f"Checking the channel {channel_id}")
            if not channel_id and not channel_id.startswith('@'):
                raise Exception('The channel name is empty or does not start with @.')

            if not await is_bot_admin_in_channel(group_id, channel_id, context):
                raise Exception('The bot must be an admin of the channel.')
        except Exception as e:
            logger.error(f"Could not init the game: {e}")
            await update.message.reply_text(f'Could not start the bot: {e}')
            return ConversationHandler.END

        r.set(f"group:{group_id}:channel_id", channel_id)
        r.set(f"group:{group_id}:current_number", 0)

        await update.message.reply_text(f'The channel {channel_id} has been associated with this group.')

        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    group_id = update.message.chat_id
    with LoggerContext(logger, {"group_id": group_id}):
        user = update.message.from_user

        logger.info(f"{user.username} canceled the initialization.")
        await update.message.reply_text(
            "Initialization cancelled.", reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a number manually."""


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
    last_found_msg = f'💎 Last found number: {current_number}'

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
    group_id = update.message.chat_id
    with LoggerContext(logger, {"group_id": group_id}):
        caption = update.message.caption
        logger.info(f"Checking the submitted number, caption: {caption}")
        if caption:
            current_number = int(r.get(f"group:{group_id}:current_number"))
            requested_num = num_p.match(caption.strip())
            requested_num = int(requested_num.group(1)) if requested_num else None
            if requested_num and requested_num == current_number + 1:
                user_id = update.message.from_user.id
                # Store user submission in Redis set
                user_key = f"group:{group_id}:user_submissions:{user_id}"
                r.sadd(user_key, requested_num)
                # Prepare a message to post in the public channel
                channel_id = r.get(f"group:{group_id}:channel_id").decode("utf-8")
                posted_msg = await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=update.message.photo[-1],
                    caption=caption,
                )
                # Store message link in message history hash
                r.hset(f"group:{group_id}:message_history", requested_num, f'{channel_id}:{posted_msg.link}')
                # Increment and persist current_number
                current_number += 1
                r.set(f"group:{group_id}:current_number", current_number)

                await update.message.reply_markdown_v2(f'🎉 [Found {requested_num}]({posted_msg.link})\! 🎉')
            else:
                logger.error(f"The number is incorrect, expected {current_number + 1}, not {requested_num}.")
                await update.message.reply_text(f'Wrong number! Expected {current_number + 1}, not {requested_num}.')
        else:
            logger.error(f"The caption is empty for message {update.message.message_id}")


def main() -> None:
    """Start the bot."""
    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PUBLIC_CHANNEL: [MessageHandler(filters.TEXT, public_channel)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(conv_handler)

    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(
        filters.CAPTION & (filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        submit_number
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
