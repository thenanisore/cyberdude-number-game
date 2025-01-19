import logging
import os
import re
import sys
from typing import Generator, Optional, Set
from urllib.parse import urlparse

import redis
from dotenv import load_dotenv
from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from logutils import ContextFilter, MessageContext

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - [group_id=%(group_id)s] - %(message)s"
    )
)
handler.addFilter(ContextFilter(["group_id"]))

logger.addHandler(handler)

# Load environment variables
load_dotenv()

# Regex to extract the number from the caption
num_p = re.compile(r"(\d+)!")

# Conversation state constants
PUBLIC_CHANNEL = 0


################
# Redis setup #
################


class GameState:
    """Class to manage the game state in Redis."""

    def __init__(self):
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_post = os.getenv("REDIS_PORT", 6379)
        if os.environ.get("REDIS_URL"):
            # Heroku Redis setup
            url = urlparse(os.environ.get("REDIS_URL"))
            logger.info("Connecting to Heroku Redis at %s:%s", url.hostname, url.port)
            self.redis = redis.Redis(
                host=url.hostname,
                port=url.port,
                password=url.password,
                ssl=(url.scheme == "rediss"),
                ssl_cert_reqs=None,
            )
        else:
            # Local Redis setup
            logger.info("Connecting to local Redis at %s:%s", redis_host, redis_post)
            self.redis = redis.Redis(host=redis_host, port=redis_post, db=0)

    def get_current_number(self, group_id) -> Optional[int]:
        number = self.redis.get(f"group:{group_id}:current_number")
        if number:
            return int(number)
        return None

    def set_current_number(self, group_id, number) -> None:
        self.redis.set(f"group:{group_id}:current_number", number)

    def get_channel_id(self, group_id) -> Optional[str]:
        channel_id = self.redis.get(f"group:{group_id}:channel_id")
        if channel_id:
            return channel_id.decode("utf-8")
        return None

    def set_channel_id(self, group_id, channel_id) -> None:
        self.redis.set(f"group:{group_id}:channel_id", channel_id)

    def get_submission_link(self, group_id, number) -> Optional[str]:
        link = self.redis.hget(f"group:{group_id}:message_history", number)
        if link:
            return link.decode("utf-8")
        return None

    def set_submission_link(self, group_id, number, link) -> None:
        self.redis.hset(f"group:{group_id}:message_history", number, link)

    def get_user_submissions(self, group_id, user_id) -> Set[int]:
        return {
            int(num)
            for num in self.redis.smembers(
                f"group:{group_id}:user_submissions:{user_id}"
            )
        }

    def add_user_submission(self, group_id, user_id, number) -> None:
        self.redis.sadd(f"group:{group_id}:user_submissions:{user_id}", number)

    def delete_for_group(self, group_id) -> None:
        keys = self.redis.scan_iter(match=f"group:{group_id}:*")
        for key in keys:
            self.redis.delete(key)

    def get_all_user_submissions(
        self, group_id
    ) -> Generator[tuple[str, Set[int]], None, None]:
        for user_key in self.redis.scan_iter(
            match=f"group:{group_id}:user_submissions:*"
        ):
            user_id = user_key.decode("utf-8").split(":")[3]
            user_numbers = {int(num) for num in self.redis.smembers(user_key)}
            yield (user_id, user_numbers)


game_state = GameState()


####################
# Command handlers #
####################


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user to associate a public channel with the current group."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        # Check if already initialized
        channel_id = game_state.get_channel_id(group_id)
        if channel_id:
            logger.info(
                "The game has already been initialized, ending the conversation."
            )
            await update.message.reply_text(
                "The game has already been initialized for this group."
            )
            return ConversationHandler.END

        logger.info("Starting the initialization for group %d", group_id)

        await update.message.reply_text(
            "Please post the handler of the public channel that will be associated with this group.\n"
            "The channel will be used to post the submissions."
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
            return chat_member.status in ["administrator", "creator"]
        except Exception as e:
            logger.error("Error checking admin permissions: %s", e)
            return False


async def is_bot_admin_in_channel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str
) -> bool:
    """Check if the bot has admin permissions in a public channel."""
    with MessageContext(logger, update.message):
        try:
            # Check if the bot can send a message to the channel
            test_message = await context.bot.send_message(
                chat_id=channel_id,
                text="Checking admin permissions. This message will self-delete.",
            )
            # If the message is sent successfully, delete it
            await test_message.delete()
            return True
        except Exception as e:
            logger.error("Bot admin check failed for channel %s: %s", channel_id, e)
            return False


async def public_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initialize the game with a public channel for posting submissions."""
    with MessageContext(logger, update.message):
        logger.info("Initializing the game")

        group_id = update.message.chat_id
        channel_id = update.message.text

        # Check if the channel exists and the bot is an admin of the channel
        try:
            logger.info("Checking the channel %s", channel_id)
            if not channel_id and not channel_id.startswith("@"):
                raise Exception("The channel name is empty or does not start with @.")
            if not await is_bot_admin_in_channel(update, context, channel_id):
                raise Exception("The bot must be an admin of the channel.")
        except Exception as e:
            logger.error("Could not init the game: %s", e)
            await update.message.reply_text(f"Could not start the bot: {e}")
            return ConversationHandler.END

        game_state.set_channel_id(group_id, channel_id)
        game_state.set_current_number(group_id, 1)

        await update.message.reply_text(
            f"The channel {channel_id} has been associated with this group."
        )

        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    with MessageContext(logger, update.message):
        user = update.message.from_user

        logger.info("%s canceled the initialization.", user.username)
        await update.message.reply_text(
            "Initialization cancelled.", reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "✨ To start the game, use the /start command and associate a public channel with the group.\n"
        "✨ The channel will be used to post the submissions.\n"
        '✨ To submit a number, send a photo with the number in the caption. It should look like this: "123!"\n'
        "✨ To view the current stats, use the /stats and /info commands."
    )


async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit a number to be compared with the current number."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        # Check if the game has been initialized
        channel_id = game_state.get_channel_id(group_id)
        if not channel_id:
            await update.message.reply_text(
                "The game has not been initialized for this group."
            )
            return
        caption = update.message.caption
        logger.info("Checking the submitted number, caption: %s", caption)
        if caption:
            number_str = num_p.match(caption.strip())
            number = int(number_str.group(1)) if number_str else None

            # Check if the requested number is already submitted
            already_existing_link = game_state.get_submission_link(group_id, number)
            if already_existing_link:
                await update.message.reply_html(
                    f'Number {number} has already been <a href="{already_existing_link}">submitted</a>!'
                )
                return

            # Check if the number is correct
            current_number = game_state.get_current_number(group_id)
            if number and number == current_number + 1:
                # Store user submission in Redis set
                game_state.add_user_submission(
                    group_id, update.message.from_user.id, number
                )
                # Prepare a message to post in the public channel
                posted_msg = await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=update.message.photo[-1],
                    caption=f"💎 Number {number} submitted by {update.message.from_user.mention_markdown_v2()} 💎",
                    parse_mode="MarkdownV2",
                )
                # Store message link in message history hash
                game_state.set_submission_link(group_id, number, posted_msg.link)
                # Increment and persist requested number if it's larger than the current number
                logger.info(
                    "Updating the current number to %d was %d.", number, current_number
                )
                game_state.set_current_number(group_id, number)

                await update.message.reply_markdown_v2(
                    f"🎉 [Found {number}]({posted_msg.link})\\! 🎉"
                )
            else:
                logger.error(
                    "The number is incorrect, expected %d, not %d.",
                    current_number + 1,
                    number,
                )
                await update.message.reply_text(
                    f"Wrong number! Expected {current_number + 1}, not {number}."
                )
        else:
            logger.error(
                "The caption is empty for message %d", update.message.message_id
            )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the stats of the users who have submitted numbers."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        try:
            # Check if the game has been initialized
            channel_id = game_state.get_channel_id(group_id)
            if not channel_id:
                await update.message.reply_text(
                    "The game has not been initialized for this group."
                )
                return

            # Fetch user stats
            user_stats = []
            for user_id, user_numbers in game_state.get_all_user_submissions(group_id):
                logger.info("Collecting stats for user %s", user_id)

                # Get the user's Telegram username (might have changed)
                try:
                    user_info = await context.bot.get_chat(user_id)
                    username = (
                        f"@{user_info.username}"
                        if user_info.username
                        else f"User {user_id}"
                    )
                except Exception as e:
                    logger.error(
                        "Could not get the user info for user %s, %s", user_id, e
                    )
                    username = (
                        f"User {user_id}"  # Fallback if the username can't be retrieved
                    )

                # Get the last submitted number and its link
                last_number = max(user_numbers) if user_numbers else "N/A"
                last_submission_link = game_state.get_submission_link(
                    group_id, last_number
                )

                # Add the user's stats
                user_stats.append(
                    f'⭐️ <b>{username}</b>: {len(user_numbers)} submissions (latest: <a href="{last_submission_link}">{last_number}</a>)'
                )

            # Format the stats message and reply
            user_stats_msg = (
                "\n".join(user_stats) if user_stats else "No submissions yet."
            )
            await update.message.reply_html(
                f"📊 Stats by users:\n{user_stats_msg}", disable_web_page_preview=True
            )

        except Exception as e:
            logger.error("Error fetching stats: %s", e)
            await update.message.reply_text("Could not show stats due to an error.")


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the current number, who found it, and the associated channel."""
    group_id = update.message.chat_id
    with MessageContext(logger, update.message):
        try:
            # Fetch the associated public channel
            channel_id = game_state.get_channel_id(group_id)
            if not channel_id:
                await update.message.reply_text(
                    "The game has not been initialized for this group."
                )
                return
            # Fetch the current number
            current_number = game_state.get_current_number(group_id)
            # Fetch the latest submission's link
            latest_submission_link = game_state.get_submission_link(
                group_id, current_number
            )
            if latest_submission_link:
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
            logger.error("Error fetching info: %s", e)
            await update.message.reply_text("Could not show info due to an error.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the game for the group, removing everything associated with it."""
    group_id = update.message.chat_id
    user_id = update.message.from_user.id

    with MessageContext(logger, update.message):
        try:
            # Check permissions
            if not await check_admin(update, context):
                logger.warning(
                    "User %d attempted to reset without sufficient permissions.",
                    user_id,
                )
                await update.message.reply_text(
                    "You must be an admin to reset the game."
                )
                return

            logger.info("Resetting the game for group %d by user %d", group_id, user_id)

            # Clear Redis keys for the group
            game_state.delete_for_group(group_id)

            await update.message.reply_text("Game has been reset for this group.")

        except Exception as e:
            logger.error("Error resetting the game: %s", e)
            await update.message.reply_text("Could not reset the game due to an error.")


def main() -> None:
    """Start the bot."""
    token = os.getenv("TOKEN")
    app = ApplicationBuilder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PUBLIC_CHANNEL: [MessageHandler(filters.TEXT, public_channel)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)

    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(
        MessageHandler(filters.CAPTION & filters.PHOTO & ~filters.COMMAND, submit)
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
