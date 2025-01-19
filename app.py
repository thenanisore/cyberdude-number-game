import logging
import os
import re
import sys
from typing import Optional
import redis

from dotenv import load_dotenv
from telegram import Message, ReplyKeyboardRemove, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from urllib.parse import urlparse

from log_utils import ContextFilter, MessageContext

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

num_p = re.compile(r'(\d+)!')

PUBLIC_CHANNEL = 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user to associate a public channel with the current group."""
    group_id = update.message.chat_id
    with MessageContext(logger, {"group_id": update.message.chat_id}):
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


async def is_bot_admin_in_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> bool:
    """Check if the bot has admin permissions in a public channel."""
    with MessageContext(logger, update.message):
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
    with MessageContext(logger, update.message):
        logger.info(f"Initializing the game")

        group_id = update.message.chat_id
        channel_id = update.message.text

        # Check if the channel exists and the bot is an admin of the channel
        try:
            logger.info(f"Checking the channel {channel_id}")
            if not channel_id and not channel_id.startswith('@'):
                raise Exception('The channel name is empty or does not start with @.')
            if not await is_bot_admin_in_channel(update.message, context, channel_id):
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
    with MessageContext(logger, update.message):
        user = update.message.from_user

        logger.info(f"{user.username} canceled the initialization.")
        await update.message.reply_text(
            "Initialization cancelled.", reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        'To start the game, use the /start command and associate a public channel with the group.\n'
        'The channel will be used to post the submissions.\n'
        'To submit a number, send a photo or video with the number in the caption. It should look like this: "123!"\n'
        'To submit an existing number, reply to the message with the submission with the /add command followed by the number.\n'
        'To view the current stats, use the /stats command.'
    )


async def submit_number(message: Message, context: ContextTypes.DEFAULT_TYPE, requested_num: Optional[int]) -> None:
    """Submit a number and update the current number if necessary."""
    with MessageContext(logger, message):
        current_number = int(r.get(f"group:{message.chat_id}:current_number"))
        if requested_num and requested_num == current_number + 1:
            group_id = message.chat_id
            user_id = message.from_user.id
            # Store user submission in Redis set
            user_key = f"group:{group_id}:user_submissions:{user_id}"
            r.sadd(user_key, requested_num)
            # Prepare a message to post in the public channel
            channel_id = r.get(f"group:{group_id}:channel_id").decode("utf-8")
            posted_msg = await context.bot.send_photo(
                chat_id=channel_id,
                photo=message.photo[-1],
                caption=f"💎 Number {requested_num} submitted by {message.from_user.mention_markdown_v2()} 💎",
                parse_mode="MarkdownV2"
            )
            # Store message link in message history hash
            r.hset(f"group:{group_id}:message_history", requested_num, f'{posted_msg.link}')
            # Increment and persist requested number if it's larger than the current number
            current_number = int(r.get(f"group:{group_id}:current_number"))
            if requested_num > current_number:
                logger.info(f"Updating the current number to {requested_num}, was {current_number}")
                r.set(f"group:{group_id}:current_number", requested_num)

            await message.reply_markdown_v2(f'🎉 [Found {requested_num}]({posted_msg.link})\\! 🎉')
        else:
            logger.error(f"The number is incorrect, expected {current_number + 1}, not {requested_num}.")
            await message.reply_text(f'Wrong number! Expected {current_number + 1}, not {requested_num}.')


async def submit_new_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a number to be compared with the current number."""
    with MessageContext(logger, update.message):
        caption = update.message.caption
        logger.info(f"Checking the submitted number, caption: {caption}")
        if caption:
            requested_num = num_p.match(caption.strip())
            requested_num = int(requested_num.group(1)) if requested_num else None
            await submit_number(update.message, context, requested_num)
        else:
            logger.error(f"The caption is empty for message {update.message.message_id}")


async def submit_existing_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit an already posted number."""
    with MessageContext(logger, update.message):
        try:
            number = int(context.args[0])
            logger.info(f"Trying to sumbit an existing number {number}")
            # Check if the message contains a photo or video
            if not update.message.reply_to_message:
                raise Exception("the message is not a reply.")
            if not update.message.reply_to_message.photo:
                raise Exception("the message does not contain a photo.")
            await submit_number(update.message.reply_to_message, context, number)
        except Exception as e:
            logger.error(f"Could not submit the number: {e}")
            await update.message.reply_text(f"Could not submit the number: {e}")
            return


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


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the current number, who found it, and the associated channel."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        try:
            # Fetch the associated public channel
            channel_id = r.get(f"group:{group_id}:channel_id")
            if not channel_id:
                await update.message.reply_text("The game has not been initialized for this group.")
                return
            channel_id = channel_id.decode("utf-8")
            # Fetch the current number
            current_number = int(r.get(f"group:{group_id}:current_number"))
            # Fetch the latest submission's link
            latest_submission_link = r.hget(f"group:{group_id}:message_history", current_number).decode("utf-8")

            await update.message.reply_markdown_v2(
                f"💎 Last found number is **[{current_number}]({latest_submission_link})**\n\n"
                f"📢 Group channel: {channel_id}"
            )

        except Exception as e:
            logger.error(f"Could not get the info: {e}")
            await update.message.reply_text("Could not get the info.")
            return


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

    app.add_handler(CommandHandler("add", submit_existing_number))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(MessageHandler(
        filters.CAPTION & filters.PHOTO & ~filters.COMMAND,
        submit_new_number
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
