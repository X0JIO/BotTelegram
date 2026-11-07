# Telegram booking calendar bot

Пользовательская инструкция с разграничением прав: [USER_GUIDE.md](USER_GUIDE.md).

## Requirements

- Python 3.10 or newer
- A bot token created with [@BotFather](https://t.me/BotFather)

## Install and run (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `BOT_TOKEN` to your token, then start the bot:

```powershell
python bot.py
```

The SQLite database is created automatically at `data/bookings.sqlite3`. A clean database contains free slots for the next 60 days and no test bookings. The database path can be changed with `DB_PATH` in `.env`.

Set `ADMIN_IDS` in `.env` to the numeric Telegram user ID(s) allowed to manage bookings. To find your ID, send `/start` to the bot and inspect the sender ID in bot logs or use a trusted Telegram ID helper. Separate multiple IDs with commas.

Системная кнопка **Menu** Telegram показывает команды: клиентам — календарь и помощь, администраторам — также управление и список команд. После `/start` командами можно пользоваться из этого меню. В личном чате бот удаляет входящие сообщения пользователя после получения, а интерфейс обновляет на месте.

After `/start`, the persistent keyboard contains **🗓 Управление календарём** for administrators (or **📅 Календарь** for clients) and **📖 Помощь**. Administrators open the native Telegram **Menu** button for commands; `/commands` opens the guided management actions. Each stage has an in-chat back control. Slot cards contain description, importance, blocking, and release controls; releasing a slot or a whole day asks for confirmation. Importance values are `обычная`, `важная`, and `срочная`.

## Production notes

- Keep `.env` private; it is excluded by `.gitignore`.
- Run one polling process per bot token. SQLite serializes writes and booking uses an immediate transaction plus a conditional update to prevent double booking.
- The configured appointment hours are in `WORKING_HOURS` in `bot.py`.
- Existing demo bookings can be cleared from the database from the administrator's **⚙️ Команды** menu by selecting the date and release action.
- Use a process supervisor (for example, Windows Task Scheduler or systemd) to restart the process after host reboot.
- On Windows, `run_bot.ps1` supervises the bot and restarts it if Python exits. For startup after reboot, configure Task Scheduler to run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\Telegram bot\run_bot.ps1"`.



