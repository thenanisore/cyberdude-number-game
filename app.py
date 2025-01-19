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
    with MessageContext(logger, update.message):
        # Check if already initialized
        channel_id = r.get(f"group:{group_id}:channel_id")
        if channel_id:
            logger.info(f"The game has already been initialized, ending the conversation.")
            await update.message.reply_text('The game has already been initialized for this group.')
            return ConversationHandler.END

        logger.info(f"Starting the initialization for group {group_id}")

        await update.message.reply_text(
            'Please post the handler of the public channel that will be associated with this group.\n'
            'The channel will be used to post the submissions.'
        )

        return PUBLIC_CHANNEL


async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is an admin."""
    group_id = update.message.chat_id
    user_id = update.message.from_user.id

    with MessageContext(logger, update.message):
        try:
            # Check permissions
            chat_member = await context.bot.get_chat_member(group_id, user_id)
            return chat_member.status in ['administrator', 'creator']
        except Exception as e:
            logger.error(f"Error checking admin permissions: {e}")
            return False


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
            if not await is_bot_admin_in_channel(update, context, channel_id):
                raise Exception('The bot must be an admin of the channel.')
        except Exception as e:
            logger.error(f"Could not init the game: {e}")
            await update.message.reply_text(f'Could not start the bot: {e}')
            return ConversationHandler.END

        r.set(f"group:{group_id}:channel_id", channel_id)
        r.set(f"group:{group_id}:current_number", 1)

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
        '✨ To start the game, use the /start command and associate a public channel with the group.\n'
        '✨ The channel will be used to post the submissions.\n'
        '✨ To submit a number, send a photo with the number in the caption. It should look like this: "123!"\n'
        '✨ To view the current stats, use the /stats command.'
    )


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a number to be compared with the current number."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        caption = update.message.caption
        logger.info(f"Checking the submitted number, caption: {caption}")
        if caption:
            number_str = num_p.match(caption.strip())
            number = int(number_str.group(1)) if number_str else None

            # Check if the requested number is already submitted
            already_existing_link = r.hget(f"group:{group_id}:message_history", number)
            if already_existing_link:
                already_existing_link = already_existing_link.decode("utf-8")
                await update.message.reply_html(f'Number {number} has already been <a href="{already_existing_link}">submitted</a>!')
                return

            # Check if the number is correct
            current_number = int(r.get(f"group:{group_id}:current_number"))
            if number and number == current_number + 1:
                # Store user submission in Redis set
                user_key = f"group:{group_id}:user_submissions:{update.message.from_user.id}"
                r.sadd(user_key, number)
                # Prepare a message to post in the public channel
                channel_id = r.get(f"group:{group_id}:channel_id").decode("utf-8")
                posted_msg = await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=update.message.photo[-1],
                    caption=f"💎 Number {number} submitted by {update.message.from_user.mention_markdown_v2()} 💎",
                    parse_mode="MarkdownV2"
                )
                # Store message link in message history hash
                r.hset(f"group:{group_id}:message_history", number, f'{posted_msg.link}')
                # Increment and persist requested number if it's larger than the current number
                logger.info(f"Updating the current number to {number}, was {current_number}")
                r.set(f"group:{group_id}:current_number", number)

                await update.message.reply_markdown_v2(f'🎉 [Found {number}]({posted_msg.link})\\! 🎉')
            else:
                logger.error(f"The number is incorrect, expected {current_number + 1}, not {number}.")
                await update.message.reply_text(f'Wrong number! Expected {current_number + 1}, not {number}.')
        else:
            logger.error(f"The caption is empty for message {update.message.message_id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        try:
            # Check if the game has been initialized
            channel_id = r.get(f"group:{group_id}:channel_id")
            if not channel_id:
                await update.message.reply_text("The game has not been initialized for this group.")
                return

            # Fetch user stats
            user_stats = []
            for key in r.scan_iter(match=f"group:{group_id}:user_submissions:*"):
                user_id = key.decode("utf-8").split(":")[3]
                logger.info(f'Collecting stats for user {user_id}')

                user_numbers = {int(num) for num in r.smembers(key)}

                # Get the user's Telegram username (might have changed)
                try:
                    user_info = await context.bot.get_chat(user_id)
                    username = f"@{user_info.username}" if user_info.username else f"User {user_id}"
                except Exception as e:
                    logger.error(f"Could not get the user info for user {user_id}: {e}")
                    username = f"User {user_id}"  # Fallback if the username can't be retrieved

                # Get the last submitted number and its link
                last_number = max(user_numbers) if user_numbers else "N/A"
                last_submission_link = r.hget(f"group:{group_id}:message_history", last_number).decode("utf-8")

                # Add the user's stats
                user_stats.append(
                    f'⭐️ <b>{username}</b>: {len(user_numbers)} submissions (latest: <a href="{last_submission_link}">{last_number}</a>)'
                )

            # Format the stats message and reply
            user_stats_msg = "\n".join(user_stats) if user_stats else "No submissions yet."
            await update.message.reply_html(f"📊 Stats by users:\n{user_stats_msg}")

        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
            await update.message.reply_text("Could not show stats due to an error.")


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
            latest_submission_link = r.hget(f"group:{group_id}:message_history", current_number)
            if latest_submission_link:
                latest_submission_link = latest_submission_link.decode("utf-8")
                await update.message.reply_markdown_v2(
                    f"💎 Last found number is **[{current_number}]({latest_submission_link})**\n\n"
                    f"📢 Group channel: {channel_id}"
                )
            else:
                await update.message.reply_markdown_v2(
                    f"💎 Current number is {current_number}: no submissions yet\\!\n"
                    f"📢 Group channel: {channel_id}"
                )

        except Exception as e:
            logger.error(f"Error fetching info: {e}")
            await update.message.reply_text("Could not show info due to an error.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    group_id = update.message.chat_id
    user_id = update.message.from_user.id

    with MessageContext(logger, update.message):
        try:
            # Check permissions
            if not await check_admin(update, context):
                logger.warning(f"User {user_id} attempted to reset without sufficient permissions.")
                await update.message.reply_text("You must be an admin to reset the game.")
                return

            logger.info(f"Resetting the game for group {group_id} by user {user_id}.")

            # Clear Redis keys for the group
            r.delete(f"group:{group_id}:current_number")
            r.delete(f"group:{group_id}:channel_id")
            for key in r.scan_iter(match=f"group:{group_id}:user_submissions:*"):
                r.delete(key)
            r.delete(f"group:{group_id}:message_history")

            await update.message.reply_text("Game has been reset for this group.")

        except Exception as e:
            logger.error(f"Error resetting the game: {e}")
            await update.message.reply_text("Could not reset the game due to an error.")


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

    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(
        filters.CAPTION & filters.PHOTO & ~filters.COMMAND,
        submit
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
