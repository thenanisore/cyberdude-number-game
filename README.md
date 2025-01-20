# number-hunting-game

A Telegram bot for the number hunting game.

The game is happening inside a Telegram group and also require a separate public channel which will serve as the game history. Participants look for the next number in real life objects and submit photos of their findings.

The bot must be an administrator in both the group and the channel to work correctly.

---

## Features
- **Photo Submission**: Players submit numbers by sending photos with captions like `123!`.
- **Automatic Validation**: Ensures the numbers are submitted in ascending order.
- **Statistics**: Tracks and displays user submissions.
- **Multi-group Support**: The bot can manage games across multiple groups and channels.

---

## Commands

### General Commands
- `/start` - Start the game and associate a public channel for submissions.
- `/help` - Show usage instructions.
- `/stats` - View user statistics.
- `/info` - Get the current number and associated channel.

### Admin Commands
- `/reset` - Reset the game for the group.

---

## Installation 🖥️

### Prerequisites
- Python 3.8+
- Redis
- Telegram bot token (via [BotFather](https://core.telegram.org/bots#botfather))

### Steps

1. Clone the repository:
   ```bash
   git clone https://github.com/thenanisore/number-hunting-game.git
   cd number-hunting-game
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
   
3. Configure environment variables:
   Create a `.env` file and add the following:
   ```env
   TOKEN=your_telegram_bot_token
   REDIS_HOST=localhost
   REDIS_PORT=6379
   ```
   
4. Start Redis locally:
   ```bash
   docker run -p 6379:6379 redis/redis-stack:latest
   ```
   
   It should be available at `localhost:6379`.

4. Start the bot:
   ```bash
   python app.py
   ```

---

## Usage 🎮

1. Add the bot to your group and make it an admin.
2. Create a public channel which will serve as the game history. Add the bot as an admin.
3. Use `/start` to initialize the game and associate a public channel.
4. Participants submit photos with captions like `123!` in ascending order.
5. Use `/stats` to view leaderboards and `/info` for the latest submission.

---

## License 📜

This project is licensed under the MIT License. See the `LICENSE` file for details.
